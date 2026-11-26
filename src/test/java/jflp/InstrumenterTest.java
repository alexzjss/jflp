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
                if (id >= base && id < base + info.probeCount()) {
                    lines.add(info.probeLines[id - base]);
                }
            }
            return lines;
        }
    }

    static Loaded load(final String name) throws Exception {
        final Instrumenter.Analysis a = Instrumenter.analyze(bytes(name));
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
