package jflp.ui;

import java.io.BufferedReader;
import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import jflp.report.Heuristic;
import jflp.report.Report;

/**
 * What the result pages show, read from a run's report: summary, ranking of
 * suspicious lines (average rank over ties), source files with their lines'
 * scores, tests, branch and def-use spectra, and the rank of the real buggy
 * lines when the source came with a fix.
 */
final class Results {

    static final class Line {
        String cls;
        int line;
        int cef;
        int cnf;
        int cep;
        int cnp;
        double susp;
        String source;
        double rank;
        boolean buggy;
    }

    private static final Map<String, Object[]> CACHE = Collections.synchronizedMap(new LinkedHashMap<String, Object[]>(
            16, 0.75f, true) {
        @Override
        protected boolean removeEldestEntry(final Map.Entry<String, Object[]> e) {
            return size() > 8;
        }
    });

    private final Workspace ws;
    private final Map<String, Object> project;
    private final Map<String, Object> run;

    Results(final Workspace ws, final Map<String, Object> project, final Map<String, Object> run) {
        this.ws = ws;
        this.project = project;
        this.run = run;
    }

    // ---- report directories ----

    String defaultHeuristic() {
        @SuppressWarnings("unchecked")
        final Map<String, Object> opt = (Map<String, Object>) run.get("options");
        final String h = Sources.str(opt.get("heuristic"));
        return h.isEmpty() ? "ochiai" : h.toLowerCase();
    }

    /** The report for heuristic h, written on first use for heuristics other than the run's. */
    File reportDir(final String heuristic) throws IOException {
        final String h = heuristic == null || heuristic.isEmpty() ? defaultHeuristic() : heuristic.toLowerCase();
        final File dir = ws.runDir((String) run.get("id"));
        if (h.equals(defaultHeuristic())) {
            return new File(dir, "report");
        }
        final File alt = new File(dir, "report-" + h);
        synchronized (Results.class) {
            if (!new File(alt, "spectra.csv").isFile()) {
                final Report.Options o = new Report.Options();
                o.dataDir = new File(dir, "data");
                o.outDir = alt;
                o.heuristic = Heuristic.byName(h);
                o.allLines = true;
                o.matrix = false;
                for (final String c : Sources.str(run.get("classDirs")).split(File.pathSeparator)) {
                    if (!c.isEmpty()) {
                        o.classDirs.add(new File(c));
                    }
                }
                Report.run(o);
            }
        }
        return alt;
    }

    /** All code lines, most suspicious first, ranked. */
    @SuppressWarnings("unchecked")
    List<Line> lines(final String heuristic) throws IOException {
        final File spectra = new File(reportDir(heuristic), "spectra.csv");
        final String key = spectra.getPath() + "@" + spectra.lastModified();
        final Object[] cached = CACHE.get(key);
        if (cached != null) {
            return (List<Line>) cached[0];
        }
        final List<Line> lines = new ArrayList<>();
        try (BufferedReader r = Files.newBufferedReader(spectra.toPath(), StandardCharsets.UTF_8)) {
            r.readLine();
            for (String s; (s = r.readLine()) != null;) {
                final String[] f = s.split(",", -1);
                if (f.length < 8) {
                    continue;
                }
                final Line l = new Line();
                l.cls = f[0];
                l.line = Integer.parseInt(f[1]);
                l.cef = Integer.parseInt(f[2]);
                l.cnf = Integer.parseInt(f[3]);
                l.cep = Integer.parseInt(f[4]);
                l.cnp = Integer.parseInt(f[5]);
                l.susp = Double.parseDouble(f[6]);
                l.source = f[7];
                lines.add(l);
            }
        }
        lines.sort((a, b) -> Double.compare(b.susp, a.susp));
        // Average rank over ties: any of the tied lines could be inspected first.
        for (int i = 0; i < lines.size();) {
            int j = i;
            while (j < lines.size() && lines.get(j).susp == lines.get(i).susp) {
                j++;
            }
            final double r = (i + 1 + j) / 2.0;
            for (int k = i; k < j; k++) {
                lines.get(k).rank = r;
            }
            i = j;
        }
        final Set<String> buggy = truthLines();
        for (final Line l : lines) {
            if (!buggy.isEmpty()) {
                for (final String b : buggy) {
                    final int colon = b.lastIndexOf(':');
                    if (Integer.parseInt(b.substring(colon + 1)) == l.line
                            && Truth.sourceOf(b.substring(0, colon), l.cls)) {
                        l.buggy = true;
                        break;
                    }
                }
            }
        }
        CACHE.put(key, new Object[] { lines });
        return lines;
    }

