package jflp.instr;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.zip.CRC32;

import jflp.data.RawFormat;

import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.commons.CodeSizeEvaluator;
import org.objectweb.asm.commons.JSRInlinerAdapter;
import org.objectweb.asm.tree.AbstractInsnNode;
import org.objectweb.asm.tree.ClassNode;
import org.objectweb.asm.tree.InsnList;
import org.objectweb.asm.tree.IntInsnNode;
import org.objectweb.asm.tree.JumpInsnNode;
import org.objectweb.asm.tree.LabelNode;
import org.objectweb.asm.tree.LdcInsnNode;
import org.objectweb.asm.tree.LineNumberNode;
import org.objectweb.asm.tree.LookupSwitchInsnNode;
import org.objectweb.asm.tree.MethodNode;
import org.objectweb.asm.tree.TableSwitchInsnNode;
import org.objectweb.asm.tree.TryCatchBlockNode;

/**
 * Line probes.
 *
 * A line is covered when execution reaches the start of any of its code
 * segments: a line-number entry, or a jump/handler target inside the line.
 * This differs on purpose from JaCoCo, which marks a line covered only once a
 * later probe confirms its instructions completed, and so reports the line
 * that throws an exception as not covered. For fault localization that line is
 * often the fault itself.
 *
 * Inserted code is straight-line and stack-neutral, so existing stack map
 * frames stay valid and no frame needs computing, which keeps the transformer
 * free of class loading and lets it handle every class file version ASM reads.
 */
public final class Instrumenter {

    public static final String RUNTIME = "jflp/rt/R";

    /** HotSpot does not JIT-compile methods with more bytecode than this (-XX:HugeMethodLimit). */
    public static final int HUGE_METHOD_LIMIT = 8000;

    private Instrumenter() {
    }

    /** A parsed class ready to be instrumented once its probe ids are allocated. */
    public static final class Analysis {
        public final ClassInfo info;
        final ClassNode node;
        final List<Insertion> insertions;
        /** Methods that the probes pushed over {@link #HUGE_METHOD_LIMIT}; filled by {@link #instrument}. */
        public final List<String> grownHuge = new ArrayList<String>();

        Analysis(final ClassInfo info, final ClassNode node, final List<Insertion> insertions) {
            this.info = info;
            this.node = node;
            this.insertions = insertions;
        }
    }

    static final class Insertion {
        final MethodNode method;
        final AbstractInsnNode before;
        final int probe;

        Insertion(final MethodNode method, final AbstractInsnNode before, final int probe) {
            this.method = method;
            this.before = before;
            this.probe = probe;
        }
    }

    /**
     * Parses a class file and decides where its probes go. Returns {@code null}
     * for classes JaCoCo's analyzer also skips (synthetic classes such as enum
     * switch maps, and module descriptors), so they never appear in reports.
     */
    public static Analysis analyze(final byte[] classFile) {
        final ClassReader reader = new ClassReader(classFile);
        if ((reader.getAccess() & (Opcodes.ACC_SYNTHETIC | Opcodes.ACC_MODULE)) != 0) {
            return null;
        }
        final ClassNode cn = new ClassNode();
        reader.accept(cn, 0);
        inlineSubroutines(cn);

        final Map<Integer, Integer> probeOfLine = new HashMap<Integer, Integer>();
        final List<Integer> lines = new ArrayList<Integer>();
        final List<Insertion> insertions = new ArrayList<Insertion>();
        final SourceFilter filter = new SourceFilter(cn);

        for (final Object o : cn.methods) {
            final MethodNode m = (MethodNode) o;
            if (m.instructions.size() == 0) {
                continue;
            }
            final SourceFilter.Result verdict = filter.filter(m);
            final Map<AbstractInsnNode, Integer> lineOf = lineOf(m);
            final Set<LabelNode> targets = targets(m);
            // A probe goes wherever a segment starts or the line credited changes, so
            // every line JaCoCo reports gets a probe and each probe credits one line.
            Integer last = null;
            for (Iterator<AbstractInsnNode> it = m.instructions.iterator(); it.hasNext();) {
                final AbstractInsnNode insn = it.next();
                if (insn instanceof LineNumberNode || (insn instanceof LabelNode && targets.contains(insn))) {
                    last = null; // a new code segment starts
                } else if (insn.getOpcode() >= 0) {
                    final AbstractInsnNode rep = verdict.countsFor(insn);
                    final Integer line = rep == null ? null : lineOf.get(rep);
                    if (line == null || line.equals(last)) {
                        continue;
                    }
                    Integer probe = probeOfLine.get(line);
                    if (probe == null) {
                        probe = lines.size();
                        probeOfLine.put(line, probe);
                        lines.add(line);
                    }
                    insertions.add(new Insertion(m, insn, probe));
                    last = line;
                }
            }
        }

        final int[] probeLines = new int[lines.size()];
        final byte[] kinds = new byte[lines.size()];
        for (int i = 0; i < probeLines.length; i++) {
            probeLines[i] = lines.get(i);
            kinds[i] = RawFormat.KIND_LINE;
        }
        final ClassInfo info = new ClassInfo(cn.name, cn.sourceFile, crc(classFile), probeLines, kinds);
        return new Analysis(info, cn, insertions);
    }

