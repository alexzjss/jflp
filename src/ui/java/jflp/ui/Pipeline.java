package jflp.ui;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.Consumer;

import jflp.report.Heuristic;
import jflp.report.Report;

/**
 * Fetching sources and running analyses, one job at a time (builds compete for
 * CPU and for the local Maven repository).
 *
 * A run: prepare, build, locate the measured classes, run the tests with the
 * agent attached through JAVA_TOOL_OPTIONS (no build file is changed), write the
 * report, analyze it. A failing test is data; a run fails only when the
 * toolchain does (no build, no coverage).
 */
final class Pipeline {

    static final String[] STEPS = { "prepare", "build", "classes", "test", "report", "analyze" };

    private final Workspace ws;
    private final Sources sources;
    private final File agentJar;
    private final ExecutorService worker = Executors.newSingleThreadExecutor(r -> {
        final Thread t = new Thread(r, "jflp-worker");
        t.setDaemon(true);
        return t;
    });
    private final Map<String, Exec.Handle> active = new ConcurrentHashMap<>();

    Pipeline(final Workspace ws, final Sources sources, final File agentJar) {
        this.ws = ws;
        this.sources = sources;
        this.agentJar = agentJar;
    }

    // ---- sources ----

    void acquire(final Map<String, Object> project) throws IOException {
        project.put("status", "acquiring");
        project.remove("error");
        ws.save(project);
        final String id = (String) project.get("id");
        final Exec.Handle h = new Exec.Handle();
        active.put(id, h);
        worker.submit(() -> {
            final File logFile = new File(ws.projectDir(id), "acquire.log");
            try (PrintWriter log = writer(logFile, false)) {
                try {
                    sources.acquire(project, line(log), h);
                    project.put("status", "ready");
                    log.println("ready: " + project.get("dir") + " (" + project.get("build") + ")");
                } catch (final Exception e) {
                    project.put("status", "error");
                    project.put("error", e.getMessage());
                    log.println("ERROR: " + e.getMessage());
                }
            } catch (final IOException e) {
                project.put("status", "error");
                project.put("error", e.getMessage());
            } finally {
                active.remove(id);
                try {
                    ws.save(project);
                } catch (final IOException ignored) {
                    // reported by the next save
                }
            }
        });
    }

    // ---- runs ----

    Map<String, Object> start(final Map<String, Object> project, final Map<String, Object> options) throws IOException {
        final Map<String, Object> run = new LinkedHashMap<>();
        final String id = ws.newId("r");
        run.put("id", id);
        run.put("projectId", project.get("id"));
        run.put("projectName", project.get("name"));
        run.put("created", Instant.now().toString());
        run.put("status", "queued");
        run.put("options", options);
        final List<Map<String, Object>> steps = new ArrayList<>();
        for (final String s : STEPS) {
            final Map<String, Object> st = new LinkedHashMap<>();
            st.put("id", s);
            st.put("status", "pending");
            steps.add(st);
        }
        run.put("steps", steps);
        ws.saveRun(run);
        final Exec.Handle h = new Exec.Handle();
        active.put(id, h);
        worker.submit(() -> execute(project, run, h));
        return run;
    }

    void cancel(final String id) {
        final Exec.Handle h = active.get(id);
        if (h != null) {
            h.cancel();
        }
    }

    private void execute(final Map<String, Object> project, final Map<String, Object> run, final Exec.Handle h) {
        final String id = (String) run.get("id");
        final File dir = ws.runDir(id);
        final long started = System.currentTimeMillis();
        try (PrintWriter log = writer(new File(dir, "log.txt"), true)) {
            final Consumer<String> out = line(log);
            run.put("status", "running");
            run.put("started", Instant.now().toString());
            ws.saveRun(run);
            try {
                steps(project, run, dir, out, h);
                run.put("status", h.cancelled() ? "cancelled" : "done");
            } catch (final Exception e) {
                run.put("status", h.cancelled() ? "cancelled" : "failed");
                run.put("error", e.getMessage());
                out.accept("ERROR: " + e.getMessage());
                for (final Map<String, Object> st : steps(run)) {
                    if ("running".equals(st.get("status"))) {
                        st.put("status", "failed");
                        st.put("ended", Instant.now().toString());
                    }
                }
            }
        } catch (final IOException e) {
            run.put("status", "failed");
            run.put("error", e.getMessage());
        } finally {
            active.remove(id);
            run.put("ended", Instant.now().toString());
            run.put("seconds", (System.currentTimeMillis() - started) / 1000.0);
            try {
                ws.saveRun(run);
            } catch (final IOException ignored) {
                // nothing better to do
            }
        }
    }

