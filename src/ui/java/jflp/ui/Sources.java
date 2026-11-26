package jflp.ui;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Turns a source description into a checkout the runs can work on:
 *
 * <ul>
 * <li>{@code git}: any repository URL, an optional ref, and an optional fix ref
 * (the diff from the checkout to it marks the buggy lines);</li>
 * <li>{@code local}: a folder, used in place;</li>
 * <li>{@code defects4j}: project and bug id, through the defects4j command;</li>
 * <li>{@code bugsjar}: project and issue, a branch of github.com/bugs-dot-jar;</li>
 * <li>{@code bears}: a Bears bug id, a branch of bears-bugs/bears-benchmark.</li>
 * </ul>
 *
 * Fills in the project: {@code dir}, {@code build}, {@code module},
 * {@code truth} ({patch, side}) and {@code expectedFailing} when known.
 */
final class Sources {

    static final List<String> BUGSJAR_PROJECTS = Arrays.asList("accumulo", "camel", "commons-math", "flink",
            "jackrabbit-oak", "logging-log4j2", "maven", "wicket");

    private final Workspace ws;

    Sources(final Workspace ws) {
        this.ws = ws;
    }

    void acquire(final Map<String, Object> p, final Consumer<String> log, final Exec.Handle h) throws Exception {
        @SuppressWarnings("unchecked")
        final Map<String, Object> src = (Map<String, Object>) p.get("source");
        final String type = str(src.get("type"));
        final File pdir = ws.projectDir(str(p.get("id")));
        final File checkout = new File(pdir, "src");
        switch (type) {
        case "local": {
            final File dir = new File(str(src.get("path"))).getAbsoluteFile();
            if (!dir.isDirectory()) {
                throw new IOException("folder not found: " + dir);
            }
            log.accept("using " + dir + " in place (builds write into its target/ or build/)");
            p.put("dir", dir.getPath());
            detectBuild(p, dir);
            break;
        }
        case "git": {
            final String url = str(src.get("url"));
            final String ref = str(src.get("ref"));
            final String fix = str(src.get("fixRef"));
            Workspace.deleteTree(checkout);
            run(Arrays.asList("git", "clone", "--quiet", url, checkout.getPath()), pdir, log, h, "git clone");
            if (!ref.isEmpty()) {
                run(Arrays.asList("git", "checkout", "--quiet", ref), checkout, log, h, "git checkout " + ref);
            }
            p.put("dir", checkout.getPath());
            p.put("commit", gitHead(checkout));
            if (!fix.isEmpty()) {
                final File patch = new File(pdir, "fix.diff");
                final String diff = Exec.output(Arrays.asList("git", "diff", "HEAD", fix, "--", "*.java"), checkout, null,
                        120);
                if (diff == null) {
                    throw new IOException("cannot diff against fix ref " + fix);
                }
                Files.write(patch.toPath(), diff.getBytes(StandardCharsets.UTF_8));
                p.put("truth", truth(patch, "-"));
                log.accept("fix ref " + fix + ": " + Truth.patchLines(patch, '-').size() + " buggy lines");
            }
            detectBuild(p, checkout);
            if (!str(src.get("module")).isEmpty()) {
                p.put("module", str(src.get("module")));
            }
            break;
        }
        case "defects4j": {
            final String d4j = Env.defects4j(ws.settings());
            if (d4j == null) {
                throw new IOException("Defects4J not found: set its home in Settings");
            }
            final String project = str(src.get("project"));
            final String bug = str(src.get("bug"));
            Workspace.deleteTree(checkout);
            run(Arrays.asList(d4j, "checkout", "-p", project, "-v", bug + "b", "-w", checkout.getPath()), pdir, log, h,
                    "defects4j checkout");
            p.put("dir", checkout.getPath());
            p.put("build", "defects4j");
            final File home = new File(d4j).getParentFile().getParentFile();
            final File patch = new File(home, "projects/" + project + "/patches/" + bug + ".src.patch");
            if (patch.isFile()) {
                p.put("truth", truth(patch, "+"));
            }
            final String trigger = Exec.output(Arrays.asList(d4j, "export", "-p", "tests.trigger"), checkout,
                    d4jEnv(), 300);
            if (trigger != null) {
                final List<String> tests = new ArrayList<>();
                for (final String t : trigger.split("\\s+")) {
                    if (t.contains("::")) {
                        final String[] cm = t.split("::");
                        tests.add(cm[1] + "(" + cm[0] + ")");
                    }
                }
                p.put("expectedFailing", tests);
            }
            break;
        }
        case "bugsjar": {
            final String project = str(src.get("project"));
            final String issue = str(src.get("issue"));
            final String url = "https://github.com/bugs-dot-jar/" + project;
            final String branch = bugsjarBranch(url, issue);
            if (branch == null) {
                throw new IOException("no Bugs.jar branch for " + issue + " in " + project);
            }
            Workspace.deleteTree(checkout);
            run(Arrays.asList("git", "clone", "--quiet", "--depth", "1", "--branch", branch, url, checkout.getPath()),
                    pdir, log, h, "git clone " + branch);
            p.put("dir", checkout.getPath());
            final File meta = new File(checkout, ".bugs-dot-jar");
            final File patch = new File(meta, "developer-patch.diff");
            p.put("truth", truth(patch, "-"));
            p.put("module", moduleOf(patch));
            p.put("expectedFailing", bugsjarFailing(new File(meta, "test-results.txt")));
            detectBuild(p, checkout);
            break;
        }
        case "bears": {
            final String bugId = str(src.get("bug"));
            final String branch = bearsBranches(log, h).get(bugId);
            if (branch == null) {
                throw new IOException("unknown Bears bug " + bugId);
            }
            Workspace.deleteTree(checkout);
            run(Arrays.asList("git", "clone", "--quiet", "--depth", "6", "--single-branch", "--branch", branch,
                    "https://github.com/bears-bugs/bears-benchmark", checkout.getPath()), pdir, log, h, "git clone");
            final String history = Exec.output(Arrays.asList("git", "log", "--format=%H %s"), checkout, null, 60);
            String buggy = null;
            String fixed = null;
            for (final String l : history == null ? new String[0] : history.split("\n")) {
                if (l.contains(" Changes in the tests") && buggy == null) {
                    buggy = l.split(" ")[0];
                }
                if (l.contains(" Human patch") && fixed == null) {
                    fixed = l.split(" ")[0];
                }
            }
            if (buggy == null && history != null) {
                for (final String l : history.split("\n")) {
                    if (l.contains(" Bug commit")) {
                        buggy = l.split(" ")[0];
                        break;
                    }
                }
            }
            if (buggy == null || fixed == null) {
                throw new IOException("unexpected commit structure on " + branch);
            }
            final File meta = new File(pdir, "bears.json");
            final String json = Exec.output(Arrays.asList("git", "show", "HEAD:bears.json"), checkout, null, 60);
            if (json != null) {
                Files.write(meta.toPath(), json.getBytes(StandardCharsets.UTF_8));
            }
            final File patch = new File(pdir, "fix.diff");
            final String diff = Exec.output(Arrays.asList("git", "diff", buggy, fixed, "--", "*.java"), checkout, null, 60);
            Files.write(patch.toPath(), (diff == null ? "" : diff).getBytes(StandardCharsets.UTF_8));
            run(Arrays.asList("git", "checkout", "--quiet", buggy), checkout, log, h, "git checkout");
            p.put("dir", checkout.getPath());
            p.put("truth", truth(patch, "-"));
            if (json != null) {
                final Map<String, Object> b = Json.object(json);
                @SuppressWarnings("unchecked")
                final Map<String, Object> tests = (Map<String, Object>) b.get("tests");
                final String[] parts = str(tests.get("failingModule")).replaceAll("^/+", "").split("/");
                p.put("module", parts.length > 5 ? String.join("/", Arrays.copyOfRange(parts, 5, parts.length)) : "");
                final List<String> failing = new ArrayList<>();
                final Object details = tests.get("failureDetails");
                if (details instanceof List) {
                    for (final Object o : (List<?>) details) {
                        @SuppressWarnings("unchecked")
                        final Map<String, Object> d = (Map<String, Object>) o;
                        failing.add(d.get("testMethod") + "(" + d.get("testClass") + ")");
                    }
                }
                p.put("expectedFailing", failing);
            }
            detectBuild(p, checkout);
            break;
        }
        default:
            throw new IllegalArgumentException("unknown source type '" + type + "'");
        }
    }

