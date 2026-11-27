package jflp.report;

import java.io.BufferedWriter;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

import jflp.data.RawFormat;
import jflp.data.RawReader;
import jflp.instr.Instrumenter;

/**
 * Turns the agent's per-JVM coverage files into reports:
 * <ul>
 * <li>{@code jaguar2.csv} and {@code jaguar2.xml}, in the formats of Jaguar2's exporters;</li>
 * <li>{@code coverageMatrix.txt}, in the format of the Jaguar2 fork, plus
 * {@code coverageMatrix.columns.csv} and {@code coverageMatrix.tests.csv}
 * naming its columns and rows, which the original lacks;</li>
 * <li>{@code jflp-summary.txt}: counts and every warning.</li>
 * </ul>
 */
public final class Report {

    public static final class Options {
        public File dataDir;
        public List<File> classDirs = new ArrayList<File>();
        public File outDir;
        public Heuristic heuristic = Heuristic.OCHIAI;
        public boolean matrix = true;
        /** Also write spectra.csv: every code line, susp 0 included (the UI's heat map needs them). */
        public boolean allLines;
    }

    /** What happened, for the caller to print and to decide the exit status. */
    public static final class Summary {
        public int files;
        public int truncatedFiles;
        public int passed;
        public int failed;
        public int classes;
        public int lines;
        /** Branch edges measured, or -1 when the agent ran without branches=true. */
        public int branches = -1;
        /** Def-use associations measured, or -1 when the agent ran without duas=true. */
        public int duas = -1;
        public final List<String> warnings = new ArrayList<String>();
    }

    private final Options o;
    private final Model model = new Model();
    private final Summary summary = new Summary();
    private final List<FileClasses> perFile = new ArrayList<FileClasses>();
    private final JacocoLines jacoco;
    private boolean jacocoMode;
    private final ElementModel branches = new ElementModel();
    private boolean branchMode;
    private final ElementModel duas = new ElementModel();
    private boolean duaMode;

    /** Class records of one coverage file, kept for mapping its probe ids in pass 2. */
    private static final class FileClasses {
        final File file;
        // {base, name, int[][] lines of each probe, int[] probe lines, byte[] kinds, int[] args, Object[][] duas}
        final List<Object[]> records = new ArrayList<Object[]>();
        int maxId;

        FileClasses(final File file) {
            this.file = file;
        }
    }

    private Report(final Options o) {
        this.o = o;
        this.jacoco = new JacocoLines(o.classDirs);
    }

    public static Summary run(final Options o) throws IOException {
        return new Report(o).execute();
    }

    private Summary execute() throws IOException {
        final File[] files = o.dataDir.listFiles();
        final List<File> data = new ArrayList<File>();
        if (files != null) {
            for (final File f : files) {
                if (f.getName().endsWith(RawFormat.SUFFIX)) {
                    data.add(f);
                }
            }
        }
        if (data.isEmpty()) {
            throw new IOException("no coverage files (*" + RawFormat.SUFFIX + ") in " + o.dataDir
                    + "; did the tests run with -javaagent:jflp.jar?");
        }
        // File names start with the JVM start time, so this is execution order.
        java.util.Collections.sort(data);
        summary.files = data.size();

        readClasses(data);
        addUnloadedClasses();
        model.seal();
        branches.seal();
        duas.seal();
        summary.classes = model.classes.size();
        summary.lines = model.columns;
        summary.branches = branchMode ? branches.columns : -1;
        summary.duas = duaMode ? duas.columns : -1;

        if (!o.outDir.isDirectory() && !o.outDir.mkdirs()) {
            throw new IOException("cannot create " + o.outDir);
        }
        final Counts counts = accumulate();
        if (summary.passed + summary.failed == 0) {
            summary.warnings.add("no test was recorded: the test framework is not hooked"
                    + " (supported: JUnit 3, JUnit 4, JUnit 5 Platform, TestNG)");
        }
        if (summary.failed == 0) {
            summary.warnings.add("no failing test: every suspiciousness score is 0");
        }
        writeCsv(counts);
        if (o.allLines) {
            writeSpectra(counts);
        }
        writeXml(counts);
        if (branchMode) {
            writeBranchCsv(counts);
        }
        if (duaMode) {
            writeDuaCsv(counts);
        }
        writeSummary();
        return summary;
    }

