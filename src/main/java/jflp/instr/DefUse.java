package jflp.instr;

import java.util.ArrayList;
import java.util.BitSet;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import org.objectweb.asm.Opcodes;
import org.objectweb.asm.Type;
import org.objectweb.asm.tree.AbstractInsnNode;
import org.objectweb.asm.tree.IincInsnNode;
import org.objectweb.asm.tree.InsnList;
import org.objectweb.asm.tree.JumpInsnNode;
import org.objectweb.asm.tree.LabelNode;
import org.objectweb.asm.tree.LocalVariableNode;
import org.objectweb.asm.tree.LookupSwitchInsnNode;
import org.objectweb.asm.tree.MethodNode;
import org.objectweb.asm.tree.TableSwitchInsnNode;
import org.objectweb.asm.tree.TryCatchBlockNode;
import org.objectweb.asm.tree.VarInsnNode;

/**
 * Def-use associations (DUAs) of the local variables of one method, and the
 * per-block bit masks the runtime needs (the bitwise algorithm of BA-DUA).
 *
 * A DUA is a definition of a local variable and a use that the definition
 * reaches without redefinition: a c-use in a block with one successor, or a
 * p-use on each outgoing edge of a block that branches. Parameters are defined
 * at method entry; {@code this} is left out (used everywhere, never redefined).
 * Uses are upward-exposed: a use preceded in its block by a definition of the
 * same variable pairs with that definition only, and such block-local pairs are
 * not DUAs (they are covered whenever the block runs).
 */
final class DefUse {

    static final class Dua {
        final int var;
        final int defBlock; // -1: method entry (parameter)
        final int defLine;
        final int useBlock;
        final int useLine;
        final int targetBlock; // -1 for a c-use
        final int targetLine;
        final String varName;

        Dua(final int var, final int defBlock, final int defLine, final int useBlock, final int useLine,
                final int targetBlock, final int targetLine, final String varName) {
            this.var = var;
            this.defBlock = defBlock;
            this.defLine = defLine;
            this.useBlock = useBlock;
            this.useLine = useLine;
            this.targetBlock = targetBlock;
            this.targetLine = targetLine;
            this.varName = varName;
        }
    }

    static final class Block {
        int start; // index of the first instruction node (a real instruction)
        int end; // index of the last real instruction, inclusive
        int line = -1;
        final List<Integer> normalSucc = new ArrayList<Integer>();
        final List<Integer> allSucc = new ArrayList<Integer>();
        /** var -> first use before any def in the block (upward exposed) */
        final Map<Integer, Integer> exposedUseLine = new HashMap<Integer, Integer>();
        /** var -> line of the last def in the block */
        final Map<Integer, Integer> lastDefLine = new HashMap<Integer, Integer>();
        // masks over DUA indices
        final BitSet cuse = new BitSet();
        final BitSet puseNode = new BitSet(); // p-uses whose use node is this block
        final BitSet edgeIn = new BitSet(); // p-uses whose edge ends at this block
        final BitSet gen = new BitSet();
        final BitSet kill = new BitSet();
    }

    final List<Block> blocks = new ArrayList<Block>();
    final List<Dua> duas = new ArrayList<Dua>();
    final BitSet entryGen = new BitSet(); // parameter definitions
    /** Instruction node index -> block, for the first real instruction of each block. */
    final Map<AbstractInsnNode, Block> blockAt = new HashMap<AbstractInsnNode, Block>();
    final List<AbstractInsnNode> firstInsn = new ArrayList<AbstractInsnNode>();

    private DefUse() {
    }

    /** Returns null for methods without code. */
    static DefUse of(final String owner, final MethodNode m, final Map<AbstractInsnNode, Integer> lineOf) {
        if (m.instructions.size() == 0) {
            return null;
        }
        final DefUse du = new DefUse();
        du.build(m, lineOf);
        return du;
    }