    // ---- benchmark catalogs ----

    List<String> defects4jProjects() throws IOException {
        final String d4j = Env.defects4j(ws.settings());
        if (d4j == null) {
            throw new IOException("Defects4J not found: set its home in Settings");
        }
        final String out = Exec.output(Arrays.asList(d4j, "pids"), null, d4jEnv(), 120);
        return out == null ? new ArrayList<String>() : lines(out);
    }

    List<String> defects4jBugs(final String project) throws IOException {
        final String d4j = Env.defects4j(ws.settings());
        if (d4j == null) {
            throw new IOException("Defects4J not found: set its home in Settings");
        }
        final String out = Exec.output(Arrays.asList(d4j, "bids", "-p", project), null, d4jEnv(), 120);
        return out == null ? new ArrayList<String>() : lines(out);
    }

    /** Issues of a Bugs.jar project, from its branch names (bugs-dot-jar_ISSUE_sha). */
    List<String> bugsjarIssues(final String project) {
        final String out = Exec.output(Arrays.asList("git", "ls-remote", "--heads",
                "https://github.com/bugs-dot-jar/" + project), null, null, 120);
        final List<String> l = new ArrayList<>();
        if (out != null) {
            final Matcher m = Pattern.compile("refs/heads/bugs-dot-jar_([A-Z0-9]+-\\d+)_").matcher(out);
            while (m.find()) {
                if (!l.contains(m.group(1))) {
                    l.add(m.group(1));
                }
            }
        }
        l.sort((a, b) -> {
            final int c = a.replaceAll("-\\d+$", "").compareTo(b.replaceAll("-\\d+$", ""));
            return c != 0 ? c : Integer.compare(num(a), num(b));
        });
        return l;
    }

