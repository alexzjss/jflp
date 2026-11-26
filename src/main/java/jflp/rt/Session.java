package jflp.rt;

import java.io.File;

import jflp.instr.ClassInfo;

/** Wiring between the agent, the probe runtime and the coverage file. */
public final class Session {

    private static Output output;

    private Session() {
    }

    public static synchronized void start(final File destDir) {
        if (output != null) {
            return;
        }
        output = new Output(destDir);
        T.init(output);
        java.lang.Runtime.getRuntime().addShutdownHook(new Thread("jflp-shutdown") {
            @Override
            public void run() {
                T.shutdown();
            }
        });
    }

    /** Allocates the probes of a newly loaded class and records its line map. */
    public static int define(final ClassInfo info) {
        final int base = R.allocate(info.probeCount());
        output.classDefined(base, info);
        return base;
    }

    /**
     * Allocates the probes of a class instrumented by JaCoCo ({@code lines=jacoco})
     * and returns the index of its probe array.
     */
    public static int defineJacoco(final String name, final long classId, final int probes) {
        final int base = R.allocate(probes);
        final int[] lines = new int[probes];
        final byte[] kinds = new byte[probes];
        java.util.Arrays.fill(lines, -1);
        java.util.Arrays.fill(kinds, jflp.data.RawFormat.KIND_JACOCO);
        output.classDefined(base, new ClassInfo(name, null, classId, lines, kinds));
        return R.register(base, probes);
    }

    public static void warn(final String message) {
        final Output o = output;
        if (o != null) {
            o.warn(message);
        }
    }
}
