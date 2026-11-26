package jflp.ui;

import java.awt.Desktop;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.SecureRandom;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import jflp.report.Heuristic;

/**
 * {@code java -jar jflp.jar ui}: a local web UI over the agent and the report.
 *
 * Listens on 127.0.0.1 only. The UI runs build commands, so a request must name
 * this host (no DNS rebinding) and every request that changes something must
 * carry the session token, which only a page served from here can read.
 */
public final class UiServer {

    private final Workspace ws;
    private final Pipeline pipeline;
    private final Sources sources;
    private final String token;
    private int port;

    private UiServer(final Workspace ws, final File agentJar) {
        this.ws = ws;
        this.sources = new Sources(ws);
        this.pipeline = new Pipeline(ws, sources, agentJar);
        final byte[] b = new byte[18];
        new SecureRandom().nextBytes(b);
        final StringBuilder t = new StringBuilder();
        for (final byte x : b) {
            t.append(String.format("%02x", x));
        }
        this.token = t.toString();
    }

    public static void main(final String[] args) throws Exception {
        int port = 7070;
        File workspace = new File(System.getProperty("user.home"), ".jflp");
        boolean browser = true;
        for (int i = 0; i < args.length; i++) {
            if ("--port".equals(args[i])) {
                port = Integer.parseInt(args[++i]);
            } else if ("--workspace".equals(args[i])) {
                workspace = new File(args[++i]);
            } else if ("--no-browser".equals(args[i])) {
                browser = false;
            } else {
                throw new IllegalArgumentException("unknown option " + args[i]);
            }
        }
        final File jar = new File(UiServer.class.getProtectionDomain().getCodeSource().getLocation().toURI());
        if (!jar.isFile()) {
            throw new IllegalStateException("run the UI from jflp.jar (java -jar jflp.jar ui): the agent is the jar");
        }
        final UiServer s = new UiServer(new Workspace(workspace.getAbsoluteFile()), jar);
        s.start(port);
        final String url = "http://127.0.0.1:" + s.port + "/";
        System.out.println("JFLP UI: " + url + "  (workspace " + workspace.getAbsolutePath() + ", Ctrl+C to stop)");
        if (browser && Desktop.isDesktopSupported()) {
            try {
                Desktop.getDesktop().browse(new URI(url));
            } catch (final Exception ignored) {
                // headless: the URL is printed
            }
        }
    }

