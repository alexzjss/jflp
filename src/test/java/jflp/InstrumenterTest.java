package jflp;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.lang.reflect.InvocationTargetException;
import java.util.Arrays;
import java.util.TreeSet;

import jflp.instr.ClassInfo;
import jflp.instr.Instrumenter;
import jflp.rt.Probes;

import org.junit.Test;

public class InstrumenterTest {

    @Test
    public void lineThatThrowsIsCovered() throws Exception {
        // JaCoCo (and so Jaguar2) reports line 8 as not covered when the division
        // throws; for fault localization that line is the prime suspect.
        final Loaded t = load("jflp.fixture.Target");
        final Object o = t.instance();
        Probes.reset();
        try {
            t.cls.getMethod("throwsOnLine8", int.class, int.class).invoke(o, 1, 0);
        } catch (final InvocationTargetException expected) {
            assertTrue(expected.getCause() instanceof ArithmeticException);
        }
        assertEquals(new TreeSet<Integer>(Arrays.asList(7, 8)), t.coveredLines());
    }

    @Test
    public void onlyExecutedBranchIsCovered() throws Exception {
        final Loaded t = load("jflp.fixture.Target");
        final Object o = t.instance();
        Probes.reset();
        t.cls.getMethod("branchy", int.class).invoke(o, 5);
        assertEquals(new TreeSet<Integer>(Arrays.asList(13, 14)), t.coveredLines());
        t.cls.getMethod("branchy", int.class).invoke(o, -5);
        assertEquals(new TreeSet<Integer>(Arrays.asList(13, 16)), t.coveredLines());
    }

    @Test
    public void finallyRunOnTheNormalPathIsCovered() throws Exception {
        // javac copies the finally block onto every exit; JaCoCo reports the
        // copies as one. Whichever copy runs, the finally line must count.
        final Loaded t = load("jflp.fixture.Cleanup");
        final Object o = t.instance();
        Probes.reset();
        t.cls.getMethod("run", boolean.class).invoke(o, false);
        assertEquals(new TreeSet<Integer>(Arrays.asList(10, 13, 15)), t.coveredLines());
        try {
            t.cls.getMethod("run", boolean.class).invoke(o, true);
        } catch (final InvocationTargetException expected) {
            assertTrue(expected.getCause() instanceof IllegalStateException);
        }
        assertEquals(new TreeSet<Integer>(Arrays.asList(10, 11, 15)), t.coveredLines());
    }

    @Test
    public void lineStartingWithNewOfObjectBuiltAcrossBranchesStillVerifies() throws Exception {
        // The stack map frames inside the ternary hold "uninitialized StringBuilder",
        // which points at the NEW instruction. A probe placed between that label and
        // NEW made the JVM reject the class (ClassFormatError; seen on classgraph).
        final Loaded t = load("jflp.fixture.Builder");
        final Object o = t.instance();
        Probes.reset();
        assertEquals("x", t.cls.getMethod("make", boolean.class).invoke(o, true).toString());
        assertEquals(new TreeSet<Integer>(Arrays.asList(7)), t.coveredLines());
    }

    @Test
    public void branchEdgesFollowExecution() throws Exception {
        final Loaded t = loadWithBranches("jflp.fixture.Branchy");
        final Object o = t.instance();
        final java.lang.reflect.Method both = t.cls.getMethod("both", boolean.class, boolean.class);
        Probes.reset();
        both.invoke(o, true, true);
        // `a && b` compiles to two IFEQs on line 7; both fall through to `return 1`.
        assertEquals(new TreeSet<String>(Arrays.asList("7 1:not-taken", "7 2:not-taken")), t.coveredBranches());
        both.invoke(o, true, false);
        assertEquals(new TreeSet<String>(Arrays.asList("7 1:not-taken", "7 2:taken")), t.coveredBranches());
        both.invoke(o, false, true);
        assertEquals(new TreeSet<String>(Arrays.asList("7 1:taken")), t.coveredBranches());

        final java.lang.reflect.Method pick = t.cls.getMethod("pick", int.class);
        pick.invoke(o, 3);
        // Distinct targets in case order, then default: case 1, cases 2-3, default.
        assertEquals(new TreeSet<String>(Arrays.asList("14 1:target-2")), t.coveredBranches());
        pick.invoke(o, 9);
        assertEquals(new TreeSet<String>(Arrays.asList("14 1:target-3")), t.coveredBranches());

        assertEquals(Long.valueOf(4L), t.cls.getMethod("wide", long.class, double.class, Object.class)
                .invoke(o, 4L, 2.0, null));
        // javac compiles `o == null ? x : y` to IFNONNULL (jump to y): with o null it falls through.
        assertEquals(new TreeSet<String>(Arrays.asList("26 1:not-taken")), t.coveredBranches());
    }

    @Test
    public void defUseAssociationsFollowExecution() throws Exception {
        // DUAs of f: x (parameter) has p-uses on both edges of `if (x > 0)` (line 8) and
        // a c-use at line 9; y has c-uses at line 11 from its definitions at 7 and 9.
        // Labels: "var def->use" or, for a p-use, "var def->use->target".
        final Loaded t = loadWith("jflp.fixture.Flow", false, true);
        assertEquals(new TreeSet<String>(Arrays.asList("x 7->8->9", "x 7->8->11", "x 7->9", "y 7->11", "y 9->11")),
                t.allDuas());
        final Object o = t.instance();
        final java.lang.reflect.Method f = t.cls.getMethod("f", int.class);
        Probes.reset();
        f.invoke(o, 5);
        assertEquals(new TreeSet<String>(Arrays.asList("x 7->8->9", "x 7->9", "y 9->11")), t.coveredDuas());
        f.invoke(o, -1);
        assertEquals(new TreeSet<String>(Arrays.asList("x 7->8->11", "y 7->11")), t.coveredDuas());
    }

