package jflp.instr;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;

import jflp.data.RawFormat;

import org.objectweb.asm.Label;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.commons.AnalyzerAdapter;
import org.objectweb.asm.tree.AbstractInsnNode;
import org.objectweb.asm.tree.ClassNode;
import org.objectweb.asm.tree.FrameNode;
import org.objectweb.asm.tree.InsnList;
import org.objectweb.asm.tree.JumpInsnNode;
import org.objectweb.asm.tree.LabelNode;
import org.objectweb.asm.tree.LookupSwitchInsnNode;
import org.objectweb.asm.tree.MethodNode;
import org.objectweb.asm.tree.TableSwitchInsnNode;

/**
 * Branch probes ({@code branches=true}): one probe per edge of every conditional
 * jump and switch, as JaCoCo places them.
 *
 * <ul>
 * <li>Not taken: a probe right after the jump, on the fall-through path.</li>
 * <li>Taken, and each distinct switch target: the jump is redirected to a
 * trampoline appended to the method that marks the probe and jumps on to the
 * original target.</li>
 * </ul>
 *
 * A trampoline is a new jump target, so class files with stack map frames
 * (version 50+) need a frame there: the state right after the jump. It is taken
 * from {@link AnalyzerAdapter}, which derives it from the existing frames without
 * loading any class. The analysis runs on the original code; line probes, being
 * stack-neutral, do not change that state.
 *
 * As for lines, JaCoCo's filters decide: jumps they ignore get no probes, and a
 * jump in a duplicated finally block counts for the representative copy.
 */
final class Branches {

    /** A jump or switch to instrument, with the local probe id of each of its edges. */
    static final class Site {
        final MethodNode method;
        final AbstractInsnNode node;
        /** Jump: {taken, not taken}. Switch: one per distinct target, in {@link #targets} order. */
        final int[] probes;
        final List<LabelNode> targets;
        /** State after the jump in frame format, or null when the class has no frames. */
        final Object[] locals;
        final Object[] stack;

        Site(final MethodNode method, final AbstractInsnNode node, final int[] probes, final List<LabelNode> targets,
                final Object[] locals, final Object[] stack) {
            this.method = method;
            this.node = node;
            this.probes = probes;
            this.targets = targets;
            this.locals = locals;
            this.stack = stack;
        }
    }

    /** Probe metadata collected while planning: line, kind and arg of each new probe. */
    static final class Probes {
        final List<Integer> lines;
        final List<Byte> kinds;
        final List<Integer> args;

        Probes(final List<Integer> lines, final List<Byte> kinds, final List<Integer> args) {
            this.lines = lines;
            this.kinds = kinds;
            this.args = args;
        }

        int add(final int line, final int arg) {
            lines.add(Integer.valueOf(line));
            kinds.add(Byte.valueOf(RawFormat.KIND_BRANCH));
            args.add(Integer.valueOf(arg));
            return lines.size() - 1;
        }
    }

    private Branches() {
    }

