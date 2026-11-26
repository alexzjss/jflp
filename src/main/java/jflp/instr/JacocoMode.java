package jflp.instr;

import java.io.IOException;
import java.util.HashMap;
import java.util.Map;

import jflp.rt.Session;

import org.jacoco.core.runtime.IExecutionDataAccessorGenerator;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;

/**
 * {@code lines=jacoco}: classes are instrumented by JaCoCo 0.8.7 itself, so a
 * line is covered exactly when Jaguar2 says it is: only once JaCoCo sees its
 * instructions complete, which leaves the line that throws uncovered. Use it to
 * reproduce Jaguar2's spectra; the default mode is more precise.
 */
public final class JacocoMode implements IExecutionDataAccessorGenerator {

    private final Map<Long, Integer> arrays = new HashMap<Long, Integer>();
    private final org.jacoco.core.instr.Instrumenter instrumenter = new org.jacoco.core.instr.Instrumenter(this);

    public byte[] instrument(final byte[] buffer, final String name) throws IOException {
        return instrumenter.instrument(buffer, name);
    }

    /** Emits the code that fetches the class's probe array, as {@code $jacocoInit} needs. */
    public int generateDataAccessor(final long classId, final String className, final int probeCount,
            final MethodVisitor mv) {
        Integer index;
        synchronized (arrays) {
            index = arrays.get(Long.valueOf(classId));
            if (index == null) {
                // Same bytes loaded again by another class loader share probes.
                index = Integer.valueOf(Session.defineJacoco(className, classId, probeCount));
                arrays.put(Long.valueOf(classId), index);
            }
        }
        mv.visitLdcInsn(index);
        mv.visitMethodInsn(Opcodes.INVOKESTATIC, Instrumenter.RUNTIME, "a", "(I)[Z", false);
        return 1;
    }
}