    @Test
    public void lineSetMatchesJaCoCo() throws Exception {
        // Same code lines JaCoCo reports for these classes; a different line set
        // would shift every column of the coverage matrix against Jaguar2's.
        assertArrayEquals(new int[] { 7, 8, 9, 13, 14, 16, 20, 21, 24, 25 },
                sortedLines("jflp.fixture.Target"));
        // Enum: values()/valueOf() and the empty constructor are generated; the
        // constants (static init) and code() remain.
        assertArrayEquals(new int[] { 3, 4, 7 }, sortedLines("jflp.fixture.Kind"));
        // Private empty no-arg constructor (utility class idiom) is filtered.
        assertArrayEquals(new int[] { 9 }, sortedLines("jflp.fixture.Holder"));
    }

    @Test
    public void syntheticClassesAreNotMeasured() throws Exception {
        final byte[] b = bytes("jflp.fixture.Target");
        // Flip ACC_SYNTHETIC in the class access flags (right after the constant pool).
        final org.objectweb.asm.ClassReader r = new org.objectweb.asm.ClassReader(b);
        final int at = r.header;
        b[at] = (byte) (b[at] | 0x10);
        assertNull(Instrumenter.analyze(b));
    }

    // ---- helpers ----

    static final class Loaded {
        final Class<?> cls;
        final int base;
        final ClassInfo info;

        Loaded(final Class<?> cls, final int base, final ClassInfo info) {
            this.cls = cls;
            this.base = base;
            this.info = info;
        }

        Object instance() throws Exception {
            return cls.getConstructor().newInstance();
        }

        TreeSet<Integer> coveredLines() {
            final TreeSet<Integer> lines = new TreeSet<Integer>();
            for (final int id : Probes.collect()) {
                if (id >= base && id < base + info.probeCount()
                        && info.probeKinds[id - base] == jflp.data.RawFormat.KIND_LINE) {
                    lines.add(info.probeLines[id - base]);
                }
            }
            return lines;
        }

        TreeSet<String> allDuas() {
            final TreeSet<String> out = new TreeSet<String>();
            for (int i = 0; i < info.probeCount(); i++) {
                if (info.probeKinds[i] == jflp.data.RawFormat.KIND_DUA) {
                    out.add(duaLabel(i));
                }
            }
            return out;
        }

        /** Covered DUAs, and resets the probes. */
        TreeSet<String> coveredDuas() {
            final TreeSet<String> out = new TreeSet<String>();
            for (final int id : Probes.collect()) {
                if (id >= base && id < base + info.probeCount()
                        && info.probeKinds[id - base] == jflp.data.RawFormat.KIND_DUA) {
                    out.add(duaLabel(id - base));
                }
            }
            return out;
        }

        private String duaLabel(final int i) {
            // parameters are defined at the method's first line (7 here)
            final Object[] d = info.probeDuas[i];
            final int target = ((Integer) d[3]).intValue();
            return d[1] + " " + d[2] + "->" + info.probeLines[i] + (target < 0 ? "" : "->" + target);
        }

        /** Covered branch edges as "line jump:edge", and resets the probes. */
        TreeSet<String> coveredBranches() {
            final TreeSet<String> out = new TreeSet<String>();
            for (final int id : Probes.collect()) {
                if (id >= base && id < base + info.probeCount()
                        && info.probeKinds[id - base] == jflp.data.RawFormat.KIND_BRANCH) {
                    final int arg = info.probeArgs[id - base];
                    final int edge = arg & 0xFFFF;
                    out.add(info.probeLines[id - base] + " " + (arg >>> 16) + ":"
                            + (edge == 0 ? "taken" : edge == 1 ? "not-taken" : "target-" + (edge - 1)));
                }
            }
            return out;
        }
    }

    static Loaded load(final String name) throws Exception {
        return load(name, false);
    }

    static Loaded loadWithBranches(final String name) throws Exception {
        return load(name, true);
    }

    static Loaded load(final String name, final boolean branches) throws Exception {
        return loadWith(name, branches, false);
    }

    static Loaded loadWith(final String name, final boolean branches, final boolean duas) throws Exception {
        final Instrumenter.Analysis a = Instrumenter.analyze(bytes(name), branches, duas);
        final int base = Probes.allocate(a.info.probeCount());
        final byte[] instrumented = Instrumenter.instrument(a, base);
        final ClassLoader loader = new ClassLoader(InstrumenterTest.class.getClassLoader()) {
            @Override
            protected Class<?> loadClass(final String n, final boolean resolve) throws ClassNotFoundException {
                if (n.equals(name)) {
                    synchronized (this) {
                        Class<?> c = findLoadedClass(n);
                        if (c == null) {
                            c = defineClass(n, instrumented, 0, instrumented.length);
                        }
                        return c;
                    }
                }
                return super.loadClass(n, resolve);
            }
        };
        return new Loaded(loader.loadClass(name), base, a.info);
    }

    static int[] sortedLines(final String name) throws IOException {
        final int[] l = Instrumenter.analyze(bytes(name)).info.probeLines.clone();
        Arrays.sort(l);
        return l;
    }

    static byte[] bytes(final String name) throws IOException {
        final InputStream in = InstrumenterTest.class.getResourceAsStream("/" + name.replace('.', '/') + ".class");
        final ByteArrayOutputStream out = new ByteArrayOutputStream();
        final byte[] buf = new byte[4096];
        for (int n; (n = in.read(buf)) > 0;) {
            out.write(buf, 0, n);
        }
        in.close();
        return out.toByteArray();
    }
}
