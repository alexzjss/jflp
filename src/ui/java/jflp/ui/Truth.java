package jflp.ui;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Buggy lines from a fix patch (same rule as bench/sbfl_common.py patch_lines).
 *
 * buggySide is '+' when the diff goes from fixed to buggy (Defects4J) and '-'
 * when it goes from buggy to fixed (Bugs.jar, Bears, a fix commit of a git
 * repository). A hunk that only adds code on the fixed side is a fault of
 * omission and marks the buggy line right after the insertion point.
 */
final class Truth {

    private static final Pattern HUNK = Pattern.compile("^@@ -(\\d+)(?:,\\d+)? \\+(\\d+)(?:,\\d+)? @@");

    private Truth() {
    }

    /** Entries "path:line" of buggy-version lines touched by the fix. */
    static Set<String> patchLines(final File patch, final char buggySide) throws IOException {
        return patchLines(Files.readAllLines(patch.toPath(), StandardCharsets.UTF_8), buggySide);
    }

    static Set<String> patchLines(final List<String> diff, final char buggySide) {
        final char fixedSide = buggySide == '+' ? '-' : '+';
        final String buggyHeader = buggySide == '+' ? "+++ " : "--- ";
        final Set<String> out = new LinkedHashSet<>();
        String path = null;
        int line = 0;
        boolean pending = false;
        for (final String raw : diff) {
            if (raw.startsWith("+++ ") || raw.startsWith("--- ")) {
                if (raw.startsWith(buggyHeader)) {
                    String p = raw.substring(4).trim().split("\t")[0];
                    if (p.startsWith("a/") || p.startsWith("b/")) {
                        p = p.substring(2);
                    }
                    path = p;
                }
                continue;
            }
            if (raw.startsWith("diff ")) {
                continue;
            }
            final Matcher m = HUNK.matcher(raw);
            if (m.find()) {
                line = Integer.parseInt(buggySide == '-' ? m.group(1) : m.group(2));
                pending = false;
                continue;
            }
            if (path == null || raw.isEmpty()) {
                continue;
            }
            final char c = raw.charAt(0);
            if (c == buggySide) {
                out.add(path + ":" + line);
                line++;
                pending = false;
            } else if (c == fixedSide) {
                pending = true;
            } else if (c == ' ') {
                if (pending) {
                    out.add(path + ":" + line);
                }
                pending = false;
                line++;
            }
        }
        return out;
    }

    /**
     * Whether a patch path ("src/main/java/org/x/Foo.java") is the source file of
     * VM class name "org/x/Foo" or one of its nested classes ("org/x/Foo$1").
     */
    static boolean sourceOf(final String path, final String vmClass) {
        if (!path.endsWith(".java")) {
            return false;
        }
        final int dollar = vmClass.indexOf('$');
        final String outer = dollar < 0 ? vmClass : vmClass.substring(0, dollar);
        final String norm = path.replace('\\', '/');
        return norm.endsWith("/" + outer + ".java") || norm.equals(outer + ".java");
    }
}
