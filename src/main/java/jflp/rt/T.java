package jflp.rt;

import java.lang.reflect.Method;

/**
 * Test lifecycle events, called from hooked test framework classes.
 *
 * Semantics follow Jaguar2 so spectra stay comparable: coverage is reset when
 * a test starts (code run by class-level setup is not attributed to any
 * test), a test fails if any failure or error is reported for it, and a test
 * whose assumption fails is not counted at all.
 *
 * Probes are global to the JVM, so two tests running at the same time cannot
 * be told apart. That case is reported as a warning in the data instead of
 * silently mixing their coverage.
 */
public final class T {

    private static Output output;

    private static String active;
    private static boolean failed;
    private static boolean skipped;

    private T() {
    }

    static synchronized void init(final Output out) {
        output = out;
    }

    public static void j4Started(final Object description) {
        started(name(description));
    }

    public static void j4Finished(final Object description) {
        finished(name(description));
    }

    public static void j4Failure(final Object failure) {
        failed(name(call(failure, "getDescription")));
    }

    public static void j4Assumption(final Object failure) {
        assumption(name(call(failure, "getDescription")));
    }

    public static void j3Started(final Object test) {
        started(String.valueOf(test));
    }

    public static void j3Finished(final Object test) {
        finished(String.valueOf(test));
    }

    public static void j3Failure(final Object test) {
        failed(String.valueOf(test));
    }

    static synchronized void started(final String name) {
        if (output == null || name == null) {
            return;
        }
        if (active == null) {
            R.reset();
            active = name;
            failed = false;
            skipped = false;
        } else if (!active.equals(name)) {
            output.warn("overlap: test '" + name + "' started while '" + active
                    + "' was running; their coverage is mixed (parallel test execution?)");
        }
    }

    static synchronized void failed(final String name) {
        if (output == null || name == null) {
            return;
        }
        if (name.equals(active)) {
            failed = true;
        } else {
            output.warn("failure outside a running test: '" + name + "' (not counted, as in Jaguar2)");
        }
    }

    static synchronized void assumption(final String name) {
        if (name != null && name.equals(active)) {
            skipped = true;
        }
    }

    static synchronized void finished(final String name) {
        if (output == null || name == null || !name.equals(active)) {
            return;
        }
        final int[] hits = R.collectAndReset();
        if (!skipped) {
            output.test(failed, name, hits);
        }
        active = null;
    }

    static synchronized void shutdown() {
        if (output == null) {
            return;
        }
        if (active != null) {
            output.warn("JVM exited while test '" + active + "' was running; that test is not counted");
        }
        output.close();
        output = null;
    }

    private static String name(final Object description) {
        final Object n = call(description, "getDisplayName");
        return n == null ? null : n.toString();
    }

    private static Object call(final Object target, final String method) {
        if (target == null) {
            return null;
        }
        try {
            final Method m = target.getClass().getMethod(method);
            return m.invoke(target);
        } catch (final Throwable e) {
            final Output o = output;
            if (o != null) {
                o.warn("cannot call " + target.getClass().getName() + "." + method + ": " + e);
            }
            return null;
        }
    }
}
