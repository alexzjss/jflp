package jflp.instr;

import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.tree.FieldInsnNode;
import org.objectweb.asm.tree.InsnList;
import org.objectweb.asm.tree.InsnNode;
import org.objectweb.asm.tree.LdcInsnNode;
import org.objectweb.asm.tree.MethodInsnNode;
import org.objectweb.asm.tree.TypeInsnNode;
import org.objectweb.asm.tree.VarInsnNode;

/**
 * How instrumented code reaches the runtime.
 *
 * Some class loaders only delegate {@code java.*} to the boot class path
 * (Ant's isolated AntClassLoader, OSGi, application servers), so a class they
 * define cannot see {@code jflp.rt}. The agent therefore adds public static
 * fields to a rarely used JDK class (the holder) and instrumented code goes
 * through them, as JaCoCo does: every loader sees {@code java.lang}. A probe
 * then is a plain array store, without a call.
 *
 * Without a holder (unit tests, which run without the agent), instrumented code
 * calls {@code jflp.rt.R} and {@code jflp.rt.T} directly.
 */
public final class Access {

    public static final String PAGES = "$jflpPages";
    public static final String ARRAYS = "$jflpArrays";
    public static final String EVENTS = "$jflpEvents";

    private static volatile String holder;

    private Access() {
    }

    /** Switches instrumentation to the holder class {@code internalName}. */
    public static void useHolder(final String internalName) {
        holder = internalName;
    }

    /** Code that marks global probe {@code id}; returns the stack it needs. */
    static int probe(final InsnList out, final int id) {
        final String h = holder;
        if (h == null) {
            out.add(Instrumenter.push(id));
            out.add(new MethodInsnNode(Opcodes.INVOKESTATIC, Instrumenter.RUNTIME, "h", "(I)V", false));
            return 1;
        }
        out.add(new FieldInsnNode(Opcodes.GETSTATIC, h, PAGES, "[[Z"));
        out.add(Instrumenter.push(id >>> 16));
        out.add(new InsnNode(Opcodes.AALOAD));
        out.add(Instrumenter.push(id & 0xFFFF));
        out.add(new InsnNode(Opcodes.ICONST_1));
        out.add(new InsnNode(Opcodes.BASTORE));
        return 3;
    }

    /** Code that reports test event {@code event} with local variable {@code var}; returns the stack it needs. */
    static int event(final InsnList out, final String event, final int var) {
        final String h = holder;
        if (h == null) {
            out.add(new VarInsnNode(Opcodes.ALOAD, var));
            out.add(new MethodInsnNode(Opcodes.INVOKESTATIC, "jflp/rt/T", event, "(Ljava/lang/Object;)V", false));
            return 1;
        }
        // $jflpEvents.equals(new Object[] { event, arg }) — Object.equals is the only
        // call every loader can link; the runtime's object dispatches on it.
        out.add(new FieldInsnNode(Opcodes.GETSTATIC, h, EVENTS, "Ljava/lang/Object;"));
        out.add(new InsnNode(Opcodes.ICONST_2));
        out.add(new TypeInsnNode(Opcodes.ANEWARRAY, "java/lang/Object"));
        out.add(new InsnNode(Opcodes.DUP));
        out.add(new InsnNode(Opcodes.ICONST_0));
        out.add(new LdcInsnNode(event));
        out.add(new InsnNode(Opcodes.AASTORE));
        out.add(new InsnNode(Opcodes.DUP));
        out.add(new InsnNode(Opcodes.ICONST_1));
        out.add(new VarInsnNode(Opcodes.ALOAD, var));
        out.add(new InsnNode(Opcodes.AASTORE));
        out.add(new MethodInsnNode(Opcodes.INVOKEVIRTUAL, "java/lang/Object", "equals", "(Ljava/lang/Object;)Z",
                false));
        out.add(new InsnNode(Opcodes.POP));
        return 5;
    }

    /** Pushes JaCoCo probe array {@code index} ({@code lines=jacoco}); returns the stack it needs. */
    static int jacocoArray(final MethodVisitor mv, final int index) {
        final String h = holder;
        if (h == null) {
            mv.visitLdcInsn(Integer.valueOf(index));
            mv.visitMethodInsn(Opcodes.INVOKESTATIC, Instrumenter.RUNTIME, "a", "(I)[Z", false);
            return 1;
        }
        mv.visitFieldInsn(Opcodes.GETSTATIC, h, ARRAYS, "[[Z");
        mv.visitLdcInsn(Integer.valueOf(index));
        mv.visitInsn(Opcodes.AALOAD);
        return 2;
    }
}
