package jflp.agent;

import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.security.ProtectionDomain;

import jflp.instr.Access;
import jflp.rt.R;

import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Opcodes;

/**
 * Adds the runtime's public static fields to a JDK class that is not loaded
 * yet, loads it, and points the fields at the runtime (see {@link Access}).
 */
final class Holder {

    /** Rarely used JDK classes; the first one not yet loaded becomes the holder. */
    private static final String[] CANDIDATES = {
        "java/lang/annotation/AnnotationTypeMismatchException",
        "java/lang/EnumConstantNotPresentException",
        "java/lang/TypeNotPresentException",
    };

    private Holder() {
    }

    /** Returns false when no candidate could be used; instrumentation then calls jflp.rt directly. */
    static boolean install(final Instrumentation inst) {
        for (final String name : CANDIDATES) {
            if (loaded(inst, name)) {
                continue;
            }
            final ClassFileTransformer t = new ClassFileTransformer() {
                public byte[] transform(final ClassLoader loader, final String className, final Class<?> redefined,
                        final ProtectionDomain pd, final byte[] buffer) {
                    return name.equals(className) && loader == null ? addFields(buffer) : null;
                }
            };
            inst.addTransformer(t);
            try {
                final Class<?> c = Class.forName(name.replace('/', '.'), true, null);
                R.attachHolder(c);
                Access.useHolder(name);
                return true;
            } catch (final Throwable e) {
                System.err.println("[jflp] cannot use " + name + " as runtime holder: " + e);
            } finally {
                inst.removeTransformer(t);
            }
        }
        return false;
    }

    private static boolean loaded(final Instrumentation inst, final String name) {
        final String dotted = name.replace('/', '.');
        for (final Class<?> c : inst.getAllLoadedClasses()) {
            if (c.getName().equals(dotted)) {
                return true;
            }
        }
        return false;
    }

    private static byte[] addFields(final byte[] buffer) {
        final ClassReader r = new ClassReader(buffer);
        final ClassWriter w = new ClassWriter(r, 0);
        r.accept(new ClassVisitor(Opcodes.ASM9, w) {
            @Override
            public void visitEnd() {
                final int pub = Opcodes.ACC_PUBLIC | Opcodes.ACC_STATIC | Opcodes.ACC_SYNTHETIC | Opcodes.ACC_TRANSIENT;
                cv.visitField(pub, Access.PAGES, "[[Z", null, null).visitEnd();
                cv.visitField(pub, Access.EVENTS, "Ljava/lang/Object;", null, null).visitEnd();
                // Volatile: a class initialised on another thread must see the latest array table.
                cv.visitField(pub | Opcodes.ACC_VOLATILE, Access.ARRAYS, "[[Z", null, null).visitEnd();
                super.visitEnd();
            }
        }, 0);
        return w.toByteArray();
    }
}