    /** Bears bug id -> branch, from the benchmark's index (cached in the workspace). */
    Map<String, String> bearsBranches(final Consumer<String> log, final Exec.Handle h) throws Exception {
        final File cache = new File(ws.root, "cache/bears-benchmark");
        if (!new File(cache, "scripts/data/bug_id_and_branch.json").isFile()) {
            Workspace.deleteTree(cache);
            Workspace.mkdirs(cache.getParentFile());
            run(Arrays.asList("git", "clone", "--quiet", "--depth", "1", "https://github.com/bears-bugs/bears-benchmark",
                    cache.getPath()), cache.getParentFile(), log, h, "git clone bears-benchmark");
        }
        final Object index = Json.parse(new String(Files.readAllBytes(
                new File(cache, "scripts/data/bug_id_and_branch.json").toPath()), StandardCharsets.UTF_8));
        final Map<String, String> m = new LinkedHashMap<>();
        for (final Object o : (List<?>) index) {
            @SuppressWarnings("unchecked")
            final Map<String, Object> b = (Map<String, Object>) o;
            m.put(str(b.get("bugId")), str(b.get("bugBranch")));
        }
        return m;
    }

    // ---- helpers ----

    static Map<String, String> d4jEnv() {
        // Defects4J 3 insists on Java 11 on the PATH.
        final Map<String, String> env = new LinkedHashMap<>();
        final String jdk11 = Env.jdkFor(new LinkedHashMap<String, Object>(), 11);
        if (jdk11 != null && Env.major(new File(jdk11)) == 11) {
            env.put("JAVA_HOME", jdk11);
            env.put("PATH", jdk11 + File.separator + "bin" + File.pathSeparator + System.getenv("PATH"));
        }
        return env;
    }

