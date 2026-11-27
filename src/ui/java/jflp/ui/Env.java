package jflp.ui;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Installed JDKs and build tools, as the Environment page shows them. */
final class Env {

    /** Current settings, kept up to date by the server, for code that has no workspace at hand. */
    static volatile Map<String, Object> settings = new LinkedHashMap<>();

    private Env() {
    }

    /** The defects4j command from the current settings (or the PATH). */
    static String defects4jPath() {
        final String d = defects4j(settings);
        return d == null ? "defects4j" : d;
    }

    /** JDKs found in the usual places, plus the ones listed in settings ("jdks"), by major version. */
    static List<Map<String, Object>> jdks(final Map<String, Object> settings) {
        final Set<File> candidates = new LinkedHashSet<>();
        final String home = System.getenv("JAVA_HOME");
        if (home != null) {
            candidates.add(new File(home));
        }
        candidates.add(new File(System.getProperty("java.home")));
        final String user = System.getProperty("user.home");
        for (final String root : Arrays.asList("/usr/lib/jvm", "/usr/java", "/opt/java", "/opt/jdk",
                "/Library/Java/JavaVirtualMachines", "C:\\Program Files\\Java", "C:\\Program Files\\Eclipse Adoptium",
                "C:\\Program Files\\Zulu", user + "/.sdkman/candidates/java", user + "/.jdks")) {
            final File[] fs = new File(root).listFiles();
            if (fs != null) {
                Arrays.sort(fs);
                for (final File f : fs) {
                    candidates.add(new File(f, "Contents/Home").isDirectory() ? new File(f, "Contents/Home") : f);
                }
            }
        }
        final Object extra = settings.get("jdks");
        if (extra instanceof List) {
            for (final Object o : (List<?>) extra) {
                candidates.add(new File(String.valueOf(o)));
            }
        }
        final Map<String, Map<String, Object>> byPath = new LinkedHashMap<>();
        for (final File c : candidates) {
            File dir;
            try {
                dir = c.getCanonicalFile();
            } catch (final Exception e) {
                continue;
            }
            if (dir.getName().equals("jre") && new File(dir.getParentFile(), "bin/javac").exists()) {
                dir = dir.getParentFile(); // a JDK 8 java.home points at its jre/
            }
            final boolean javac = new File(dir, "bin/javac").exists() || new File(dir, "bin/javac.exe").exists();
            if (!javac || byPath.containsKey(dir.getPath())) {
                continue;
            }
            final int major = major(dir);
            if (major <= 0) {
                continue;
            }
            final Map<String, Object> j = new LinkedHashMap<>();
            j.put("path", dir.getPath());
            j.put("major", major);
            j.put("version", version(dir));
            byPath.put(dir.getPath(), j);
        }
        final List<Map<String, Object>> l = new ArrayList<>(byPath.values());
        l.sort((a, b) -> ((Integer) a.get("major")).compareTo((Integer) b.get("major")));
        return l;
    }

    /** The JDK with this major version (or the closest above it), or null. */
    static String jdkFor(final Map<String, Object> settings, final int major) {
        String best = null;
        int bestMajor = Integer.MAX_VALUE;
        for (final Map<String, Object> j : jdks(settings)) {
            final int m = (Integer) j.get("major");
            if (m >= major && m < bestMajor) {
                best = (String) j.get("path");
                bestMajor = m;
            }
        }
        return best;
    }

    static int major(final File jdk) {
        final String v = version(jdk);
        if (v == null) {
            return -1;
        }
        final Matcher m = Pattern.compile("^(1\\.)?(\\d+)").matcher(v);
        return m.find() ? Integer.parseInt(m.group(2)) : -1;
    }

    static String version(final File jdk) {
        final File release = new File(jdk, "release");
        if (release.isFile()) {
            try {
                for (final String line : Files.readAllLines(release.toPath(), StandardCharsets.UTF_8)) {
                    if (line.startsWith("JAVA_VERSION=")) {
                        return line.substring("JAVA_VERSION=".length()).replace("\"", "").trim();
                    }
                }
            } catch (final Exception ignored) {
                // fall through
            }
        }
        final String out = Exec.output(Arrays.asList(new File(jdk, "bin/java").getPath(), "-version"), null, null, 20);
        if (out != null) {
            final Matcher m = Pattern.compile("version \"([^\"]+)\"").matcher(out);
            if (m.find()) {
                return m.group(1);
            }
        }
        return null;
    }

    /** Build and benchmark tools with their versions; missing ones have no version. */
    static List<Map<String, Object>> tools(final Map<String, Object> settings) {
        final List<Map<String, Object>> l = new ArrayList<>();
        l.add(tool("git", Arrays.asList("git", "--version"), "git version ([^\\s]+)"));
        l.add(tool("Maven", Arrays.asList(exe("mvn"), "-v"), "Apache Maven ([^\\s]+)"));
        l.add(tool("Gradle", Arrays.asList(exe("gradle"), "--version"), "Gradle ([^\\s]+)"));
        l.add(tool("Ant", Arrays.asList(exe("ant"), "-version"), "Ant\\(TM\\) version ([^\\s]+)"));
        l.add(tool("Perl", Arrays.asList("perl", "-e", "print $]"), "([\\d.]+)"));
        final String d4j = defects4j(settings);
        final Map<String, Object> t = new LinkedHashMap<>();
        t.put("name", "Defects4J");
        t.put("version", d4j == null ? null : "installed");
        t.put("path", d4j);
        t.put("hint", d4j == null ? "set the Defects4J home in Settings (needs Perl and JDK 11)" : null);
        l.add(t);
        return l;
    }

    /** Path of the defects4j command, from the Defects4J home in settings or the PATH. */
    static String defects4j(final Map<String, Object> settings) {
        final Object home = settings.get("defects4jHome");
        if (home != null && !String.valueOf(home).trim().isEmpty()) {
            final File f = new File(String.valueOf(home).trim(), "framework/bin/defects4j");
            return f.isFile() ? f.getPath() : null;
        }
        final String path = System.getenv("PATH");
        if (path != null) {
            for (final String d : path.split(File.pathSeparator)) {
                final File f = new File(d, "defects4j");
                if (f.isFile()) {
                    return f.getPath();
                }
            }
        }
        return null;
    }

    static String exe(final String name) {
        return Exec.windows() ? name + ".cmd" : name;
    }

    private static Map<String, Object> tool(final String name, final List<String> cmd, final String versionRegex) {
        final Map<String, Object> t = new LinkedHashMap<>();
        t.put("name", name);
        final String out = Exec.output(cmd, null, null, 30);
        String version = null;
        if (out != null) {
            final Matcher m = Pattern.compile(versionRegex).matcher(out);
            version = m.find() ? m.group(1) : "installed";
        }
        t.put("version", version);
        return t;
    }
}