    static void plan(final ClassNode cn, final MethodNode m, final SourceFilter.Result verdict,
            final Map<AbstractInsnNode, Integer> lineOf, final Probes probes, final List<Site> sites) {
        final List<AbstractInsnNode> branching = new ArrayList<AbstractInsnNode>();
        for (Iterator<AbstractInsnNode> it = m.instructions.iterator(); it.hasNext();) {
            final AbstractInsnNode insn = it.next();
            if (isConditional(insn) || isSwitch(insn)) {
                branching.add(insn);
            }
        }
        if (branching.isEmpty()) {
            return;
        }
        final boolean frames = (cn.version & 0xFFFF) >= Opcodes.V1_6;
        final Map<AbstractInsnNode, Object[][]> states = frames ? states(cn, m, branching) : null;

        // Representatives first: a duplicate may come before its representative.
        final Map<AbstractInsnNode, int[]> ids = new HashMap<AbstractInsnNode, int[]>();
        final Map<Integer, Integer> jumpsOnLine = new HashMap<Integer, Integer>();
        for (final AbstractInsnNode insn : branching) {
            if (verdict.countsFor(insn) != insn) {
                continue;
            }
            final Integer line = lineOf.get(insn);
            if (line == null) {
                continue;
            }
            Integer k = jumpsOnLine.get(line);
            k = Integer.valueOf(k == null ? 1 : k.intValue() + 1);
            jumpsOnLine.put(line, k);
            final int edges = isSwitch(insn) ? distinctTargets(insn).size() : 2;
            final int[] p = new int[edges];
            for (int e = 0; e < edges; e++) {
                final int edge = isSwitch(insn) ? RawFormat.EDGE_TARGET + e
                        : e == 0 ? RawFormat.EDGE_TAKEN : RawFormat.EDGE_NOT_TAKEN;
                p[e] = probes.add(line.intValue(), (k.intValue() << 16) | edge);
            }
            ids.put(insn, p);
        }
        for (final AbstractInsnNode insn : branching) {
            final AbstractInsnNode rep = verdict.countsFor(insn);
            final int[] p = rep == null ? null : ids.get(rep);
            if (p == null) {
                continue;
            }
            final List<LabelNode> targets = isSwitch(insn) ? distinctTargets(insn)
                    : java.util.Collections.singletonList(((JumpInsnNode) insn).label);
            if (isSwitch(insn) && targets.size() != p.length) {
                continue; // a duplicate copy whose switch differs from its representative
            }
            Object[] locals = null;
            Object[] stack = null;
            if (frames) {
                final Object[][] s = states.get(insn);
                if (s == null) {
                    continue; // unreachable code: no state to build a frame from
                }
                locals = s[0];
                stack = s[1];
            }
            sites.add(new Site(m, insn, p, targets, locals, stack));
        }
    }

    /** Applies a site; probe ids are {@code base + site.probes[i]}. Returns the extra stack it needs. */
    static int apply(final Site s, final int base) {
        final InsnList code = s.method.instructions;
        int stack = 0;
        if (s.node instanceof JumpInsnNode) {
            final JumpInsnNode jump = (JumpInsnNode) s.node;
            final InsnList notTaken = new InsnList();
            stack = Math.max(stack, Access.probe(notTaken, base + s.probes[1]));
            code.insert(jump, notTaken);
            jump.label = trampoline(s, jump.label, base + s.probes[0]);
        } else {
            for (int i = 0; i < s.targets.size(); i++) {
                final LabelNode target = s.targets.get(i);
                final LabelNode t = trampoline(s, target, base + s.probes[i]);
                retarget(s.node, target, t);
            }
        }
        return Math.max(stack, 3);
    }

    private static LabelNode trampoline(final Site s, final LabelNode target, final int probe) {
        final LabelNode start = new LabelNode();
        final InsnList t = new InsnList();
        t.add(start);
        if (s.locals != null) {
            t.add(new FrameNode(Opcodes.F_NEW, s.locals.length, s.locals, s.stack.length, s.stack));
        }
        Access.probe(t, probe);
        t.add(new JumpInsnNode(Opcodes.GOTO, target));
        s.method.instructions.add(t);
        return start;
    }

    private static void retarget(final AbstractInsnNode sw, final LabelNode from, final LabelNode to) {
        final List<LabelNode> labels;
        if (sw instanceof TableSwitchInsnNode) {
            final TableSwitchInsnNode t = (TableSwitchInsnNode) sw;
            if (t.dflt == from) {
                t.dflt = to;
            }
            labels = t.labels;
        } else {
            final LookupSwitchInsnNode l = (LookupSwitchInsnNode) sw;
            if (l.dflt == from) {
                l.dflt = to;
            }
            labels = l.labels;
        }
        for (int i = 0; i < labels.size(); i++) {
            if (labels.get(i) == from) {
                labels.set(i, to);
            }
        }
    }

    static boolean isConditional(final AbstractInsnNode insn) {
        final int op = insn.getOpcode();
        return op >= Opcodes.IFEQ && op <= Opcodes.IF_ACMPNE || op == Opcodes.IFNULL || op == Opcodes.IFNONNULL;
    }