    private void steps(final Map<String, Object> project, final Map<String, Object> run, final File dir,
            final Consumer<String> out, final Exec.Handle h) throws Exception {
        @SuppressWarnings("unchecked")
        final Map<String, Object> opt = (Map<String, Object>) run.get("options");
        final String build = Sources.str(project.get("build"));
        final File root = new File(Sources.str(project.get("dir")));
        final String module = Sources.str(opt.get("module")).isEmpty() ? Sources.str(project.get("module"))
                : Sources.str(opt.get("module"));
        final File moduleDir = module.isEmpty() ? root : new File(root, module);

        // prepare
        begin(run, "prepare");
        final String jdk = jdk(opt, build);
        if (jdk == null) {
            throw new IOException("no JDK found; add one in Settings");
        }
        run.put("jdk", jdk);
        out.accept("project " + project.get("name") + " (" + build + ") at " + root);
        out.accept("JDK " + jdk + (module.isEmpty() ? "" : ", module " + module));
        end(run, "prepare", "JDK " + Env.version(new File(jdk)));

        // build
        begin(run, "build");
        final Map<String, String> env = new LinkedHashMap<>();
        env.put("JAVA_HOME", jdk);
        env.put("PATH", jdk + File.separator + "bin" + File.pathSeparator + System.getenv("PATH"));
        if ("defects4j".equals(build)) {
            env.putAll(Sources.d4jEnv()); // Defects4J insists on Java 11
        }
        final List<String> buildCmd = buildCommand(build, opt, module, root);
        if (buildCmd != null) {
            out.accept("$ " + String.join(" ", buildCmd));
            final int code = Exec.run(buildCmd, root, env, timeout(opt), out, h);
            cancelled(h);
            if (code != 0) {
                throw new IOException("build failed (exit " + code + "); see the log");
            }
        }
        end(run, "build", buildCmd == null ? "skipped" : "ok");

        // classes
        begin(run, "classes");
        final List<File> classDirs = classDirs(build, opt, root, moduleDir, env);
        if (classDirs.isEmpty()) {
            throw new IOException("no compiled classes found; set the class directories in the run options");
        }
        final StringBuilder cd = new StringBuilder();
        for (final File f : classDirs) {
            cd.append(cd.length() == 0 ? "" : File.pathSeparator).append(f.getPath());
            out.accept("measuring classes in " + f);
        }
        run.put("classDirs", cd.toString());
        end(run, "classes", classDirs.size() + " folder(s)");

        // test
        begin(run, "test");
        final File data = new File(dir, "data");
        Workspace.deleteTree(data);
        // Options go to a file: JDK 8 and older silently ignore a JAVA_TOOL_OPTIONS
        // longer than 1024 characters, and these paths are long.
        final StringBuilder opts = new StringBuilder("destdir=").append(data.getPath()).append("\nclassdirs=")
                .append(cd);
        if ("jacoco".equals(opt.get("lines"))) {
            opts.append("\nlines=jacoco");
        }
        if (Boolean.TRUE.equals(opt.get("branches"))) {
            opts.append("\nbranches=true");
        }
        if (Boolean.TRUE.equals(opt.get("duas"))) {
            opts.append("\nduas=true");
        }
        final File optsFile = new File(dir, "agent.options");
        java.nio.file.Files.write(optsFile.toPath(), opts.toString().getBytes(StandardCharsets.UTF_8));
        final String agent = "-javaagent:" + agentJar.getPath() + "=@" + optsFile.getPath();
        final String inherited = System.getenv("JAVA_TOOL_OPTIONS");
        final String toolOptions = (inherited == null ? "" : inherited + " ") + agent;
        env.put("JAVA_TOOL_OPTIONS", toolOptions);
        if (toolOptions.length() > 1024 && Env.major(new File(jdk)) <= 8) {
            out.accept("WARNING: JAVA_TOOL_OPTIONS is " + toolOptions.length() + " characters; JDK 8 and older ignore it"
                    + " above 1024, so the agent would not load. Shorten the inherited JAVA_TOOL_OPTIONS or use JDK 9+.");
        }
        final List<String> testCmd = testCommand(build, opt, module, root);
        out.accept("$ " + String.join(" ", testCmd));
        out.accept("(agent: " + agent + ": " + opts.toString().replace('\n', ',') + ")");
        final File testCwd = "maven".equals(build) && !module.isEmpty() ? moduleDir : root;
        final int code = Exec.run(testCmd, testCwd, env, timeout(opt), out, h);
        cancelled(h);
        final File[] files = data.listFiles();
        if (files == null || files.length == 0) {
            throw new IOException("the tests produced no coverage (exit " + code + "): did they run? see the log");
        }
        end(run, "test", "exit " + code + (code == 0 ? "" : " (failing tests are expected: they are the data)"));

        // report
        begin(run, "report");
        final Report.Options ro = new Report.Options();
        ro.dataDir = data;
        ro.outDir = new File(dir, "report");
        ro.allLines = true;
        ro.heuristic = Heuristic.byName(Sources.str(opt.get("heuristic")).isEmpty() ? "ochiai"
                : Sources.str(opt.get("heuristic")));
        ro.classDirs.addAll(classDirs);
        final Report.Summary s = Report.run(ro);
        for (final String w : s.warnings) {
            out.accept("WARNING: " + w);
        }
        end(run, "report", s.passed + s.failed + " tests, " + s.failed + " failing, " + s.lines + " lines");

        // analyze
        begin(run, "analyze");
        final Map<String, Object> summary = new Results(ws, project, run).summary();
        run.put("summary", summary);
        end(run, "analyze", "ok");
    }