    private void start(final int wanted) throws IOException {
        Env.settings = ws.settings();
        final HttpServer server = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), wanted), 64);
        server.createContext("/", this::handle);
        server.setExecutor(Executors.newFixedThreadPool(8));
        server.start();
        port = server.getAddress().getPort();
    }

    // ---- routing ----

    private void handle(final HttpExchange x) throws IOException {
        try {
            final String host = x.getRequestHeaders().getFirst("Host");
            if (host == null || !(host.equals("127.0.0.1:" + port) || host.equals("localhost:" + port))) {
                send(x, 403, "text/plain", "forbidden host".getBytes(StandardCharsets.UTF_8));
                return;
            }
            final String path = x.getRequestURI().getPath();
            if (path.startsWith("/api/")) {
                if (!"GET".equals(x.getRequestMethod()) && !token.equals(x.getRequestHeaders().getFirst("X-JFLP-Token"))) {
                    json(x, 403, error("missing or wrong session token"));
                    return;
                }
                api(x, path.substring(5).split("/"));
            } else {
                serveStatic(x, path);
            }
        } catch (final ApiError e) {
            json(x, e.status, error(e.getMessage()));
        } catch (final Exception e) {
            json(x, 500, error(e.toString()));
        } finally {
            x.close();
        }
    }

    private static final class ApiError extends RuntimeException {
        final int status;

        ApiError(final int status, final String message) {
            super(message);
            this.status = status;
        }
    }

    private void api(final HttpExchange x, final String[] p) throws Exception {
        final String m = x.getRequestMethod();
        final Map<String, String> q = query(x);
        final String r0 = p.length > 0 ? p[0] : "";
        if (r0.equals("session")) {
            final Map<String, Object> s = new LinkedHashMap<>();
            s.put("token", token);
            s.put("workspace", ws.root.getPath());
            s.put("heuristics", heuristics());
            json(x, 200, s);
        } else if (r0.equals("env")) {
            final Map<String, Object> e = new LinkedHashMap<>();
            final Map<String, Object> settings = ws.settings();
            e.put("jdks", Env.jdks(settings));
            e.put("tools", Env.tools(settings));
            e.put("settings", settings);
            e.put("java", System.getProperty("java.version"));
            e.put("os", System.getProperty("os.name") + " " + System.getProperty("os.arch"));
            json(x, 200, e);
        } else if (r0.equals("settings") && m.equals("PUT")) {
            final Map<String, Object> s = body(x);
            ws.saveSettings(s);
            Env.settings = ws.settings();
            json(x, 200, s);
        } else if (r0.equals("projects")) {
            projects(x, m, p, q);
        } else if (r0.equals("runs")) {
            runs(x, m, p, q);
        } else if (r0.equals("catalog")) {
            catalog(x, p, q);
        } else {
            throw new ApiError(404, "no such endpoint");
        }
    }

    private void projects(final HttpExchange x, final String m, final String[] p, final Map<String, String> q)
            throws Exception {
        if (p.length == 1 && m.equals("GET")) {
            final List<Map<String, Object>> l = new ArrayList<>();
            for (final Map<String, Object> pr : ws.projects()) {
                final Map<String, Object> v = new LinkedHashMap<>(pr);
                final List<Map<String, Object>> runs = ws.runs((String) pr.get("id"));
                v.put("runs", runs.size());
                v.put("lastRun", runs.isEmpty() ? null : brief(runs.get(0)));
                l.add(v);
            }
            json(x, 200, l);
            return;
        }
        if (p.length == 1 && m.equals("POST")) {
            final Map<String, Object> b = body(x);
            @SuppressWarnings("unchecked")
            final Map<String, Object> src = (Map<String, Object>) b.get("source");
            if (src == null || Sources.str(src.get("type")).isEmpty()) {
                throw new ApiError(400, "source.type is required");
            }
            final Map<String, Object> pr = new LinkedHashMap<>();
            pr.put("id", ws.newId("p"));
            pr.put("name", Sources.str(b.get("name")).isEmpty() ? defaultName(src) : Sources.str(b.get("name")));
            pr.put("source", src);
            pr.put("created", Instant.now().toString());
            pipeline.acquire(pr);
            json(x, 201, pr);
            return;
        }
        final Map<String, Object> pr = ws.project(p[1]);
        if (pr == null) {
            throw new ApiError(404, "no such project");
        }
        if (p.length == 2 && m.equals("GET")) {
            final Map<String, Object> v = new LinkedHashMap<>(pr);
            final List<Map<String, Object>> runs = new ArrayList<>();
            for (final Map<String, Object> r : ws.runs(p[1])) {
                runs.add(brief(r));
            }
            v.put("runList", runs);
            json(x, 200, v);
        } else if (p.length == 2 && m.equals("DELETE")) {
            ws.deleteProject(p[1]);
            json(x, 200, error(null));
        } else if (p.length == 3 && p[2].equals("refetch") && m.equals("POST")) {
            pipeline.acquire(pr);
            json(x, 200, pr);
        } else if (p.length == 3 && p[2].equals("log")) {
            final File f = new File(ws.projectDir(p[1]), "acquire.log");
            final Map<String, Object> l = new LinkedHashMap<>();
            l.put("text", f.isFile() ? new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8) : "");
            l.put("status", pr.get("status"));
            json(x, 200, l);
        } else if (p.length == 3 && p[2].equals("runs") && m.equals("POST")) {
            if (!"ready".equals(pr.get("status"))) {
                throw new ApiError(409, "the project is not ready (" + pr.get("status") + ")");
            }
            json(x, 201, pipeline.start(pr, body(x)));
        } else {
            throw new ApiError(404, "no such endpoint");
        }
    }

    private void runs(final HttpExchange x, final String m, final String[] p, final Map<String, String> q)
            throws Exception {
        if (p.length == 1) {
            final List<Map<String, Object>> l = new ArrayList<>();
            for (final Map<String, Object> r : ws.runs(null)) {
                l.add(brief(r));
                if (l.size() >= intParam(q, "limit", 50)) {
                    break;
                }
            }
            json(x, 200, l);
            return;
        }
        final Map<String, Object> run = ws.run(p[1]);
        if (run == null) {
            throw new ApiError(404, "no such run");
        }
        final Map<String, Object> pr = ws.project((String) run.get("projectId"));
        final String view = p.length > 2 ? p[2] : "";
        final String h = q.get("h");
        if (view.isEmpty()) {
            json(x, 200, run);
        } else if (view.equals("log")) {
            json(x, 200, tail(new File(ws.runDir(p[1]), "log.txt"), Long.parseLong(q.getOrDefault("offset", "0")),
                    run.get("status")));
        } else if (view.equals("cancel") && m.equals("POST")) {
            pipeline.cancel(p[1]);
            json(x, 200, run);
        } else {
            if (!"done".equals(run.get("status")) || pr == null) {
                throw new ApiError(409, "the run has no results (" + run.get("status") + ")");
            }
            final Results res = new Results(ws, pr, run);
            switch (view) {
            case "summary":
                json(x, 200, res.summary());
                break;
            case "ranking":
                json(x, 200, res.ranking(h, intParam(q, "offset", 0), intParam(q, "limit", 100), q.get("filter")));
                break;
            case "files":
                json(x, 200, res.files(h));
                break;
            case "source":
                json(x, 200, res.source(q.get("file"), h));
                break;
            case "tests":
                json(x, 200, res.tests());
                break;
            case "elements":
                json(x, 200, res.elements(q.getOrDefault("kind", "branches"), h, intParam(q, "limit", 200)));
                break;
            case "artifacts":
                json(x, 200, res.artifacts());
                break;
            case "download": {
                final String name = p.length > 3 ? p[3] : "";
                final File f = new File(res.reportDir(null), name);
                if (name.contains("/") || name.contains("\\") || name.startsWith(".") || !f.isFile()) {
                    throw new ApiError(404, "no such file");
                }
                x.getResponseHeaders().add("Content-Disposition", "attachment; filename=\"" + name + "\"");
                send(x, 200, name.endsWith(".xml") ? "application/xml" : "text/plain; charset=utf-8",
                        Files.readAllBytes(f.toPath()));
                break;
            }
            default:
                throw new ApiError(404, "no such view");
            }
        }
    }

    private void catalog(final HttpExchange x, final String[] p, final Map<String, String> q) throws Exception {
        final String bench = p.length > 1 ? p[1] : "";
        final String what = p.length > 2 ? p[2] : "";
        try {
            if (bench.equals("defects4j") && what.equals("projects")) {
                json(x, 200, sources.defects4jProjects());
            } else if (bench.equals("defects4j") && what.equals("bugs")) {
                json(x, 200, sources.defects4jBugs(q.get("project")));
            } else if (bench.equals("bugsjar") && what.equals("projects")) {
                json(x, 200, Sources.BUGSJAR_PROJECTS);
            } else if (bench.equals("bugsjar") && what.equals("bugs")) {
                json(x, 200, sources.bugsjarIssues(q.get("project")));
            } else if (bench.equals("bears") && what.equals("bugs")) {
                final List<Map<String, Object>> l = new ArrayList<>();
                for (final Map.Entry<String, String> e : sources.bearsBranches(s -> { }, null).entrySet()) {
                    final Map<String, Object> b = new LinkedHashMap<>();
                    b.put("id", e.getKey());
                    final String[] parts = e.getValue().split("-");
                    b.put("project", parts.length > 2 ? String.join("-", java.util.Arrays.copyOf(parts, parts.length - 2))
                            : e.getValue());
                    l.add(b);
                }
                json(x, 200, l);
            } else {
                throw new ApiError(404, "no such catalog");
            }
        } catch (final IOException e) {
            throw new ApiError(503, e.getMessage());
        }
    }

    // ---- helpers ----

    private static Map<String, Object> brief(final Map<String, Object> r) {
        final Map<String, Object> b = new LinkedHashMap<>();
        for (final String k : new String[] { "id", "projectId", "projectName", "created", "status", "seconds", "error",
            "options" }) {
            b.put(k, r.get(k));
        }
        @SuppressWarnings("unchecked")
        final Map<String, Object> s = (Map<String, Object>) r.get("summary");
        if (s != null) {
            final Map<String, Object> sb = new LinkedHashMap<>();
            for (final String k : new String[] { "tests", "failed", "lines", "topSusp" }) {
                sb.put(k, s.get(k));
            }
            @SuppressWarnings("unchecked")
            final Map<String, Object> t = (Map<String, Object>) s.get("truth");
            sb.put("bestRank", t == null ? null : t.get("bestRank"));
            b.put("summary", sb);
        }
        return b;
    }

    private static String defaultName(final Map<String, Object> src) {
        switch (Sources.str(src.get("type"))) {
        case "git": {
            final String u = Sources.str(src.get("url")).replaceAll("\\.git$", "").replaceAll("/+$", "");
            return u.substring(u.lastIndexOf('/') + 1);
        }
        case "local":
            return new File(Sources.str(src.get("path"))).getName();
        case "defects4j":
            return src.get("project") + "-" + src.get("bug");
        case "bugsjar":
            return Sources.str(src.get("issue"));
        case "bears":
            return Sources.str(src.get("bug"));
        default:
            return "project";
        }
    }

    private static List<String> heuristics() {
        final List<String> l = new ArrayList<>();
        for (final Heuristic h : Heuristic.values()) {
            l.add(h.name().toLowerCase());
        }
        return l;
    }

    private static Map<String, Object> tail(final File f, final long offset, final Object status) throws IOException {
        final Map<String, Object> m = new LinkedHashMap<>();
        m.put("status", status);
        if (!f.isFile()) {
            m.put("text", "");
            m.put("offset", 0);
            return m;
        }
        try (RandomAccessFile r = new RandomAccessFile(f, "r")) {
            final long len = r.length();
            final long from = Math.min(Math.max(0, offset), len);
            final int n = (int) Math.min(len - from, 512 * 1024);
            final byte[] b = new byte[n];
            r.seek(from);
            r.readFully(b);
            m.put("text", new String(b, StandardCharsets.UTF_8));
            m.put("offset", from + n);
        }
        return m;
    }

    private static Map<String, Object> error(final String message) {
        final Map<String, Object> m = new LinkedHashMap<>();
        m.put(message == null ? "ok" : "error", message == null ? Boolean.TRUE : message);
        return m;
    }

    private static int intParam(final Map<String, String> q, final String k, final int dflt) {
        try {
            return q.containsKey(k) ? Integer.parseInt(q.get(k)) : dflt;
        } catch (final NumberFormatException e) {
            return dflt;
        }
    }

    private static Map<String, String> query(final HttpExchange x) throws IOException {
        final Map<String, String> m = new LinkedHashMap<>();
        final String raw = x.getRequestURI().getRawQuery();
        if (raw != null) {
            for (final String kv : raw.split("&")) {
                final int eq = kv.indexOf('=');
                if (eq > 0) {
                    m.put(URLDecoder.decode(kv.substring(0, eq), "UTF-8"), URLDecoder.decode(kv.substring(eq + 1), "UTF-8"));
                }
            }
        }
        return m;
    }

    private static Map<String, Object> body(final HttpExchange x) throws IOException {
        final ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (InputStream in = x.getRequestBody()) {
            final byte[] b = new byte[8192];
            for (int n; (n = in.read(b)) > 0;) {
                out.write(b, 0, n);
            }
        }
        final String s = new String(out.toByteArray(), StandardCharsets.UTF_8).trim();
        try {
            return s.isEmpty() ? new LinkedHashMap<String, Object>() : Json.object(s);
        } catch (final IllegalArgumentException e) {
            throw new ApiError(400, "bad JSON: " + e.getMessage());
        }
    }

    private static void json(final HttpExchange x, final int status, final Object v) throws IOException {
        x.getResponseHeaders().add("Cache-Control", "no-store");
        send(x, status, "application/json; charset=utf-8", Json.write(v).getBytes(StandardCharsets.UTF_8));
    }

    private static void send(final HttpExchange x, final int status, final String type, final byte[] b)
            throws IOException {
        x.getResponseHeaders().add("Content-Type", type);
        x.getResponseHeaders().add("X-Content-Type-Options", "nosniff");
        x.sendResponseHeaders(status, b.length == 0 ? -1 : b.length);
        if (b.length > 0) {
            try (OutputStream o = x.getResponseBody()) {
                o.write(b);
            }
        }
    }

    private void serveStatic(final HttpExchange x, final String path) throws IOException {
        String p = path.equals("/") || path.isEmpty() ? "/index.html" : path;
        if (p.contains("..")) {
            send(x, 404, "text/plain", new byte[0]);
            return;
        }
        InputStream in = UiServer.class.getResourceAsStream("/jflp/ui/web" + p);
        if (in == null && !p.contains(".")) {
            p = "/index.html"; // client-side routes
            in = UiServer.class.getResourceAsStream("/jflp/ui/web" + p);
        }
        if (in == null) {
            send(x, 404, "text/plain", "not found".getBytes(StandardCharsets.UTF_8));
            return;
        }
        final ByteArrayOutputStream out = new ByteArrayOutputStream();
        try {
            final byte[] b = new byte[8192];
            for (int n; (n = in.read(b)) > 0;) {
                out.write(b, 0, n);
            }
        } finally {
            in.close();
        }
        x.getResponseHeaders().add("Cache-Control", "no-cache");
        send(x, 200, type(p), out.toByteArray());
    }

    private static String type(final String p) {
        if (p.endsWith(".html")) {
            return "text/html; charset=utf-8";
        }
        if (p.endsWith(".js")) {
            return "text/javascript; charset=utf-8";
        }
        if (p.endsWith(".css")) {
            return "text/css; charset=utf-8";
        }
        if (p.endsWith(".svg")) {
            return "image/svg+xml";
        }
        if (p.endsWith(".png")) {
            return "image/png";
        }
        return "application/octet-stream";
    }
}
