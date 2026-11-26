package jflp.instr;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;

import org.objectweb.asm.Opcodes;
import org.objectweb.asm.tree.AbstractInsnNode;
import org.objectweb.asm.tree.ClassNode;
import org.objectweb.asm.tree.MethodInsnNode;
import org.objectweb.asm.tree.MethodNode;
import org.objectweb.asm.tree.VarInsnNode;

/**
 * Compiler-generated methods that have no source counterpart. They are left
 * out so that the line set matches what JaCoCo, and therefore Jaguar2, reports:
 * a line made only of generated code is not a line the developer wrote.
 */
final class Filters {

    private Filters() {
    }

    static boolean ignored(final ClassNode c, final MethodNode m) {
        if ((m.access & Opcodes.ACC_SYNTHETIC) != 0 && !m.name.startsWith("lambda$")) {
            return true; // bridges, accessors, $deserializeLambda$, other tools' probes
        }
        if ((c.access & Opcodes.ACC_ENUM) != 0 && "java/lang/Enum".equals(c.superName)) {
            if ("values".equals(m.name) && m.desc.equals("()[L" + c.name + ";")) {
                return true;
            }
            if ("valueOf".equals(m.name) && m.desc.equals("(Ljava/lang/String;)L" + c.name + ";")) {
                return true;
            }
            if ("<init>".equals(m.name) && m.desc.startsWith("(Ljava/lang/String;I")
                    && isTrivialConstructor(m, "java/lang/Enum", "(Ljava/lang/String;I)V")) {
                return true;
            }
        }
        if ("<init>".equals(m.name) && "()V".equals(m.desc) && (m.access & Opcodes.ACC_PRIVATE) != 0
                && isTrivialConstructor(m, c.superName, "()V")) {
            return true;
        }
        return false;
    }

    /** Body is exactly: load this and the forwarded arguments, call super, return. */
    private static boolean isTrivialConstructor(final MethodNode m, final String superName,
            final String superDesc) {
        final List<AbstractInsnNode> code = new ArrayList<AbstractInsnNode>();
        for (Iterator<AbstractInsnNode> it = m.instructions.iterator(); it.hasNext();) {
            final AbstractInsnNode insn = it.next();
            if (insn.getOpcode() >= 0) {
                code.add(insn);
            }
        }
        if (code.size() < 3 || !Instrumenter.isAload0(code.get(0))
                || code.get(code.size() - 1).getOpcode() != Opcodes.RETURN) {
            return false;
        }
        final AbstractInsnNode call = code.get(code.size() - 2);
        if (call.getOpcode() != Opcodes.INVOKESPECIAL) {
            return false;
        }
        final MethodInsnNode mi = (MethodInsnNode) call;
        if (!"<init>".equals(mi.name) || !mi.owner.equals(superName) || !mi.desc.equals(superDesc)) {
            return false;
        }
        for (int i = 1; i < code.size() - 2; i++) {
            if (!(code.get(i) instanceof VarInsnNode)) {
                return false;
            }
        }
        return true;
    }
}