    private String jdk(final Map<String, Object> opt, final String build) {
        final String chosen = Sources.str(opt.get("jdk"));
        if (!chosen.isEmpty()) {
            return chosen;
        }
        final Map<String, Object> settings = ws.settings();
        if ("defects4j".equals(build)) {
            return Env.jdkFor(settings, 11);
        }
        final String dflt = Sources.str(settings.get("defaultJdk"));
        if (!dflt.isEmpty()) {
            return dflt;
        }
        final String eleven = Env.jdkFor(settings, 11);
        return eleven != null ? eleven : Env.jdkFor(settings, 1);
    }

    private static List<String> buildCommand(final String build, final Map<String, Object> opt, final String module,
            final File root) {
        final String custom = Sources.str(opt.get("buildCommand"));
        if (!custom.isEmpty()) {
            return shell(custom);
        }
        switch (build) {
        case "maven": {
            final List<String> c = new ArrayList<>(Arrays.asList(Env.exe("mvn"), "-B"));
            c.addAll(MAVEN_SKIPS);
            if (module.isEmpty()) {
                c.add("test-compile");
            } else {
                c.addAll(Arrays.asList("-DskipTests", "-Dmaven.test.failure.ignore=true", "install", "-pl", module, "-am"));
            }
            return c;
        }
        case "gradle":
            return new ArrayList<>(Arrays.asList(gradle(root), "--no-daemon", "-q", "testClasses"));
        case "defects4j":
            return new ArrayList<>(Arrays.asList(Env.defects4jPath(), "compile"));
        case "ant":
            return new ArrayList<>(Arrays.asList(Env.exe("ant"), "compile"));
        default:
            return null;
        }
    }

    private static List<String> testCommand(final String build, final Map<String, Object> opt, final String module,
            final File root) throws IOException {
        final String custom = Sources.str(opt.get("testCommand"));
        if (!custom.isEmpty()) {
            return shell(custom);
        }
        switch (build) {
        case "maven": {
            final List<String> c = new ArrayList<>(Arrays.asList(Env.exe("mvn"), "-B"));
            c.addAll(MAVEN_SKIPS);
            c.addAll(Arrays.asList("-Dmaven.test.failure.ignore=true", "-DfailIfNoTests=false",
                    "-Dsurefire.failIfNoSpecifiedTests=false", "test"));
            return c;
        }
        case "gradle":
            return new ArrayList<>(Arrays.asList(gradle(root), "--no-daemon", "test", "--continue"));
        case "defects4j":
            return new ArrayList<>(Arrays.asList(Env.defects4jPath(), "test"));
        case "ant":
            return new ArrayList<>(Arrays.asList(Env.exe("ant"), "test"));
        default:
            throw new IOException("set a test command in the run options for this project");
        }
    }

    static final List<String> MAVEN_SKIPS = Arrays.asList("-Drat.skip", "-Dcheckstyle.skip", "-Danimal.sniffer.skip",
            "-Denforcer.skip", "-Dlicense.skip", "-Djacoco.skip", "-Dmaven.javadoc.skip", "-Dspotbugs.skip",
            "-Dpmd.skip");