    /** Emits the instrumented class with probe ids {@code base .. base + probeCount - 1}. */
    public static byte[] instrument(final Analysis a, final int base) {
        final Map<MethodNode, Integer> touched = new HashMap<MethodNode, Integer>();
        final Map<MethodNode, Integer> sizeBefore = new HashMap<MethodNode, Integer>();
        for (final Insertion ins : a.insertions) {
            if (!sizeBefore.containsKey(ins.method)) {
                sizeBefore.put(ins.method, Integer.valueOf(codeSize(ins.method)));
            }
            final InsnList probe = new InsnList();
            final int stack = Access.probe(probe, base + ins.probe);
            ins.method.instructions.insertBefore(ins.before, probe);
            touched.put(ins.method, Integer.valueOf(stack));
        }
        for (final Map.Entry<MethodNode, Integer> e : touched.entrySet()) {
            final MethodNode m = e.getKey();
            m.maxStack += e.getValue().intValue();
            if (sizeBefore.get(m).intValue() <= HUGE_METHOD_LIMIT && codeSize(m) > HUGE_METHOD_LIMIT) {
                a.grownHuge.add(a.info.name.replace('/', '.') + "." + m.name);
            }
        }
        final ClassWriter cw = new ClassWriter(0);
        a.node.accept(cw);
        return cw.toByteArray();
    }

    private static int codeSize(final MethodNode m) {
        final CodeSizeEvaluator e = new CodeSizeEvaluator(null);
        m.accept(e);
        return e.getMaxSize();
    }

    /**
     * Class files for Java 1.4 and older compile finally blocks to JSR/RET
     * subroutines. They are inlined, as JaCoCo does before analyzing, so that
     * the filters see the same code; the inlined code behaves identically.
     */
    private static void inlineSubroutines(final ClassNode cn) {
        for (int i = 0; i < cn.methods.size(); i++) {
            final MethodNode m = cn.methods.get(i);
            boolean jsr = false;
            for (Iterator<AbstractInsnNode> it = m.instructions.iterator(); it.hasNext() && !jsr;) {
                jsr = it.next().getOpcode() == Opcodes.JSR;
            }
            if (jsr) {
                final String[] exceptions = m.exceptions.toArray(new String[m.exceptions.size()]);
                final MethodNode inlined = new MethodNode(Opcodes.ASM9, m.access, m.name, m.desc, m.signature,
                        exceptions);
                m.accept(new JSRInlinerAdapter(inlined, m.access, m.name, m.desc, m.signature, exceptions));
                cn.methods.set(i, inlined);
            }
        }
    }

    /** Source line of each instruction (the last line entry before it), as JaCoCo assigns them. */
    private static Map<AbstractInsnNode, Integer> lineOf(final MethodNode m) {
        final Map<AbstractInsnNode, Integer> lines = new HashMap<AbstractInsnNode, Integer>();
        Integer current = null;
        for (Iterator<AbstractInsnNode> it = m.instructions.iterator(); it.hasNext();) {
            final AbstractInsnNode insn = it.next();
            if (insn instanceof LineNumberNode) {
                current = Integer.valueOf(((LineNumberNode) insn).line);
            } else if (insn.getOpcode() >= 0 && current != null) {
                lines.put(insn, current);
            }
        }
        return lines;
    }

    private static Set<LabelNode> targets(final MethodNode m) {
        final Set<LabelNode> t = new HashSet<LabelNode>();
        for (Iterator<AbstractInsnNode> it = m.instructions.iterator(); it.hasNext();) {
            final AbstractInsnNode insn = it.next();
            if (insn instanceof JumpInsnNode) {
                t.add(((JumpInsnNode) insn).label);
            } else if (insn instanceof TableSwitchInsnNode) {
                final TableSwitchInsnNode s = (TableSwitchInsnNode) insn;
                t.add(s.dflt);
                addAll(t, s.labels);
            } else if (insn instanceof LookupSwitchInsnNode) {
                final LookupSwitchInsnNode s = (LookupSwitchInsnNode) insn;
                t.add(s.dflt);
                addAll(t, s.labels);
            }
        }
        for (final Object o : m.tryCatchBlocks) {
            t.add(((TryCatchBlockNode) o).handler);
        }
        return t;
    }

    private static void addAll(final Set<LabelNode> t, final List<?> labels) {
        for (final Object l : labels) {
            t.add((LabelNode) l);
        }
    }

    static AbstractInsnNode push(final int v) {
        if (v >= -1 && v <= 5) {
            return new org.objectweb.asm.tree.InsnNode(Opcodes.ICONST_0 + v);
        }
        if (v >= Byte.MIN_VALUE && v <= Byte.MAX_VALUE) {
            return new IntInsnNode(Opcodes.BIPUSH, v);
        }
        if (v >= Short.MIN_VALUE && v <= Short.MAX_VALUE) {
            return new IntInsnNode(Opcodes.SIPUSH, v);
        }
        return new LdcInsnNode(Integer.valueOf(v));
    }

    static long crc(final byte[] b) {
        final CRC32 c = new CRC32();
        c.update(b, 0, b.length);
        return c.getValue();
    }
}