    // ---- pass 1: classes ------------------------------------------------------------------

    private void readClasses(final List<File> data) throws IOException {
        for (final File f : data) {
            final FileClasses fc = new FileClasses(f);
            perFile.add(fc);
            final boolean complete = RawReader.read(f, new RawReader.Visitor() {
                public void classRecord(final int base, final String name, final String source,
                        final long crc, final int[] lines, final byte[] kinds, final int[] args, final Object[][] duaInfo)
                        throws IOException {
                    final boolean jacocoProbes = lines.length > 0 && kinds[0] == RawFormat.KIND_JACOCO;
                    final JacocoLines.Mapping m = jacocoProbes ? jacoco.get(name, crc, lines.length) : null;
                    if (m != null && m.excluded) {
                        return;
                    }
                    final Model.Clazz c = model.clazz(name);
                    final Long k = Long.valueOf(crc);
                    if (!c.crcs.contains(k)) {
                        if (!c.crcs.isEmpty()) {
                            summary.warnings.add("class " + name.replace('/', '.')
                                    + " was loaded with different bytecode in different class loaders"
                                    + " or JVMs; its line sets are merged");
                        }
                        c.crcs.add(k);
                    }
                    final int[][] linesOfProbe = new int[lines.length][];
                    if (jacocoProbes) {
                        jacocoMode = true;
                        if (m == null) {
                            summary.warnings.add("class " + name.replace('/', '.') + " (lines=jacoco): no class file"
                                    + " with the same bytecode in the class directories; its coverage is dropped");
                            return;
                        }
                        if (c.source == null) {
                            c.source = m.source;
                        }
                        for (final int l : m.lines) {
                            c.lineSet.add(Integer.valueOf(l));
                        }
                        System.arraycopy(m.linesOfProbe, 0, linesOfProbe, 0, lines.length);
                    } else {
                        if (c.source == null) {
                            c.source = source;
                        }
                        for (int i = 0; i < lines.length; i++) {
                            if (kinds[i] == RawFormat.KIND_BRANCH) {
                                branchMode = true;
                                branches.add(name, lines[i], ElementModel.describe(args[i]));
                                linesOfProbe[i] = new int[0];
                            } else if (kinds[i] == RawFormat.KIND_DUA) {
                                duaMode = true;
                                duas.add(name, lines[i], ElementModel.dua(duaInfo[i]));
                                linesOfProbe[i] = new int[0];
                            } else if (kinds[i] == RawFormat.KIND_PAD) {
                                linesOfProbe[i] = new int[0];
                            } else {
                                c.lineSet.add(Integer.valueOf(lines[i]));
                                linesOfProbe[i] = new int[] { lines[i] };
                            }
                        }
                    }
                    fc.records.add(new Object[] { Integer.valueOf(base), name, linesOfProbe, lines, kinds, args,
                        duaInfo });
                    fc.maxId = Math.max(fc.maxId, base + lines.length);
                }

                public void testRecord(final boolean failed, final String name, final int[] ids)
                            throws IOException {
                }

                public void warning(final String message) {
                    summary.warnings.add(message);
                }
            });
            if (!complete) {
                summary.truncatedFiles++;
                summary.warnings.add(f.getName() + " ends abruptly: its JVM was killed or crashed;"
                        + " tests completed before that are kept, the one running is lost");
            }
        }
    }

    /** Classes no test loaded still belong to the spectra, with all-zero coverage. */
    private void addUnloadedClasses() throws IOException {
        for (final File dir : o.classDirs) {
            if (!dir.isDirectory()) {
                summary.warnings.add("class directory " + dir + " does not exist");
                continue;
            }
            addUnloadedClasses(dir);
        }
    }