    private List<File> classDirs(final String build, final Map<String, Object> opt, final File root,
            final File moduleDir, final Map<String, String> env) {
        final List<File> out = new ArrayList<>();
        final String custom = Sources.str(opt.get("classDirs"));
        if (!custom.isEmpty()) {
            for (final String c : custom.split("[,;\\n]")) {
                if (!c.trim().isEmpty()) {
                    final File f = new File(c.trim());
                    out.add(f.isAbsolute() ? f : new File(root, c.trim()));
                }
            }
            return out;
        }
        if ("defects4j".equals(build)) {
            final String rel = Exec.output(Arrays.asList(Env.defects4jPath(), "export", "-p", "dir.bin.classes"), root,
                    env, 300);
            if (rel != null && !rel.trim().isEmpty()) {
                out.add(new File(root, rel.trim()));
            }
            return out;
        }
        find(moduleDir, out, 0);
        return out;
    }

    /** target/classes (Maven), build/classes/{java,kotlin,groovy}/main (Gradle), build/classes (Ant). */
    private static void find(final File dir, final List<File> out, final int depth) {
        if (depth > 6 || !dir.isDirectory() || dir.getName().startsWith(".") || dir.getName().equals("node_modules")) {
            return;
        }
        // Only folders that are modules of the build (a pom.xml or build.gradle next to them).
        final File mvn = new File(dir, "target/classes");
        if (new File(dir, "pom.xml").isFile() && mvn.isDirectory() && hasClasses(mvn)) {
            out.add(mvn);
        }
        final boolean gradle = new File(dir, "build.gradle").isFile() || new File(dir, "build.gradle.kts").isFile();
        for (final String lang : new String[] { "java", "kotlin", "groovy", "scala" }) {
            final File g = new File(dir, "build/classes/" + lang + "/main");
            if (gradle && g.isDirectory() && hasClasses(g)) {
                out.add(g);
            }
        }
        final File[] subs = dir.listFiles(File::isDirectory);
        if (subs == null) {
            return;
        }
        Arrays.sort(subs);
        for (final File s : subs) {
            if (!s.getName().equals("target") && !s.getName().equals("build") && !s.getName().equals("src")) {
                find(s, out, depth + 1);
            }
        }
    }

    private static boolean hasClasses(final File dir) {
        final File[] fs = dir.listFiles();
        if (fs == null) {
            return false;
        }
        for (final File f : fs) {
            if (f.getName().endsWith(".class") || f.isDirectory() && hasClasses(f)) {
                return true;
            }
        }
        return false;
    }

    private static String gradle(final File root) {
        final File wrapper = new File(root, Exec.windows() ? "gradlew.bat" : "gradlew");
        return wrapper.isFile() ? wrapper.getPath() : Env.exe("gradle");
    }

    private static List<String> shell(final String cmd) {
        return Exec.windows() ? Arrays.asList("cmd", "/c", cmd) : Arrays.asList("sh", "-c", cmd);
    }

    private static long timeout(final Map<String, Object> opt) {
        final Object t = opt.get("timeoutMinutes");
        return (t instanceof Number ? ((Number) t).longValue() : 120) * 60;
    }

    private static void cancelled(final Exec.Handle h) throws IOException {
        if (h.cancelled()) {
            throw new IOException("cancelled");
        }
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> steps(final Map<String, Object> run) {
        return (List<Map<String, Object>>) run.get("steps");
    }

    private void begin(final Map<String, Object> run, final String step) throws IOException {
        for (final Map<String, Object> s : steps(run)) {
            if (step.equals(s.get("id"))) {
                s.put("status", "running");
                s.put("started", Instant.now().toString());
            }
        }
        ws.saveRun(run);
    }

    private void end(final Map<String, Object> run, final String step, final String detail) throws IOException {
        for (final Map<String, Object> s : steps(run)) {
            if (step.equals(s.get("id"))) {
                s.put("status", "done");
                s.put("ended", Instant.now().toString());
                s.put("detail", detail);
            }
        }
        ws.saveRun(run);
    }

    private static PrintWriter writer(final File f, final boolean append) throws IOException {
        Workspace.mkdirs(f.getParentFile());
        return new PrintWriter(new OutputStreamWriter(new FileOutputStream(f, append), StandardCharsets.UTF_8), true);
    }

    private static Consumer<String> line(final PrintWriter log) {
        return l -> {
            synchronized (log) {
                log.println(l);
            }
        };
    }
}