    private void build(final MethodNode m, final Map<AbstractInsnNode, Integer> lineOf) {
        final InsnList code = m.instructions;
        final AbstractInsnNode[] insns = code.toArray();
        final Map<LabelNode, Integer> labelIndex = new HashMap<LabelNode, Integer>();
        for (int i = 0; i < insns.length; i++) {
            if (insns[i] instanceof LabelNode) {
                labelIndex.put((LabelNode) insns[i], Integer.valueOf(i));
            }
        }
        // Leaders: first instruction, jump/switch targets, handler starts, instruction after a transfer.
        final boolean[] leader = new boolean[insns.length + 1];
        int first = nextReal(insns, 0);
        if (first < 0) {
            return;
        }
        leader[first] = true;
        for (int i = 0; i < insns.length; i++) {
            final AbstractInsnNode n = insns[i];
            for (final LabelNode l : targets(n)) {
                mark(leader, insns, labelIndex.get(l).intValue());
            }
            if (endsBlock(n)) {
                final int next = nextReal(insns, i + 1);
                if (next >= 0) {
                    leader[next] = true;
                }
            }
        }
        for (final Object o : m.tryCatchBlocks) {
            mark(leader, insns, labelIndex.get(((TryCatchBlockNode) o).handler).intValue());
        }
        // Blocks.
        final int[] blockOf = new int[insns.length];
        java.util.Arrays.fill(blockOf, -1);
        Block cur = null;
        for (int i = 0; i < insns.length; i++) {
            if (insns[i].getOpcode() < 0) {
                continue;
            }
            if (leader[i] || cur == null) {
                cur = new Block();
                cur.start = i;
                blocks.add(cur);
                blockAt.put(insns[i], cur);
                firstInsn.add(insns[i]);
            }
            cur.end = i;
            blockOf[i] = blocks.size() - 1;
            if (cur.line < 0 && lineOf.get(insns[i]) != null) {
                cur.line = lineOf.get(insns[i]).intValue();
            }
        }
        // Edges.
        for (int b = 0; b < blocks.size(); b++) {
            final Block blk = blocks.get(b);
            final AbstractInsnNode last = insns[blk.end];
            final List<Integer> succ = new ArrayList<Integer>();
            for (final LabelNode l : targets(last)) {
                addOnce(succ, blockOf[nextReal(insns, labelIndex.get(l).intValue())]);
            }
            if (!isUnconditionalTransfer(last) && b + 1 < blocks.size()) {
                addOnce(succ, b + 1);
            }
            blk.normalSucc.addAll(succ);
            blk.allSucc.addAll(succ);
        }
        for (final Object o : m.tryCatchBlocks) {
            final TryCatchBlockNode t = (TryCatchBlockNode) o;
            final int from = labelIndex.get(t.start).intValue();
            final int to = labelIndex.get(t.end).intValue();
            final int handler = blockOf[nextReal(insns, labelIndex.get(t.handler).intValue())];
            for (int i = from; i < to; i++) {
                if (blockOf[i] >= 0) {
                    addOnce(blocks.get(blockOf[i]).allSucc, handler);
                }
            }
        }
        // Defs and upward-exposed uses per block.
        final boolean isStatic = (m.access & Opcodes.ACC_STATIC) != 0;
        for (final Block blk : blocks) {
            for (int i = blk.start; i <= blk.end; i++) {
                final AbstractInsnNode n = insns[i];
                final Integer line = lineOf.get(n);
                final int l = line == null ? blk.line : line.intValue();
                final int use = useOf(n);
                if (use >= 0 && !(use == 0 && !isStatic) && !blk.lastDefLine.containsKey(Integer.valueOf(use))
                        && !blk.exposedUseLine.containsKey(Integer.valueOf(use))) {
                    blk.exposedUseLine.put(Integer.valueOf(use), Integer.valueOf(l));
                }
                final int def = defOf(n);
                if (def >= 0) {
                    blk.lastDefLine.put(Integer.valueOf(def), Integer.valueOf(l));
                }
            }
        }
        // Definitions: (block or -1 for entry, var). Reaching definitions over all edges.
        final List<int[]> defs = new ArrayList<int[]>(); // {block, var, line}
        final Map<Integer, BitSet> defsOfVar = new HashMap<Integer, BitSet>();
        final Type[] args = Type.getArgumentTypes(m.desc);
        int slot = isStatic ? 0 : 1;
        final int entryLine = blocks.isEmpty() ? -1 : blocks.get(0).line;
        for (final Type a : args) {
            addDef(defs, defsOfVar, -1, slot, entryLine);
            slot += a.getSize();
        }
        for (int b = 0; b < blocks.size(); b++) {
            for (final Map.Entry<Integer, Integer> e : blocks.get(b).lastDefLine.entrySet()) {
                if (!(e.getKey().intValue() == 0 && !isStatic)) {
                    addDef(defs, defsOfVar, b, e.getKey().intValue(), e.getValue().intValue());
                }
            }
        }
        final BitSet[] in = new BitSet[blocks.size()];
        final BitSet[] out = new BitSet[blocks.size()];
        for (int b = 0; b < blocks.size(); b++) {
            in[b] = new BitSet();
            out[b] = new BitSet();
        }
        final BitSet entry = new BitSet();
        for (int d = 0; d < defs.size(); d++) {
            if (defs.get(d)[0] == -1) {
                entry.set(d);
            }
        }
        boolean changed = true;
        while (changed) {
            changed = false;
            for (int b = 0; b < blocks.size(); b++) {
                final BitSet newIn = new BitSet();
                if (b == 0) {
                    newIn.or(entry);
                }
                for (int p = 0; p < blocks.size(); p++) {
                    if (blocks.get(p).allSucc.contains(Integer.valueOf(b))) {
                        newIn.or(out[p]);
                    }
                }
                final BitSet newOut = (BitSet) newIn.clone();
                for (final Integer v : blocks.get(b).lastDefLine.keySet()) {
                    final BitSet vd = defsOfVar.get(v);
                    if (vd != null) {
                        newOut.andNot(vd);
                    }
                }
                for (int d = 0; d < defs.size(); d++) {
                    if (defs.get(d)[0] == b) {
                        newOut.set(d);
                    }
                }
                if (!newIn.equals(in[b]) || !newOut.equals(out[b])) {
                    in[b] = newIn;
                    out[b] = newOut;
                    changed = true;
                }
            }
        }
        // DUAs.
        final Map<Long, Integer> duaOfDefUse = new HashMap<Long, Integer>();
        for (int b = 0; b < blocks.size(); b++) {
            final Block blk = blocks.get(b);
            for (final Map.Entry<Integer, Integer> u : blk.exposedUseLine.entrySet()) {
                final int var = u.getKey().intValue();
                final BitSet reaching = (BitSet) in[b].clone();
                final BitSet vd = defsOfVar.get(u.getKey());
                if (vd == null) {
                    continue;
                }
                reaching.and(vd);
                for (int d = reaching.nextSetBit(0); d >= 0; d = reaching.nextSetBit(d + 1)) {
                    final int[] def = defs.get(d);
                    final String name = varName(m, var, insns, blk.start);
                    if (blk.normalSucc.size() >= 2) {
                        for (final Integer k : blk.normalSucc) {
                            final Block t = blocks.get(k.intValue());
                            final int idx = duas.size();
                            duas.add(new Dua(var, def[0], def[2], b, u.getValue().intValue(), k.intValue(), t.line, name));
                            blk.puseNode.set(idx);
                            t.edgeIn.set(idx);
                            mapDef(duaOfDefUse, d, idx);
                        }
                    } else {
                        final int idx = duas.size();
                        duas.add(new Dua(var, def[0], def[2], b, u.getValue().intValue(), -1, -1, name));
                        blk.cuse.set(idx);
                        mapDef(duaOfDefUse, d, idx);
                    }
                }
            }
        }
        // gen/kill over DUAs: a block generates the DUAs of its own (last) defs and
        // kills every DUA of a variable it defines.
        for (int d = 0; d < defs.size(); d++) {
            final int[] def = defs.get(d);
            final BitSet ofDef = duasOfDef(duaOfDefUse, d);
            if (def[0] == -1) {
                entryGen.or(ofDef);
            } else {
                blocks.get(def[0]).gen.or(ofDef);
            }
        }
        for (final Block blk : blocks) {
            for (int idx = 0; idx < duas.size(); idx++) {
                if (blk.lastDefLine.containsKey(Integer.valueOf(duas.get(idx).var))) {
                    blk.kill.set(idx);
                }
            }
            final BitSet g = (BitSet) blk.gen.clone();
            blk.kill.andNot(g);
        }
    }