    static void detectBuild(final Map<String, Object> p, final File dir) {
        if (p.get("build") != null && !"".equals(p.get("build"))) {
            return;
        }
        if (new File(dir, "pom.xml").isFile()) {
            p.put("build", "maven");
        } else if (new File(dir, "build.gradle").isFile() || new File(dir, "build.gradle.kts").isFile()) {
            p.put("build", "gradle");
        } else if (new File(dir, "build.xml").isFile()) {
            p.put("build", "ant");
        } else {
            p.put("build", "custom");
        }
    }

    private static Map<String, Object> truth(final File patch, final String side) {
        final Map<String, Object> t = new LinkedHashMap<>();
        t.put("patch", patch.getPath());
        t.put("side", side);
        return t;
    }

    private static String bugsjarBranch(final String url, final String issue) {
        final String out = Exec.output(Arrays.asList("git", "ls-remote", "--heads", url), null, null, 120);
        if (out == null) {
            return null;
        }
        final Matcher m = Pattern.compile("refs/heads/(bugs-dot-jar_" + Pattern.quote(issue) + "_[0-9a-f]+)").matcher(out);
        return m.find() ? m.group(1) : null;
    }

    static List<String> bugsjarFailing(final File results) throws IOException {
        final List<String> out = new ArrayList<>();
        if (!results.isFile()) {
            return out;
        }
        boolean section = false;
        for (final String line : Files.readAllLines(results.toPath(), StandardCharsets.ISO_8859_1)) {
            if (line.startsWith("Failed tests:") || line.startsWith("Tests in error:")) {
                section = true;
                continue;
            }
            if (!section) {
                continue;
            }
            final Matcher old = Pattern.compile("^\\s+(\\w+)\\(([\\w.$]+)\\)").matcher(line);
            final Matcher neu = Pattern.compile("^\\s+([\\w$]+)\\.(\\w+)[:(]").matcher(line);
            if (old.find()) {
                out.add(old.group(1) + "(" + old.group(2) + ")");
            } else if (neu.find()) {
                out.add(neu.group(2) + "(" + neu.group(1) + ")");
            } else if (line.trim().isEmpty() || line.startsWith("Tests run:")) {
                section = false;
            }
        }
        return out;
    }

    static String moduleOf(final File patch) throws IOException {
        for (final String line : Files.readAllLines(patch.toPath(), StandardCharsets.UTF_8)) {
            if (line.startsWith("--- a/") && ("/" + line.substring(6)).contains("/src/main/java/")) {
                final String p = line.substring(6).trim();
                return p.substring(0, p.indexOf("src/main/java/")).replaceAll("/+$", "");
            }
        }
        return "";
    }

    private static String gitHead(final File dir) {
        final String out = Exec.output(Arrays.asList("git", "rev-parse", "--short", "HEAD"), dir, null, 30);
        return out == null ? null : out.trim();
    }

    private static void run(final List<String> cmd, final File cwd, final Consumer<String> log, final Exec.Handle h,
            final String what) throws Exception {
        log.accept("$ " + String.join(" ", cmd));
        final int code = Exec.run(cmd, cwd, d4jLike(cmd) ? d4jEnv() : null, 3600, log, h);
        if (code != 0) {
            throw new IOException(what + " failed (exit " + code + ")");
        }
    }

    private static boolean d4jLike(final List<String> cmd) {
        return cmd.get(0).endsWith("defects4j");
    }

    private static List<String> lines(final String out) {
        final List<String> l = new ArrayList<>();
        for (final String s : out.split("\n")) {
            if (!s.trim().isEmpty()) {
                l.add(s.trim());
            }
        }
        return l;
    }

    private static int num(final String issue) {
        final Matcher m = Pattern.compile("(\\d+)$").matcher(issue);
        return m.find() ? Integer.parseInt(m.group(1)) : 0;
    }

    static String str(final Object o) {
        return o == null ? "" : String.valueOf(o).trim();
    }
}
