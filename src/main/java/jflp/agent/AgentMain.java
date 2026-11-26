package jflp.agent;

import java.io.File;
import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.security.ProtectionDomain;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import jflp.instr.Hooks;
import jflp.instr.Instrumenter;
import jflp.instr.JacocoMode;
import jflp.rt.Session;

/**
 * Agent proper, loaded from the boot class path.
 *
 * Options ({@code key=value}, comma separated):
 * <ul>
 * <li>{@code destdir} — where the coverage file goes (default {@code target/jflp});</li>
 * <li>{@code classdirs} — directories whose classes are measured, separated by
 * the platform path separator (default {@code target/classes}, as Jaguar2);</li>
 * <li>{@code lines} — {@code entry} (default): a line is covered once execution
 * reaches it; {@code jacoco}: as JaCoCo and Jaguar2 count it, which leaves the
 * line that throws an exception uncovered.</li>
 * </ul>
 * Relative paths are resolved against the JVM's working directory.
 */
public final class AgentMain implements ClassFileTransformer {

    private final File[] classDirs;
    private final Map<String, Integer> bases = new HashMap<String, Integer>();
    private final JacocoMode jacoco;

    private AgentMain(final File[] classDirs, final JacocoMode jacoco) {
        this.classDirs = classDirs;
        this.jacoco = jacoco;
    }

    public static void start(final String args, final Instrumentation inst) throws Exception {
        final Map<String, String> opts = parse(args);
        final File dest = absolute(value(opts, "destdir", "target" + File.separator + "jflp"));
        final List<File> dirs = new ArrayList<File>();
        for (final String d : value(opts, "classdirs", "target" + File.separator + "classes")
                .split(File.pathSeparator)) {
            if (d.length() > 0) {
                dirs.add(absolute(d));
            }
        }
        final String lines = value(opts, "lines", "entry");
        if (!"entry".equals(lines) && !"jacoco".equals(lines)) {
            throw new IllegalArgumentException("[jflp] lines must be entry or jacoco, not '" + lines + "'");
        }
        Session.start(dest);
        if (!Holder.install(inst)) {
            System.err.println("[jflp] WARNING: no runtime holder; classes defined by isolating class loaders"
                    + " (Ant, OSGi) will fail with NoClassDefFoundError jflp/rt/R");
        }
        inst.addTransformer(new AgentMain(dirs.toArray(new File[dirs.size()]),
                "jacoco".equals(lines) ? new JacocoMode() : null));
    }

    public byte[] transform(final ClassLoader loader, final String name, final Class<?> redefined,
            final ProtectionDomain pd, final byte[] buffer) {
        if (name == null || redefined != null) {
            return null;
        }
        try {
            final byte[] hooked = Hooks.hook(name, buffer);
            if (hooked != null) {
                return hooked;
            }
            if (name.startsWith("jflp/") || !measured(name)) {
                return null;
            }
            return jacoco != null ? jacoco.instrument(buffer, name) : instrument(name, buffer);
        } catch (final Throwable e) {
            Session.warn("class " + name.replace('/', '.') + " not instrumented: " + e);
            return null;
        }
    }

    private byte[] instrument(final String name, final byte[] buffer) {
        final Instrumenter.Analysis a = Instrumenter.analyze(buffer);
        if (a == null) {
            return null;
        }
        final String key = name + '#' + a.info.crc;
        Integer base;
        synchronized (bases) {
            base = bases.get(key);
            if (base == null) {
                // Same bytes loaded again by another class loader share probes.
                base = Session.define(a.info);
                bases.put(key, base);
            }
        }
        final byte[] out = Instrumenter.instrument(a, base.intValue());
        for (final String m : a.grownHuge) {
            hugeWarning(m);
        }
        return out;
    }

    private boolean hugeWarned;

    /**
     * A method the probes push over HotSpot's JIT limit runs interpreted, often
     * several times slower (Defects4J Math: BOBYQAOptimizer, 9 s -> 93 s).
     */
    private synchronized void hugeWarning(final String method) {
        Session.warn("method " + method + " exceeds " + Instrumenter.HUGE_METHOD_LIMIT
                + " bytes of bytecode once instrumented and will not be JIT-compiled;"
                + " add -XX:-DontCompileHugeMethods to the test JVM if tests are slow");
        if (!hugeWarned) {
            hugeWarned = true;
            System.err.println("[jflp] WARNING: instrumentation pushed " + method + " (and maybe others, see"
                    + " jflp-summary.txt) over the JIT size limit; -XX:-DontCompileHugeMethods avoids the slowdown");
        }
    }

    private boolean measured(final String name) {
        for (final File d : classDirs) {
            if (new File(d, name + ".class").isFile()) {
                return true;
            }
        }
        return false;
    }

    private static Map<String, String> parse(final String args) {
        final Map<String, String> m = new HashMap<String, String>();
        if (args == null) {
            return m;
        }
        for (final String kv : args.split(",")) {
            final int eq = kv.indexOf('=');
            if (eq > 0) {
                m.put(kv.substring(0, eq).trim(), kv.substring(eq + 1).trim());
            } else if (kv.trim().length() > 0) {
                throw new IllegalArgumentException("[jflp] bad agent option '" + kv + "'; expected key=value");
            }
        }
        for (final String k : m.keySet()) {
            if (!"destdir".equals(k) && !"classdirs".equals(k) && !"lines".equals(k)) {
                throw new IllegalArgumentException("[jflp] unknown agent option '" + k + "'");
            }
        }
        return m;
    }

    private static String value(final Map<String, String> m, final String k, final String dflt) {
        final String v = m.get(k);
        return v == null || v.length() == 0 ? dflt : v;
    }

    private static File absolute(final String path) {
        final File f = new File(path);
        return f.isAbsolute() ? f : new File(System.getProperty("user.dir"), path);
    }
}
