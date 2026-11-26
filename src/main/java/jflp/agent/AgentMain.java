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
import jflp.rt.Session;

/**
 * Agent proper, loaded from the boot class path.
 *
 * Options ({@code key=value}, comma separated):
 * <ul>
 * <li>{@code destdir} — where the coverage file goes (default {@code target/jflp});</li>
 * <li>{@code classdirs} — directories whose classes are measured, separated by
 * the platform path separator (default {@code target/classes}, as Jaguar2).</li>
 * </ul>
 * Relative paths are resolved against the JVM's working directory.
 */
public final class AgentMain implements ClassFileTransformer {

    private final File[] classDirs;
    private final Map<String, Integer> bases = new HashMap<String, Integer>();

    private AgentMain(final File[] classDirs) {
        this.classDirs = classDirs;
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
        Session.start(dest);
        inst.addTransformer(new AgentMain(dirs.toArray(new File[dirs.size()])));
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
            return instrument(name, buffer);
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
        return Instrumenter.instrument(a, base.intValue());
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
            if (!"destdir".equals(k) && !"classdirs".equals(k)) {
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