    private Set<String> truthLines() throws IOException {
        @SuppressWarnings("unchecked")
        final Map<String, Object> t = (Map<String, Object>) project.get("truth");
        if (t == null) {
            return Collections.emptySet();
        }
        final File patch = new File(Sources.str(t.get("patch")));
        if (!patch.isFile()) {
            return Collections.emptySet();
        }
        return Truth.patchLines(patch, Sources.str(t.get("side")).charAt(0));
    }

    // ---- views ----

    Map<String, Object> summary() throws IOException {
        final Map<String, Object> s = new LinkedHashMap<>();
        final File report = reportDir(null);
        final List<String> warnings = new ArrayList<>();
        for (final String l : Files.readAllLines(new File(report, "jflp-summary.txt").toPath(), StandardCharsets.UTF_8)) {
            if (l.startsWith("  ")) {
                warnings.add(l.trim());
            } else if (l.contains(": ")) {
                final String k = l.substring(0, l.indexOf(": "));
                final String v = l.substring(l.indexOf(": ") + 2);
                if (k.equals("tests")) {
                    s.put("tests", Integer.parseInt(v.split(" ")[0]));
                } else if (k.equals("lines") || k.equals("classes") || k.equals("branches")
                        || k.equals("def-use associations")) {
                    s.put(k.equals("def-use associations") ? "duas" : k, Integer.parseInt(v.trim()));
                } else if (k.equals("line semantics")) {
                    s.put("semantics", v);
                }
            }
        }
        s.put("warnings", warnings);
        final List<Map<String, Object>> tests = tests();
        int failed = 0;
        final List<String> failing = new ArrayList<>();
        for (final Map<String, Object> t : tests) {
            if ("FAIL".equals(t.get("outcome"))) {
                failed++;
                failing.add((String) t.get("name"));
            }
        }
        s.put("failed", failed);
        s.put("passed", tests.size() - failed);
        s.put("failing", failing);
        final Object expected = project.get("expectedFailing");
        if (expected instanceof List) {
            final List<String> missing = new ArrayList<>();
            for (final Object e : (List<?>) expected) {
                if (!failing.contains(String.valueOf(e))) {
                    missing.add(String.valueOf(e));
                }
            }
            s.put("expectedFailing", expected);
            s.put("expectedMissing", missing);
        }
        final List<Line> lines = lines(null);
        s.put("lines", lines.size());
        s.put("suspicious", lines.stream().filter(l -> l.susp > 0).count());
        s.put("topSusp", lines.isEmpty() ? 0 : lines.get(0).susp);
        s.put("heuristic", defaultHeuristic());
        s.put("truth", truth(lines));
        s.put("histogram", histogram(lines));
        return s;
    }

    /** Buggy lines found among the code lines, with their ranks, and the best one (EXAM = rank / lines). */
    Map<String, Object> truth(final List<Line> lines) throws IOException {
        if (project.get("truth") == null) {
            return null;
        }
        final Map<String, Object> t = new LinkedHashMap<>();
        final List<Map<String, Object>> found = new ArrayList<>();
        double best = -1;
        for (final Line l : lines) {
            if (l.buggy) {
                found.add(row(l));
                if (best < 0 || l.rank < best) {
                    best = l.rank;
                }
            }
        }
        t.put("patchLines", truthLines().size());
        t.put("lines", found);
        t.put("bestRank", best < 0 ? null : best);
        t.put("exam", best < 0 || lines.isEmpty() ? null : best / lines.size());
        return t;
    }

    private static List<Integer> histogram(final List<Line> lines) {
        final int[] bins = new int[10];
        for (final Line l : lines) {
            if (l.susp > 0) {
                bins[Math.min(9, (int) Math.floor(Math.min(l.susp, 1.0) * 10))]++;
            }
        }
        final List<Integer> out = new ArrayList<>();
        for (final int b : bins) {
            out.add(b);
        }
        return out;
    }

