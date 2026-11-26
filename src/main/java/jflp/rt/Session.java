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

    public static void warn(final String message) {
        final Output o = output;
        if (o != null) {
            o.warn(message);
        }
    }
}