    /**
     * Walks the directory as JaCoCo's {@code Analyzer.analyzeAll} does, in
     * {@code listFiles()} order, which is the order the Jaguar2 fork fills its
     * class map in. That order breaks ties in the coverage matrix layout.
     */
    private void addUnloadedClasses(final File dir) throws IOException {
        final File[] entries = dir.listFiles();
        if (entries == null) {
            return;
        }
        for (final File f : entries) {
            if (f.isDirectory()) {
                addUnloadedClasses(f);
            } else if (f.getName().endsWith(".class")) {
                final Instrumenter.Analysis a;
                try {
                    a = Instrumenter.analyze(readAll(f), branchMode, duaMode);
                } catch (final RuntimeException e) {
                    summary.warnings.add("cannot analyze " + f + ": " + e);
                    continue;
                }
                if (a == null) {
                    continue;
                }
                model.directoryOrder.add(a.info.name);
                if (model.classes.containsKey(a.info.name)) {
                    continue;
                }
                final Model.Clazz c = model.clazz(a.info.name);
                c.source = a.info.source;
                for (int i = 0; i < a.info.probeCount(); i++) {
                    if (a.info.probeKinds[i] == RawFormat.KIND_BRANCH) {
                        branches.add(a.info.name, a.info.probeLines[i], ElementModel.describe(a.info.probeArgs[i]));
                    } else if (a.info.probeKinds[i] == RawFormat.KIND_DUA) {
                        duas.add(a.info.name, a.info.probeLines[i], ElementModel.dua(a.info.probeDuas[i]));
                    } else if (a.info.probeKinds[i] == RawFormat.KIND_PAD) {
                        continue;
                    } else {
                        c.lineSet.add(Integer.valueOf(a.info.probeLines[i]));
                    }
                }
            }
        }
    }

    // ---- pass 2: tests --------------------------------------------------------------------

    static final class Counts {
        int[] cef;
        int[] cep;
        int[] bcef;
        int[] bcep;
        int[] dcef;
        int[] dcep;
        /** Per source file and line, counting each test once even if several classes cover the line. */
        final Map<String, TreeMap<Integer, int[]>> bySource = new TreeMap<String, TreeMap<Integer, int[]>>();
    }

