package jflp.instr;

import java.util.ArrayList;
import java.util.BitSet;
import java.util.Iterator;
import java.util.List;

import org.objectweb.asm.Opcodes;
import org.objectweb.asm.tree.AbstractInsnNode;
import org.objectweb.asm.tree.FrameNode;
import org.objectweb.asm.tree.InsnList;
import org.objectweb.asm.tree.InsnNode;
import org.objectweb.asm.tree.LdcInsnNode;
import org.objectweb.asm.tree.MethodNode;
import org.objectweb.asm.tree.VarInsnNode;

/**
 * Def-use coverage ({@code duas=true}), with BA-DUA's bitwise algorithm.
 *
 * Each invocation keeps, in new local variables, the set of live definitions
 * ({@code alive}) and the p-uses that the block just left can complete
 * ({@code sleepy}), one {@code long} per 64 DUAs. On entering block k:
 * <ol>
 * <li>p-uses on the edge just taken: {@code covered |= sleepy & edgeIn(k)};</li>
 * <li>c-uses of k: {@code covered |= alive & cuse(k)};</li>
 * <li>{@code sleepy = alive & puse(k)} when k branches, else 0;</li>
 * <li>{@code alive = (alive & ~kill(k)) | gen(k)}.</li>
 * </ol>
 * {@code covered} is the class's {@code long[]} in the runtime, so nothing has
 * to be flushed when the method exits or throws. As in BA-DUA, a block's
 * definitions become live when the block is entered.
 *
 * The new locals appear in every stack map frame of the method: frames are
 * expanded, so each gets padded to the old {@code maxLocals} and extended.
 */
final class Duas {

    /** Methods with more DUAs than this are left out (their masks would bloat the code). */
    static final int MAX_DUAS = 1024;

    static final class Site {
        final MethodNode method;
        final DefUse du;
        final int wordOffset; // first word of this method in the class's bit set
        final int words;

        Site(final MethodNode method, final DefUse du, final int wordOffset, final int words) {
            this.method = method;
            this.du = du;
            this.wordOffset = wordOffset;
            this.words = words;
        }
    }

    private Duas() {
    }

    /** Applies a site; {@code longsIndex} is the class's bit set in the runtime. Returns the extra stack. */
    static int apply(final Site s, final int longsIndex) {
        final MethodNode m = s.method;
        final int w = s.words;
        final int start = m.maxLocals;
        final int alive = start;
        final int sleepy = start + 2 * w;
        final int arr = start + 4 * w;
        boolean anyPuse = false;
        for (final DefUse.Block b : s.du.blocks) {
            anyPuse |= !b.puseNode.isEmpty();
        }

        patchFrames(m, start, w);

        // Once, before any label: entry can be a loop target, and parameters are defined only once.
        final InsnList init = new InsnList();
        int stack = Access.duaArray(init, longsIndex);
        init.add(new VarInsnNode(Opcodes.ASTORE, arr));
        for (int i = 0; i < w; i++) {
            init.add(ldc(word(s.du.entryGen, i)));
            init.add(new VarInsnNode(Opcodes.LSTORE, alive + 2 * i));
            init.add(new InsnNode(Opcodes.LCONST_0));
            init.add(new VarInsnNode(Opcodes.LSTORE, sleepy + 2 * i));
        }
        m.instructions.insert(init);

        for (int bi = 0; bi < s.du.blocks.size(); bi++) {
            final DefUse.Block b = s.du.blocks.get(bi);
            final InsnList code = new InsnList();
            for (int i = 0; i < w; i++) {
                final long edge = word(b.edgeIn, i);
                if (edge != 0) {
                    cover(code, arr, s.wordOffset + i, sleepy + 2 * i, edge);
                }
                final long cuse = word(b.cuse, i);
                if (cuse != 0) {
                    cover(code, arr, s.wordOffset + i, alive + 2 * i, cuse);
                }
            }
            for (int i = 0; i < w && anyPuse; i++) {
                final long puse = word(b.puseNode, i);
                if (puse != 0) {
                    code.add(new VarInsnNode(Opcodes.LLOAD, alive + 2 * i));
                    code.add(ldc(puse));
                    code.add(new InsnNode(Opcodes.LAND));
                } else {
                    code.add(new InsnNode(Opcodes.LCONST_0));
                }
                code.add(new VarInsnNode(Opcodes.LSTORE, sleepy + 2 * i));
            }
            for (int i = 0; i < w; i++) {
                final long kill = word(b.kill, i);
                final long gen = word(b.gen, i);
                if (kill != 0 || gen != 0) {
                    code.add(new VarInsnNode(Opcodes.LLOAD, alive + 2 * i));
                    if (kill != 0) {
                        code.add(ldc(~kill));
                        code.add(new InsnNode(Opcodes.LAND));
                    }
                    if (gen != 0) {
                        code.add(ldc(gen));
                        code.add(new InsnNode(Opcodes.LOR));
                    }
                    code.add(new VarInsnNode(Opcodes.LSTORE, alive + 2 * i));
                }
            }
            if (code.size() == 0) {
                continue;
            }
            final AbstractInsnNode first = s.du.firstInsn.get(bi);
            if (first.getOpcode() == Opcodes.NEW) {
                m.instructions.insert(first, code); // see Instrumenter: frames name a NEW by its label
            } else {
                m.instructions.insertBefore(first, code);
            }
        }
        m.maxLocals = start + 4 * w + 1;
        return Math.max(stack, 8);
    }

    /** {@code arr[word] |= local & mask}. */
    private static void cover(final InsnList code, final int arr, final int word, final int local, final long mask) {
        code.add(new VarInsnNode(Opcodes.ALOAD, arr));
        code.add(Instrumenter.push(word));
        code.add(new InsnNode(Opcodes.DUP2));
        code.add(new InsnNode(Opcodes.LALOAD));
        code.add(new VarInsnNode(Opcodes.LLOAD, local));
        code.add(ldc(mask));
        code.add(new InsnNode(Opcodes.LAND));
        code.add(new InsnNode(Opcodes.LOR));
        code.add(new InsnNode(Opcodes.LASTORE));
    }

    /** Pads every frame's locals to {@code start} slots and appends alive, sleepy (longs) and the bit set. */
    private static void patchFrames(final MethodNode m, final int start, final int words) {
        for (Iterator<AbstractInsnNode> it = m.instructions.iterator(); it.hasNext();) {
            final AbstractInsnNode insn = it.next();
            if (!(insn instanceof FrameNode)) {
                continue;
            }
            final FrameNode f = (FrameNode) insn;
            final List<Object> locals = f.local == null ? new ArrayList<Object>() : new ArrayList<Object>(f.local);
            int slots = 0;
            for (final Object o : locals) {
                slots += o == Opcodes.LONG || o == Opcodes.DOUBLE ? 2 : 1;
            }
            while (slots < start) {
                locals.add(Opcodes.TOP);
                slots++;
            }
            for (int i = 0; i < 2 * words; i++) {
                locals.add(Opcodes.LONG);
            }
            locals.add("[J");
            f.local = locals;
        }
    }

    private static long word(final BitSet b, final int i) {
        long v = 0;
        for (int j = 0; j < 64; j++) {
            if (b.get(i * 64 + j)) {
                v |= 1L << j;
            }
        }
        return v;
    }

    private static AbstractInsnNode ldc(final long v) {
        if (v == 0) {
            return new InsnNode(Opcodes.LCONST_0);
        }
        if (v == 1) {
            return new InsnNode(Opcodes.LCONST_1);
        }
        return new LdcInsnNode(Long.valueOf(v));
    }
}
