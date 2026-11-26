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

    /** JUnit Platform: {@code executionStarted(TestDescriptor)}. Containers are ignored. */
    public static void j5Started(final Object descriptor) {
        if (Boolean.TRUE.equals(call(descriptor, "isTest"))) {
            started(platformName(descriptor));
        }
    }

    /** JUnit Platform: {@code executionFinished(TestDescriptor, TestExecutionResult)}, as {@code Object[]}. */
    public static void j5Finished(final Object descriptorAndResult) {
        final Object[] a = (Object[]) descriptorAndResult;
        if (!Boolean.TRUE.equals(call(a[0], "isTest"))) {
            return;
        }
        final Object status = call(a[1], "getStatus");
        platformFinished(platformName(a[0]), status == null ? "" : status.toString());
    }

    /**
     * Outcome and end in one step: the platform's listeners wrap one another, so the
     * same executionFinished arrives several times; only the first, while the test is
     * still active, counts.
     */
    private static synchronized void platformFinished(final String name, final String status) {
        if (name == null || !name.equals(active)) {
            return;
        }
        if ("FAILED".equals(status)) {
            failed = true;
        } else if ("ABORTED".equals(status)) {
            skipped = true; // a failed assumption, as in JUnit 4
        }
        finished(name);
    }

    /**
     * {@code method(class)} like JUnit 4's display name, so that a JUnit 4 test run
     * by the vintage engine, reported both by RunNotifier and by the platform, is
     * recognised as one test. Parameterized and repeated invocations share their
     * method and get the invocation's display name appended: {@code method[[1] 5](class)}.
     */
    static String platformName(final Object descriptor) {
        final Object source = call(call(descriptor, "getSource"), "orElse", null);
        final Object cls = call(source, "getClassName");
        final Object method = call(source, "getMethodName");
        if (cls == null || method == null) {
            final Object id = call(descriptor, "getUniqueId");
            return id == null ? null : id.toString();
        }
        final Object display = call(descriptor, "getDisplayName");
        final String d = display == null ? "" : display.toString();
        final boolean plain = d.equals(method) || d.equals(method + "()") || d.startsWith(method + "(");
        return method + (plain ? "" : "[" + d + "]") + "(" + cls + ")";
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

    /** {@code target.method(arg)} for a one-argument method found by name (Optional.orElse). */
    private static Object call(final Object target, final String method, final Object arg) {
        if (target == null) {
            return null;
        }
        try {
            for (final Method m : target.getClass().getMethods()) {
                if (m.getName().equals(method) && m.getParameterTypes().length == 1) {
                    m.setAccessible(true);
                    return m.invoke(target, arg);
                }
            }
            return null;
        } catch (final Throwable e) {
            final Output o = output;
            if (o != null) {
                o.warn("cannot call " + target.getClass().getName() + "." + method + ": " + e);
            }
            return null;
        }
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
            m.setAccessible(true); // public method of a non-public class (JUnit's descriptors)
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