    Map<String, Object> ranking(final String heuristic, final int offset, final int limit, final String filter)
            throws IOException {
        final List<Line> lines = lines(heuristic);
        final List<Map<String, Object>> rows = new ArrayList<>();
        int total = 0;
        final String f = filter == null ? "" : filter.toLowerCase();
        for (final Line l : lines) {
            if (!f.isEmpty() && !(l.cls.replace('/', '.').toLowerCase().contains(f))) {
                continue;
            }
            if (total >= offset && rows.size() < limit) {
                rows.add(row(l));
            }
            total++;
        }
        final Map<String, Object> m = new LinkedHashMap<>();
        m.put("total", total);
        m.put("rows", rows);
        m.put("heuristic", heuristic == null || heuristic.isEmpty() ? defaultHeuristic() : heuristic);
        m.put("truth", truth(lines));
        return m;
    }

    private static Map<String, Object> row(final Line l) {
        final Map<String, Object> r = new LinkedHashMap<>();
        r.put("rank", l.rank);
        r.put("class", l.cls.replace('/', '.'));
        r.put("line", l.line);
        r.put("source", l.source);
        r.put("cef", l.cef);
        r.put("cnf", l.cnf);
        r.put("cep", l.cep);
        r.put("cnp", l.cnp);
        r.put("susp", l.susp);
        r.put("buggy", l.buggy);
        return r;
    }

    /** Source files with their most suspicious line, most suspicious first. */
    List<Map<String, Object>> files(final String heuristic) throws IOException {
        final Map<String, Map<String, Object>> by = new LinkedHashMap<>();
        for (final Line l : lines(heuristic)) {
            Map<String, Object> f = by.get(l.source);
            if (f == null) {
                f = new LinkedHashMap<>();
                f.put("source", l.source);
                f.put("lines", 0);
                f.put("maxSusp", 0.0);
                f.put("bestRank", l.rank);
                f.put("buggy", 0);
                f.put("covered", 0);
                by.put(l.source, f);
            }
            f.put("lines", (Integer) f.get("lines") + 1);
            if (l.cef + l.cep > 0) {
                f.put("covered", (Integer) f.get("covered") + 1);
            }
            if (l.buggy) {
                f.put("buggy", (Integer) f.get("buggy") + 1);
            }
            f.put("maxSusp", Math.max((Double) f.get("maxSusp"), l.susp));
            f.put("bestRank", Math.min((Double) f.get("bestRank"), l.rank));
        }
        final List<Map<String, Object>> out = new ArrayList<>(by.values());
        out.sort((a, b) -> Double.compare((Double) a.get("bestRank"), (Double) b.get("bestRank")));
        return out;
    }

    /** A source file's text with each code line's spectrum. */
    Map<String, Object> source(final String source, final String heuristic) throws IOException {
        final Map<Integer, Line> at = new HashMap<>();
        for (final Line l : lines(heuristic)) {
            if (l.source.equals(source)) {
                final Line prev = at.get(l.line);
                if (prev == null || l.susp > prev.susp) {
                    at.put(l.line, l);
                }
            }
        }
        final File f = locate(source);
        final Map<String, Object> m = new LinkedHashMap<>();
        m.put("source", source);
        m.put("path", f == null ? null : f.getPath());
        final List<Map<String, Object>> lines = new ArrayList<>();
        final List<String> text = f == null ? new ArrayList<String>() : readText(f);
        final int n = Math.max(text.size(), at.isEmpty() ? 0 : Collections.max(at.keySet()));
        for (int i = 1; i <= n; i++) {
            final Map<String, Object> r = new LinkedHashMap<>();
            r.put("n", i);
            r.put("text", i <= text.size() ? text.get(i - 1) : "");
            final Line l = at.get(i);
            if (l != null) {
                r.put("susp", l.susp);
                r.put("rank", l.rank);
                r.put("cef", l.cef);
                r.put("cep", l.cep);
                r.put("buggy", l.buggy);
            }
            lines.add(r);
        }
        m.put("lines", lines);
        return m;
    }

    private static List<String> readText(final File f) throws IOException {
        try {
            return Files.readAllLines(f.toPath(), StandardCharsets.UTF_8);
        } catch (final java.nio.charset.MalformedInputException e) {
            return Files.readAllLines(f.toPath(), StandardCharsets.ISO_8859_1);
        }
    }

    /** The checkout file for "org/x/Foo.java", preferring main source trees. */
    File locate(final String source) {
        final File root = new File(Sources.str(project.get("dir")));
        final List<File> hits = new ArrayList<>();
        search(root, source, hits, 0);
        hits.sort((a, b) -> Integer.compare(score(a), score(b)));
        return hits.isEmpty() ? null : hits.get(0);
    }