    private Counts accumulate() throws IOException {
        final Counts counts = new Counts();
        counts.cef = new int[model.columns];
        counts.cep = new int[model.columns];
        counts.bcef = new int[branches.columns];
        counts.bcep = new int[branches.columns];
        counts.dcef = new int[duas.columns];
        counts.dcep = new int[duas.columns];

        // Column -> slot in the per-source-file counts.
        final int[][] sourceSlot = new int[model.columns][];
        for (final Model.Clazz c : model.classes.values()) {
            if (c.source == null) {
                continue;
            }
            final String key = c.packageName() + '/' + c.source;
            TreeMap<Integer, int[]> lines = counts.bySource.get(key);
            if (lines == null) {
                lines = new TreeMap<Integer, int[]>();
                counts.bySource.put(key, lines);
            }
            for (int p = 0; p < c.lines.length; p++) {
                final Integer line = Integer.valueOf(c.lines[p]);
                int[] slot = lines.get(line);
                if (slot == null) {
                    slot = new int[] { 0, 0, -1 }; // cef, cep, last test counted
                    lines.put(line, slot);
                }
                sourceSlot[c.colBase + p] = slot;
            }
        }

        final Matrix matrix = o.matrix ? new Matrix(model, o.outDir) : null;
        final ElementModel.MatrixWriter branchMatrix = o.matrix && branchMode
                ? new ElementModel.MatrixWriter(branches, o.outDir, "branchMatrix", "column,class,line,branch",
                        new ElementModel.ColumnLine() {
                            public String of(final int c) {
                                return branches.classOfCol[c].replace('/', '.') + "," + branches.lineOfCol[c] + ","
                                        + branches.labelOfCol[c];
                            }
                        }) : null;
        final ElementModel.MatrixWriter duaMatrix = o.matrix && duaMode
                ? new ElementModel.MatrixWriter(duas, o.outDir, "duaMatrix",
                        "column,class,method,variable,def_line,use_line,target_line", new ElementModel.ColumnLine() {
                            public String of(final int c) {
                                final String[] d = duas.labelOfCol[c].split("\t", -1);
                                return duas.classOfCol[c].replace('/', '.') + "," + Csv.quote(d[0]) + "," + d[1] + ","
                                        + d[2] + "," + duas.lineOfCol[c] + "," + d[3];
                            }
                        }) : null;
        final int[] branchSeen = new int[branches.columns];
        Arrays.fill(branchSeen, -1);
        final int[] duaSeen = new int[duas.columns];
        Arrays.fill(duaSeen, -1);
        final int[] seen = new int[model.columns];
        Arrays.fill(seen, -1);
        final int[] testIndex = { 0 };
        try {
            for (final FileClasses fc : perFile) {
                final int[][] idToCols = new int[fc.maxId][];
                final int[] idToBranch = new int[fc.maxId];
                Arrays.fill(idToBranch, -1);
                final int[] idToDua = new int[fc.maxId];
                Arrays.fill(idToDua, -1);
                for (final Object[] r : fc.records) {
                    final int[] probeLines = (int[]) r[3];
                    final byte[] probeKinds = (byte[]) r[4];
                    final int[] probeArgs = (int[]) r[5];
                    final Object[][] probeDuas = (Object[][]) r[6];
                    for (int i = 0; i < probeLines.length; i++) {
                        final int id = ((Integer) r[0]).intValue() + i;
                        if (probeKinds[i] == RawFormat.KIND_BRANCH) {
                            idToBranch[id] = branches.col((String) r[1], probeLines[i],
                                    ElementModel.describe(probeArgs[i]));
                        } else if (probeKinds[i] == RawFormat.KIND_DUA) {
                            idToDua[id] = duas.col((String) r[1], probeLines[i], ElementModel.dua(probeDuas[i]));
                        }
                    }
                    final int base = ((Integer) r[0]).intValue();
                    final Model.Clazz c = model.classes.get(r[1]);
                    final int[][] lines = (int[][]) r[2];
                    for (int i = 0; i < lines.length; i++) {
                        final int[] cols = new int[lines[i].length];
                        for (int j = 0; j < cols.length; j++) {
                            cols[j] = c.col(lines[i][j]);
                        }
                        idToCols[base + i] = cols;
                    }
                }
                RawReader.read(fc.file, new RawReader.Visitor() {
                    public void classRecord(final int base, final String name, final String source,
                            final long crc, final int[] lines, final byte[] kinds, final int[] args,
                            final Object[][] duaInfo) {
                    }

                    public void testRecord(final boolean failed, final String name, final int[] ids)
                            throws IOException {
                        final int t = testIndex[0]++;
                        int[] cols = new int[ids.length];
                        int n = 0;
                        final int[] bcols = new int[ids.length];
                        int bn = 0;
                        final int[] dcols = new int[ids.length];
                        int dn = 0;
                        for (final int id : ids) {
                            final int du = id < idToDua.length ? idToDua[id] : -1;
                            if (du >= 0 && duaSeen[du] != t) {
                                duaSeen[du] = t;
                                dcols[dn++] = du;
                                if (failed) {
                                    counts.dcef[du]++;
                                } else {
                                    counts.dcep[du]++;
                                }
                            }
                            final int b = id < idToBranch.length ? idToBranch[id] : -1;
                            if (b >= 0 && branchSeen[b] != t) {
                                branchSeen[b] = t;
                                bcols[bn++] = b;
                                if (failed) {
                                    counts.bcef[b]++;
                                } else {
                                    counts.bcep[b]++;
                                }
                            }
                            final int[] mapped = id < idToCols.length ? idToCols[id] : null;
                            if (mapped == null) {
                                continue;
                            }
                            for (final int col : mapped) {
                                if (col < 0 || seen[col] == t) {
                                    continue;
                                }
                                if (n == cols.length) {
                                    final int[] grown = new int[n * 2 + 1];
                                    System.arraycopy(cols, 0, grown, 0, n);
                                    cols = grown;
                                }
                                seen[col] = t;
                                cols[n++] = col;
                                if (failed) {
                                    counts.cef[col]++;
                                } else {
                                    counts.cep[col]++;
                                }
                                final int[] slot = sourceSlot[col];
                                if (slot != null && slot[2] != t) {
                                    slot[2] = t;
                                    slot[failed ? 0 : 1]++;
                                }
                            }
                        }
                        if (failed) {
                            summary.failed++;
                        } else {
                            summary.passed++;
                        }
                        if (matrix != null) {
                            matrix.row(name, failed, cols, n);
                        }
                        if (branchMatrix != null) {
                            branchMatrix.row(name, failed, bcols, bn);
                        }
                        if (duaMatrix != null) {
                            duaMatrix.row(name, failed, dcols, dn);
                        }
                    }

                    public void warning(final String message) {
                    }
                });
            }
        } finally {
            if (matrix != null) {
                matrix.close();
            }
            if (branchMatrix != null) {
                branchMatrix.close();
            }
            if (duaMatrix != null) {
                duaMatrix.close();
            }
        }
        return counts;
    }

