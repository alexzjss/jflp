package jflp.ui;

import java.io.BufferedReader;
import java.io.File;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;

/** Runs external commands (git, mvn, defects4j...), streaming merged output line by line. */
final class Exec {

    /** A command that can be stopped from another thread (the run's Cancel button). */
    static final class Handle {
        private volatile Process process;
        private volatile boolean cancelled;

        void cancel() {
            cancelled = true;
            final Process p = process;
            if (p != null) {
                destroyTree(p);
            }
        }

        boolean cancelled() {
            return cancelled;
        }
    }

    private Exec() {
    }

    /**
     * Runs cmd in cwd with env overrides; each output line goes to sink. Returns the
     * exit status, or -1 when it timed out or was cancelled.
     */
    static int run(final List<String> cmd, final File cwd, final Map<String, String> env, final long timeoutSeconds,
            final Consumer<String> sink, final Handle handle) throws IOException, InterruptedException {
        final ProcessBuilder pb = new ProcessBuilder(cmd).directory(cwd).redirectErrorStream(true);
        if (env != null) {
            for (final Map.Entry<String, String> e : env.entrySet()) {
                if (e.getValue() == null) {
                    pb.environment().remove(e.getKey());
                } else {
                    pb.environment().put(e.getKey(), e.getValue());
                }
            }
        }
        if (handle != null && handle.cancelled()) {
            return -1;
        }
        final Process p;
        try {
            p = pb.start();
        } catch (final IOException e) {
            sink.accept("cannot start " + cmd.get(0) + ": " + e.getMessage());
            return 127;
        }
        if (handle != null) {
            handle.process = p;
        }
        final Thread reader = new Thread(() -> {
            try (BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                for (String line; (line = r.readLine()) != null;) {
                    sink.accept(line);
                }
            } catch (final IOException ignored) {
                // process went away
            }
        }, "exec-output");
        reader.setDaemon(true);
        reader.start();
        final boolean done = p.waitFor(timeoutSeconds, TimeUnit.SECONDS);
        if (!done) {
            sink.accept("[jflp] timed out after " + timeoutSeconds + " s; stopping " + cmd.get(0));
            destroyTree(p);
        }
        reader.join(5000);
        if (!done || handle != null && handle.cancelled()) {
            return -1;
        }
        return p.exitValue();
    }

    /** Output of a short command, or null when it cannot run. */
    static String output(final List<String> cmd, final File cwd, final Map<String, String> env, final long timeoutSeconds) {
        final StringBuilder b = new StringBuilder();
        try {
            final int code = run(cmd, cwd, env, timeoutSeconds, line -> b.append(line).append('\n'), null);
            return code == 0 ? b.toString() : null;
        } catch (final Exception e) {
            return null;
        }
    }

    private static void destroyTree(final Process p) {
        // Maven and Gradle fork test JVMs; stop the whole tree, children first.
        try {
            final Object handle = Process.class.getMethod("toHandle").invoke(p);
            final Object descendants = handle.getClass().getMethod("descendants").invoke(handle);
            ((java.util.stream.Stream<?>) descendants).forEach(h -> {
                try {
                    h.getClass().getMethod("destroyForcibly").invoke(h);
                } catch (final Exception ignored) {
                    // already gone
                }
            });
        } catch (final Exception java8) {
            // ProcessHandle is Java 9+; on Java 8 only the direct child is stopped
        }
        p.destroyForcibly();
    }

    static boolean windows() {
        return System.getProperty("os.name", "").toLowerCase().contains("win");
    }
}
