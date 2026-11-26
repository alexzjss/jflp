package jflp.ui;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Projects and runs, persisted as JSON under the workspace directory:
 *
 * <pre>
 * settings.json
 * projects/&lt;id&gt;/project.json   source, build system, checkout location, status
 * projects/&lt;id&gt;/acquire.log    clone/checkout output
 * projects/&lt;id&gt;/src/          checkout (not used for local folders)
 * runs/&lt;id&gt;/run.json           options, status, steps, summary
 * runs/&lt;id&gt;/log.txt            build and test output
 * runs/&lt;id&gt;/data/              the agent's coverage files
 * runs/&lt;id&gt;/report/            jflp report output
 * </pre>
 *
 * Projects and runs are plain maps, the same shape the API returns.
 */
final class Workspace {

    final File root;
    private final Map<String, Map<String, Object>> projects = new LinkedHashMap<>();
    private final Map<String, Map<String, Object>> runs = new LinkedHashMap<>();
    private Map<String, Object> settings;
    private long lastId;

    Workspace(final File root) throws IOException {
        this.root = root;
        mkdirs(new File(root, "projects"));
        mkdirs(new File(root, "runs"));
        settings = load(new File(root, "settings.json"));
        if (settings == null) {
            settings = new LinkedHashMap<>();
        }
        for (final File d : list(new File(root, "projects"))) {
            final Map<String, Object> p = load(new File(d, "project.json"));
            if (p != null) {
                if ("acquiring".equals(p.get("status"))) {
                    p.put("status", "error");
                    p.put("error", "interrupted: the UI stopped while the source was being fetched");
                }
                projects.put((String) p.get("id"), p);
            }
        }
        for (final File d : list(new File(root, "runs"))) {
            final Map<String, Object> r = load(new File(d, "run.json"));
            if (r != null) {
                final Object st = r.get("status");
                if ("running".equals(st) || "queued".equals(st)) {
                    r.put("status", "failed");
                    r.put("error", "interrupted: the UI stopped during the run");
                }
                runs.put((String) r.get("id"), r);
            }
        }
    }

    synchronized String newId(final String prefix) {
        long t = System.currentTimeMillis();
        if (t <= lastId) {
            t = lastId + 1;
        }
        lastId = t;
        return prefix + Long.toString(t, 36);
    }

    // ---- projects ----

    synchronized List<Map<String, Object>> projects() {
        final List<Map<String, Object>> l = new ArrayList<>(projects.values());
        l.sort(Comparator.comparing((Map<String, Object> p) -> String.valueOf(p.get("created"))).reversed());
        return l;
    }

    synchronized Map<String, Object> project(final String id) {
        return projects.get(id);
    }

    synchronized void save(final Map<String, Object> project) throws IOException {
        projects.put((String) project.get("id"), project);
        store(new File(projectDir((String) project.get("id")), "project.json"), project);
    }

    synchronized void deleteProject(final String id) throws IOException {
        final Map<String, Object> p = projects.remove(id);
        if (p == null) {
            return;
        }
        for (final Map<String, Object> r : new ArrayList<>(runs.values())) {
            if (id.equals(r.get("projectId"))) {
                runs.remove(r.get("id"));
                deleteTree(runDir((String) r.get("id")));
            }
        }
        deleteTree(projectDir(id));
    }

    File projectDir(final String id) {
        return new File(new File(root, "projects"), id);
    }

    // ---- runs ----

    synchronized List<Map<String, Object>> runs(final String projectId) {
        final List<Map<String, Object>> l = new ArrayList<>();
        for (final Map<String, Object> r : runs.values()) {
            if (projectId == null || projectId.equals(r.get("projectId"))) {
                l.add(r);
            }
        }
        l.sort(Comparator.comparing((Map<String, Object> r) -> String.valueOf(r.get("created"))).reversed());
        return l;
    }

    synchronized Map<String, Object> run(final String id) {
        return runs.get(id);
    }

    synchronized void saveRun(final Map<String, Object> run) throws IOException {
        runs.put((String) run.get("id"), run);
        store(new File(runDir((String) run.get("id")), "run.json"), run);
    }

    File runDir(final String id) {
        return new File(new File(root, "runs"), id);
    }

    // ---- settings ----

    synchronized Map<String, Object> settings() {
        return new LinkedHashMap<>(settings);
    }

    synchronized void saveSettings(final Map<String, Object> s) throws IOException {
        settings = new LinkedHashMap<>(s);
        store(new File(root, "settings.json"), settings);
    }

    // ---- files ----

    static Map<String, Object> load(final File f) {
        if (!f.isFile()) {
            return null;
        }
        try {
            return Json.object(new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8));
        } catch (final Exception e) {
            return null;
        }
    }

    static void store(final File f, final Map<String, Object> m) throws IOException {
        mkdirs(f.getParentFile());
        final File tmp = new File(f.getPath() + ".tmp");
        Files.write(tmp.toPath(), Json.write(m).getBytes(StandardCharsets.UTF_8));
        if (!tmp.renameTo(f)) {
            Files.copy(tmp.toPath(), f.toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING);
            tmp.delete();
        }
    }

    static void mkdirs(final File d) throws IOException {
        if (!d.isDirectory() && !d.mkdirs()) {
            throw new IOException("cannot create " + d);
        }
    }

    static void deleteTree(final File f) {
        if (f.isDirectory() && !Files.isSymbolicLink(f.toPath())) {
            for (final File c : list(f)) {
                deleteTree(c);
            }
        }
        f.delete();
    }

    private static List<File> list(final File d) {
        final File[] fs = d.listFiles();
        final List<File> l = new ArrayList<>();
        if (fs != null) {
            for (final File f : fs) {
                l.add(f);
            }
        }
        return l;
    }
}