    private static void mapDef(final Map<Long, Integer> m, final int def, final int dua) {
        m.put(Long.valueOf(((long) def << 32) | dua), Integer.valueOf(dua));
    }

    private static BitSet duasOfDef(final Map<Long, Integer> m, final int def) {
        final BitSet b = new BitSet();
        for (final Long k : m.keySet()) {
            if ((int) (k.longValue() >>> 32) == def) {
                b.set((int) k.longValue());
            }
        }
        return b;
    }

    private static void addDef(final List<int[]> defs, final Map<Integer, BitSet> byVar, final int block,
            final int var, final int line) {
        BitSet b = byVar.get(Integer.valueOf(var));
        if (b == null) {
            b = new BitSet();
            byVar.put(Integer.valueOf(var), b);
        }
        b.set(defs.size());
        defs.add(new int[] { block, var, line });
    }

    private static String varName(final MethodNode m, final int var, final AbstractInsnNode[] insns, final int at) {
        if (m.localVariables != null) {
            for (final Object o : m.localVariables) {
                final LocalVariableNode lv = (LocalVariableNode) o;
                if (lv.index == var && index(insns, lv.start) <= at && at <= index(insns, lv.end)) {
                    return lv.name;
                }
            }
            for (final Object o : m.localVariables) {
                final LocalVariableNode lv = (LocalVariableNode) o;
                if (lv.index == var) {
                    return lv.name;
                }
            }
        }
        return "local" + var;
    }

