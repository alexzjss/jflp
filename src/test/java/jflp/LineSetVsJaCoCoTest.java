package jflp;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.Enumeration;
import java.util.List;
import java.util.TreeSet;
import java.util.jar.JarEntry;
import java.util.jar.JarFile;

import jflp.instr.Instrumenter;

import org.jacoco.core.analysis.Analyzer;
import org.jacoco.core.analysis.CoverageBuilder;
import org.jacoco.core.analysis.IClassCoverage;
import org.jacoco.core.analysis.ICounter;
import org.jacoco.core.data.ExecutionDataStore;
import org.junit.Test;

/**
 * The coverage matrix and the CSV/XML only line up with Jaguar2's when both
 * tools agree on which lines hold code. JaCoCo 0.8.7 (the fork's version) is
 * the reference; real third-party jars are the corpus.
 */
public class LineSetVsJaCoCoTest {

    @Test
    public void fixturesMatchExactly() throws Exception {
        final List<String> diffs = compare(fixtures());
        assertTrue(diffs.toString(), diffs.isEmpty());
    }

    /**
     * Jars on the test class path by default; {@code -Djflp.corpus=a.jar:b.jar}
     * points it at any other code base (a JDK's rt.jar is a good stress test).
     */
    @Test
    public void thirdPartyJarsMatch() throws Exception {
        final List<byte[]> corpus = new ArrayList<byte[]>();
        final String explicit = System.getProperty("jflp.corpus");
        for (final String jar : (explicit != null ? explicit : System.getProperty("java.class.path"))
                .split(File.pathSeparator)) {
            if (explicit != null || jar.matches(".*(junit-4|asm-9|asm-tree-9|asm-commons-9|hamcrest-core).*\\.jar")) {
                corpus.addAll(classesOf(new File(jar)));
            }
        }
        assertTrue("corpus not found on the class path", corpus.size() > 300);
        final List<String> diffs = compare(corpus);
        assertEquals(diffs.size() + " of " + corpus.size() + " classes differ, e.g. "
                + diffs.subList(0, Math.min(5, diffs.size())), 0, diffs.size());
    }

    private static List<String> compare(final List<byte[]> classes) throws IOException {
        final List<String> diffs = new ArrayList<String>();
        for (final byte[] b : classes) {
            final CoverageBuilder builder = new CoverageBuilder();
            new Analyzer(new ExecutionDataStore(), builder).analyzeClass(b, "x");
            final Instrumenter.Analysis a = Instrumenter.analyze(b);
            if (builder.getClasses().isEmpty()) {
                if (a != null) {
                    diffs.add(a.info.name + ": JaCoCo skips the class");
                }
                continue;
            }
            final IClassCoverage cc = builder.getClasses().iterator().next();
            final TreeSet<Integer> expected = new TreeSet<Integer>();
            for (int l = cc.getFirstLine(); l >= 0 && l <= cc.getLastLine(); l++) {
                if (cc.getLine(l).getStatus() != ICounter.EMPTY) {
                    expected.add(l);
                }
            }
            final TreeSet<Integer> actual = new TreeSet<Integer>();
            if (a != null) {
                for (final int l : a.info.probeLines) {
                    actual.add(l);
                }
            }
            if (!expected.equals(actual)) {
                final TreeSet<Integer> missing = new TreeSet<Integer>(expected);
                missing.removeAll(actual);
                final TreeSet<Integer> extra = new TreeSet<Integer>(actual);
                extra.removeAll(expected);
                diffs.add(cc.getName() + " missing=" + missing + " extra=" + extra);
            }
        }
        return diffs;
    }

    private static List<byte[]> fixtures() throws IOException {
        final List<byte[]> l = new ArrayList<byte[]>();
        for (final String n : new String[] { "Target", "Kind", "Holder" }) {
            l.add(InstrumenterTest.bytes("jflp.fixture." + n));
        }
        return l;
    }

    private static List<byte[]> classesOf(final File jar) throws IOException {
        final List<byte[]> l = new ArrayList<byte[]>();
        final JarFile jf = new JarFile(jar);
        try {
            for (final Enumeration<JarEntry> e = jf.entries(); e.hasMoreElements();) {
                final JarEntry entry = e.nextElement();
                if (entry.getName().endsWith(".class") && !entry.getName().contains("module-info")
                        && !entry.getName().startsWith("META-INF/")) {
                    final InputStream in = jf.getInputStream(entry);
                    final ByteArrayOutputStream out = new ByteArrayOutputStream();
                    final byte[] buf = new byte[8192];
                    for (int n; (n = in.read(buf)) > 0;) {
                        out.write(buf, 0, n);
                    }
                    in.close();
                    l.add(out.toByteArray());
                }
            }
        } finally {
            jf.close();
        }
        return l;
    }
}