    static boolean isSwitch(final AbstractInsnNode insn) {
        return insn instanceof TableSwitchInsnNode || insn instanceof LookupSwitchInsnNode;
    }

    /** Case targets then default, each once, as JaCoCo counts switch branches. */
    static List<LabelNode> distinctTargets(final AbstractInsnNode sw) {
        final List<LabelNode> all = new ArrayList<LabelNode>();
        if (sw instanceof TableSwitchInsnNode) {
            all.addAll(((TableSwitchInsnNode) sw).labels);
            all.add(((TableSwitchInsnNode) sw).dflt);
        } else {
            all.addAll(((LookupSwitchInsnNode) sw).labels);
            all.add(((LookupSwitchInsnNode) sw).dflt);
        }
        final List<LabelNode> distinct = new ArrayList<LabelNode>();
        for (final LabelNode l : all) {
            if (!distinct.contains(l)) {
                distinct.add(l);
            }
        }
        return distinct;
    }

    /** Frame-format locals and stack right after each branching instruction (its operands popped). */
    private static Map<AbstractInsnNode, Object[][]> states(final ClassNode cn, final MethodNode m,
            final List<AbstractInsnNode> branching) {
        final Map<Label, LabelNode> labelNodes = new HashMap<Label, LabelNode>();
        for (Iterator<AbstractInsnNode> it = m.instructions.iterator(); it.hasNext();) {
            final AbstractInsnNode insn = it.next();
            if (insn instanceof LabelNode) {
                labelNodes.put(((LabelNode) insn).getLabel(), (LabelNode) insn);
            }
        }
        final Map<AbstractInsnNode, Object[][]> states = new HashMap<AbstractInsnNode, Object[][]>();
        final AnalyzerAdapter[] analyzer = new AnalyzerAdapter[1];
        final int[] next = { 0 };
        // The delegate is called before the analyzer applies the instruction, so the
        // analyzer's lists still hold the state the jump starts from.
        final MethodVisitor recorder = new MethodVisitor(Opcodes.ASM9) {
            @Override
            public void visitJumpInsn(final int opcode, final Label label) {
                if (opcode != Opcodes.GOTO && opcode != Opcodes.JSR) {
                    record(1 + (opcode >= Opcodes.IF_ICMPEQ && opcode <= Opcodes.IF_ACMPNE ? 1 : 0));
                }
            }

            @Override
            public void visitTableSwitchInsn(final int min, final int max, final Label dflt, final Label... labels) {
                record(1);
            }

            @Override
            public void visitLookupSwitchInsn(final Label dflt, final int[] keys, final Label[] labels) {
                record(1);
            }

            private void record(final int pops) {
                final AbstractInsnNode insn = branching.get(next[0]++);
                final AnalyzerAdapter a = analyzer[0];
                if (a.locals == null || a.stack == null) {
                    return;
                }
                final List<Object> stack = new ArrayList<Object>(a.stack.subList(0, a.stack.size() - pops));
                states.put(insn, new Object[][] { frame(a.locals, labelNodes, true), frame(stack, labelNodes, false) });
            }
        };
        analyzer[0] = new AnalyzerAdapter(cn.name, m.access, m.name, m.desc, recorder);
        m.accept(analyzer[0]);
        return states;
    }

    /** AnalyzerAdapter's slot lists (two entries per long/double) to visitFrame's format. */
    private static Object[] frame(final List<Object> slots, final Map<Label, LabelNode> labels, final boolean trim) {
        final List<Object> out = new ArrayList<Object>();
        for (int i = 0; i < slots.size(); i++) {
            Object o = slots.get(i);
            if (o instanceof Label) {
                o = labels.get(o);
            }
            out.add(o);
            if (o == Opcodes.LONG || o == Opcodes.DOUBLE) {
                i++;
            }
        }
        if (trim) {
            while (!out.isEmpty() && out.get(out.size() - 1) == Opcodes.TOP) {
                out.remove(out.size() - 1);
            }
        }
        return out.toArray();
    }
}