    private static int score(final File f) {
        final String p = f.getPath().replace('\\', '/');
        return (p.contains("/src/main/") ? 0 : 10) + (p.contains("/test") ? 5 : 0) + p.length() / 100;
    }

    private static void search(final File dir, final String source, final List<File> hits, final int depth) {
        if (depth > 14) {
            return;
        }
        final File direct = new File(dir, source);
        if (direct.isFile()) {
            hits.add(direct);
        }
        final File[] subs = dir.listFiles(File::isDirectory);
        if (subs == null) {
            return;
        }
        for (final File s : subs) {
            final String n = s.getName();
            if (!n.startsWith(".") && !n.equals("target") && !n.equals("build") && !n.equals("node_modules")) {
                search(s, source, hits, depth + 1);
            }
        }
    }

    List<Map<String, Object>> tests() throws IOException {
        final File f = new File(reportDir(null), "coverageMatrix.tests.csv");
        final List<Map<String, Object>> out = new ArrayList<>();
        if (!f.isFile()) {
            return out;
        }
        final List<String> rows = Files.readAllLines(f.toPath(), StandardCharsets.UTF_8);
        for (int i = 1; i < rows.size(); i++) {
            final String[] c = splitCsv(rows.get(i));
            if (c.length >= 3) {
                final Map<String, Object> t = new LinkedHashMap<>();
                t.put("row", Integer.parseInt(c[0]));
                t.put("outcome", c[1]);
                t.put("name", c[2]);
                out.add(t);
            }
        }
        return out;
    }

    /** branches.csv or duas.csv rows, most suspicious first. */
    Map<String, Object> elements(final String kind, final String heuristic, final int limit) throws IOException {
        final File f = new File(reportDir(heuristic), "branches".equals(kind) ? "branches.csv" : "duas.csv");
        final Map<String, Object> m = new LinkedHashMap<>();
        m.put("available", f.isFile());
        final List<Map<String, Object>> rows = new ArrayList<>();
        if (f.isFile()) {
            final List<String[]> all = new ArrayList<>();
            for (final String l : Files.readAllLines(f.toPath(), StandardCharsets.UTF_8)) {
                all.add(splitCsv(l));
            }
            all.sort((a, b) -> Double.compare(Double.parseDouble(b[b.length - 1]), Double.parseDouble(a[a.length - 1])));
            for (final String[] c : all) {
                if (rows.size() >= limit) {
                    break;
                }
                final Map<String, Object> r = new LinkedHashMap<>();
                r.put("class", c[0].replace('/', '.'));
                if ("branches".equals(kind)) {
                    r.put("line", Integer.parseInt(c[1]));
                    r.put("branch", c[2]);
                } else {
                    r.put("method", c[1]);
                    r.put("variable", c[2]);
                    r.put("def", Integer.parseInt(c[3]));
                    r.put("use", Integer.parseInt(c[4]));
                    r.put("target", c[5].isEmpty() ? null : Integer.parseInt(c[5]));
                }
                r.put("cef", Integer.parseInt(c[c.length - 5]));
                r.put("cep", Integer.parseInt(c[c.length - 3]));
                r.put("susp", Double.parseDouble(c[c.length - 1]));
                rows.add(r);
            }
            m.put("total", all.size());
        }
        m.put("rows", rows);
        return m;
    }

    /** Report files that can be downloaded. */
    List<Map<String, Object>> artifacts() throws IOException {
        final List<Map<String, Object>> out = new ArrayList<>();
        final File[] fs = reportDir(null).listFiles();
        if (fs != null) {
            java.util.Arrays.sort(fs);
            for (final File f : fs) {
                final Map<String, Object> a = new LinkedHashMap<>();
                a.put("name", f.getName());
                a.put("bytes", f.length());
                out.add(a);
            }
        }
        return out;
    }

    static String[] splitCsv(final String line) {
        final List<String> out = new ArrayList<>();
        final StringBuilder b = new StringBuilder();
        boolean q = false;
        for (int i = 0; i < line.length(); i++) {
            final char c = line.charAt(i);
            if (q) {
                if (c == '"' && i + 1 < line.length() && line.charAt(i + 1) == '"') {
                    b.append('"');
                    i++;
                } else if (c == '"') {
                    q = false;
                } else {
                    b.append(c);
                }
            } else if (c == '"') {
                q = true;
            } else if (c == ',') {
                out.add(b.toString());
                b.setLength(0);
            } else {
                b.append(c);
            }
        }
        out.add(b.toString());
        return out.toArray(new String[0]);
    }
}