    // ---- writers --------------------------------------------------------------------------

    private double susp(final int cef, final int cep) {
        return o.heuristic.eval(cef, summary.failed - cef, cep, summary.passed - cep);
    }

    /** Jaguar2's {@code %f}, pinned to a dot decimal separator whatever the locale. */
    static String fmt(final double d) {
        return String.format(Locale.US, "%f", Double.valueOf(d));
    }

    private void writeCsv(final Counts c) throws IOException {
        final Writer w = writer(new File(o.outDir, "jaguar2.csv"));
        try {
            for (final Model.Clazz clazz : model.classes.values()) {
                for (int p = 0; p < clazz.lines.length; p++) {
                    final int col = clazz.colBase + p;
                    final int cef = c.cef[col];
                    final int cep = c.cep[col];
                    final double s = susp(cef, cep);
                    if (s > 0) {
                        w.write(clazz.name + "," + clazz.lines[p] + "," + cef + "," + (summary.failed - cef)
                                + "," + cep + "," + (summary.passed - cep) + "," + fmt(s) + "\n");
                    }
                }
            }
        } finally {
            w.close();
        }
    }

    /** {@code branches.csv}: jaguar2.csv's columns plus the branch ({@link ElementModel#describe}). */
    private void writeBranchCsv(final Counts c) throws IOException {
        final Writer w = writer(new File(o.outDir, "branches.csv"));
        try {
            for (int col = 0; col < branches.columns; col++) {
                final int cef = c.bcef[col];
                final int cep = c.bcep[col];
                final double s = susp(cef, cep);
                if (s > 0) {
                    w.write(branches.classOfCol[col] + "," + branches.lineOfCol[col] + ","
                            + branches.labelOfCol[col] + "," + cef + "," + (summary.failed - cef)
                            + "," + cep + "," + (summary.passed - cep) + "," + fmt(s) + "\n");
                }
            }
        } finally {
            w.close();
        }
    }

    /**
     * {@code duas.csv}: {@code class,method,variable,def_line,use_line,target_line,cef,cnf,cep,cnp,susp};
     * target_line is empty for a c-use and the line of the edge's target for a p-use.
     */
    private void writeDuaCsv(final Counts c) throws IOException {
        final Writer w = writer(new File(o.outDir, "duas.csv"));
        try {
            for (int col = 0; col < duas.columns; col++) {
                final int cef = c.dcef[col];
                final int cep = c.dcep[col];
                final double s = susp(cef, cep);
                if (s > 0) {
                    final String[] d = duas.labelOfCol[col].split("\t", -1);
                    w.write(duas.classOfCol[col] + "," + Csv.quote(d[0]) + "," + d[1] + "," + d[2] + ","
                            + duas.lineOfCol[col] + "," + d[3] + "," + cef + "," + (summary.failed - cef) + "," + cep
                            + "," + (summary.passed - cep) + "," + fmt(s) + "\n");
                }
            }
        } finally {
            w.close();
        }
    }

    /** {@code spectra.csv}: header, then every code line as in jaguar2.csv, susp 0 included. */
    private void writeSpectra(final Counts c) throws IOException {
        final Writer w = writer(new File(o.outDir, "spectra.csv"));
        try {
            w.write("class,line,cef,cnf,cep,cnp,susp,source\n");
            for (final Model.Clazz clazz : model.classes.values()) {
                final String source = clazz.source == null ? "" : clazz.packageName() + (clazz.packageName().length() == 0
                        ? "" : "/") + clazz.source;
                for (int p = 0; p < clazz.lines.length; p++) {
                    final int col = clazz.colBase + p;
                    final int cef = c.cef[col];
                    final int cep = c.cep[col];
                    w.write(clazz.name + "," + clazz.lines[p] + "," + cef + "," + (summary.failed - cef) + "," + cep
                            + "," + (summary.passed - cep) + "," + fmt(susp(cef, cep)) + "," + source + "\n");
                }
            }
        } finally {
            w.close();
        }
    }

