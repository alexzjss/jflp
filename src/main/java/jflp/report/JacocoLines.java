package jflp.report;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

import org.jacoco.core.analysis.Analyzer;
import org.jacoco.core.analysis.CoverageBuilder;
import org.jacoco.core.analysis.IClassCoverage;
import org.jacoco.core.analysis.ICounter;
import org.jacoco.core.data.ExecutionData;
import org.jacoco.core.data.ExecutionDataStore;
import org.jacoco.core.internal.data.CRC64;

/**
 * Lines covered by each probe of a class instrumented with {@code lines=jacoco}.
 *
 * JaCoCo's line status is an OR over probes (a probe covers the instructions
 * before it), so analysing the class once per probe, with only that probe set,
 * gives a table that turns any test's probes into exactly the lines JaCoCo, and
 * so Jaguar2, would report. It costs one analysis per probe per class, once,
 * instead of Jaguar2's full analysis per test.
 */
final class JacocoLines {

    static final class Mapping {
        final String source;
        final int[][] linesOfProbe;
        /** Every code line, covered or not (the same set the default mode uses). */
        final int[] lines;
        /** Synthetic classes and the like, which JaCoCo instruments but its analyzer skips. */
        final boolean excluded;

        Mapping(final String source, final int[][] linesOfProbe, final int[] lines) {
            this.source = source;
            this.linesOfProbe = linesOfProbe;
            this.lines = lines;
            this.excluded = false;
        }

        Mapping() {
            this.source = null;
            this.linesOfProbe = null;
            this.lines = null;
            this.excluded = true;
        }
    }

    private final List<File> classDirs;
    private Map<Long, File> filesById;
    private final Map<Long, Mapping> cache = new HashMap<Long, Mapping>();

    JacocoLines(final List<File> classDirs) {
        this.classDirs = classDirs;
    }

    /** Returns null when no class file in the class directories has this id. */
    Mapping get(final String name, final long classId, final int probes) throws IOException {
        final Long key = Long.valueOf(classId);
        Mapping m = cache.get(key);
        if (m != null) {
            return m;
        }
        if (filesById == null) {
            filesById = new HashMap<Long, File>();
            for (final File d : classDirs) {
                index(d);
            }
        }
        final File f = filesById.get(key);
        if (f == null) {
            return null;
        }
        final byte[] bytes = Report.readAll(f);
        final jflp.instr.Instrumenter.Analysis a = jflp.instr.Instrumenter.analyze(bytes);
        if (a == null) {
            m = new Mapping();
            cache.put(key, m);
            return m;
        }
        final int[][] lines = new int[probes][];
        String source = null;
        for (int p = 0; p < probes; p++) {
            final boolean[] hit = new boolean[probes];
            hit[p] = true;
            final ExecutionDataStore store = new ExecutionDataStore();
            store.put(new ExecutionData(classId, name, hit));
            final CoverageBuilder b = new CoverageBuilder();
            new Analyzer(store, b).analyzeClass(bytes, f.getPath());
            final TreeSet<Integer> covered = new TreeSet<Integer>();
            for (final IClassCoverage cc : b.getClasses()) {
                source = cc.getSourceFileName();
                for (int l = cc.getFirstLine(); l >= 0 && l <= cc.getLastLine(); l++) {
                    final int status = cc.getLine(l).getStatus();
                    if (status == ICounter.FULLY_COVERED || status == ICounter.PARTLY_COVERED) {
                        covered.add(Integer.valueOf(l));
                    }
                }
            }
            lines[p] = new int[covered.size()];
            int i = 0;
            for (final Integer l : covered) {
                lines[p][i++] = l.intValue();
            }
        }
        m = new Mapping(source, lines, a.info.probeLines);
        cache.put(key, m);
        return m;
    }

    private void index(final File dir) throws IOException {
        final File[] entries = dir.listFiles();
        if (entries == null) {
            return;
        }
        final List<File> sub = new ArrayList<File>();
        for (final File f : entries) {
            if (f.isDirectory()) {
                sub.add(f);
            } else if (f.getName().endsWith(".class")) {
                filesById.put(Long.valueOf(CRC64.classId(Report.readAll(f))), f);
            }
        }
        for (final File d : sub) {
            index(d);
        }
    }
}