    private static int index(final AbstractInsnNode[] insns, final LabelNode l) {
        for (int i = 0; i < insns.length; i++) {
            if (insns[i] == l) {
                return i;
            }
        }
        return -1;
    }

    static int useOf(final AbstractInsnNode n) {
        final int op = n.getOpcode();
        if (op >= Opcodes.ILOAD && op <= Opcodes.ALOAD || op == Opcodes.RET) {
            return ((VarInsnNode) n).var;
        }
        if (op == Opcodes.IINC) {
            return ((IincInsnNode) n).var;
        }
        return -1;
    }

    static int defOf(final AbstractInsnNode n) {
        final int op = n.getOpcode();
        if (op >= Opcodes.ISTORE && op <= Opcodes.ASTORE) {
            return ((VarInsnNode) n).var;
        }
        if (op == Opcodes.IINC) {
            return ((IincInsnNode) n).var;
        }
        return -1;
    }

    private static List<LabelNode> targets(final AbstractInsnNode n) {
        final List<LabelNode> t = new ArrayList<LabelNode>();
        if (n instanceof JumpInsnNode) {
            t.add(((JumpInsnNode) n).label);
        } else if (n instanceof TableSwitchInsnNode) {
            t.addAll(((TableSwitchInsnNode) n).labels);
            t.add(((TableSwitchInsnNode) n).dflt);
        } else if (n instanceof LookupSwitchInsnNode) {
            t.addAll(((LookupSwitchInsnNode) n).labels);
            t.add(((LookupSwitchInsnNode) n).dflt);
        }
        return t;
    }

    private static boolean endsBlock(final AbstractInsnNode n) {
        return n instanceof JumpInsnNode || n instanceof TableSwitchInsnNode || n instanceof LookupSwitchInsnNode
                || isUnconditionalTransfer(n);
    }

    private static boolean isUnconditionalTransfer(final AbstractInsnNode n) {
        final int op = n.getOpcode();
        return op == Opcodes.GOTO || op == Opcodes.ATHROW || op >= Opcodes.IRETURN && op <= Opcodes.RETURN
                || op == Opcodes.TABLESWITCH || op == Opcodes.LOOKUPSWITCH || op == Opcodes.RET
                || op == Opcodes.JSR;
    }

    private static void mark(final boolean[] leader, final AbstractInsnNode[] insns, final int from) {
        final int r = nextReal(insns, from);
        if (r >= 0) {
            leader[r] = true;
        }
    }

    private static int nextReal(final AbstractInsnNode[] insns, final int from) {
        for (int i = from; i < insns.length; i++) {
            if (insns[i].getOpcode() >= 0) {
                return i;
            }
        }
        return -1;
    }

    private static void addOnce(final List<Integer> l, final int v) {
        if (v >= 0 && !l.contains(Integer.valueOf(v))) {
            l.add(Integer.valueOf(v));
        }
    }
}