    private void writeXml(final Counts c) throws IOException {
        // Packages in the order Jaguar2 groups them: packages of the sorted classes first.
        final Set<String> packages = new LinkedHashSet<String>();
        for (final Model.Clazz clazz : model.classes.values()) {
            packages.add(clazz.packageName());
        }
        final Map<String, List<String>> sourcesByPackage = new HashMap<String, List<String>>();
        for (final String key : c.bySource.keySet()) {
            final String pkg = key.substring(0, key.lastIndexOf('/'));
            packages.add(pkg);
            List<String> l = sourcesByPackage.get(pkg);
            if (l == null) {
                l = new ArrayList<String>();
                sourcesByPackage.put(pkg, l);
            }
            l.add(key);
        }

        final Writer w = writer(new File(o.outDir, "jaguar2.xml"));
        try {
            w.write("<?xml version=\"1.0\" ?><report><tests fail=\"" + summary.failed + "\" pass=\""
                    + summary.passed + "\"></tests>");
            for (final String pkg : packages) {
                w.write("<package name=\"" + Xml.escape(pkg) + "\">");
                final List<String> sources = sourcesByPackage.get(pkg);
                if (sources != null) {
                    for (final String key : sources) {
                        w.write("<sourcefile name=\"" + Xml.escape(key.substring(key.lastIndexOf('/') + 1)) + "\">");
                        for (final Map.Entry<Integer, int[]> e : c.bySource.get(key).entrySet()) {
                            final int cef = e.getValue()[0];
                            final int cep = e.getValue()[1];
                            final double s = susp(cef, cep);
                            if (s > 0) {
                                w.write("<line nr=\"" + e.getKey() + "\" cef=\"" + cef + "\" cep=\"" + cep
                                        + "\" susp=\"" + fmt(s) + "\"></line>");
                            }
                        }
                        w.write("</sourcefile>");
                    }
                }
                w.write("</package>");
            }
            w.write("</report>");
        } finally {
            w.close();
        }
    }

    private void writeSummary() throws IOException {
        final Writer w = writer(new File(o.outDir, "jflp-summary.txt"));
        try {
            w.write("coverage files: " + summary.files + " (" + summary.truncatedFiles + " truncated)\n");
            w.write("tests: " + (summary.passed + summary.failed) + " (" + summary.failed + " failed, "
                    + summary.passed + " passed)\n");
            w.write("classes: " + summary.classes + "\n");
            w.write("lines: " + summary.lines + "\n");
            if (summary.branches >= 0) {
                w.write("branches: " + summary.branches + "\n");
            }
            if (summary.duas >= 0) {
                w.write("def-use associations: " + summary.duas + "\n");
            }
            w.write("heuristic: " + o.heuristic.name().toLowerCase() + "\n");
            w.write("line semantics: " + (jacocoMode ? "jacoco (as Jaguar2)" : "entry") + "\n");
            w.write("warnings: " + summary.warnings.size() + "\n");
            for (final String s : summary.warnings) {
                w.write("  " + s + "\n");
            }
        } finally {
            w.close();
        }
    }

    static Writer writer(final File f) throws IOException {
        return new BufferedWriter(new OutputStreamWriter(new FileOutputStream(f), "UTF-8"), 1 << 16);
    }

    static byte[] readAll(final File f) throws IOException {
        final InputStream in = new FileInputStream(f);
        try {
            final byte[] b = new byte[(int) f.length()];
            int off = 0;
            while (off < b.length) {
                final int r = in.read(b, off, b.length - off);
                if (r < 0) {
                    throw new IOException("short read: " + f);
                }
                off += r;
            }
            return b;
        } finally {
            in.close();
        }
    }
}
