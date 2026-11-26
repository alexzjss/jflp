package jflp.cli;

import java.io.File;

import jflp.report.Heuristic;
import jflp.report.Report;

/** {@code java -jar jflp.jar report [options]}. */
public final class Main {

    private static final String USAGE = "usage: java -jar jflp.jar ui [--port N] [--workspace DIR] [--no-browser]\n"
            + "       java -jar jflp.jar report [options]\n"
            + "  --data DIR        agent coverage files (default target/jflp)\n"
            + "  --classes DIRS    measured class directories, separated by '" + File.pathSeparator
            + "' (default target/classes)\n"
            + "  --out DIR         where reports go (default target)\n"
            + "  --heuristic NAME  for jaguar2.csv/xml (default ochiai; also tarantula, jaccard,\n"
            + "                    zoltar, op, minus, mccon, drt, kulczynski2, wong3)\n"
            + "  --no-matrix       skip coverageMatrix.txt\n"
            + "  --all-lines       also write spectra.csv with every code line (susp 0 included)\n"
            + "  --strict          exit with status 3 when there is any warning\n"
            + "\n"
            + "agent: -javaagent:/path/to/jflp.jar[=destdir=DIR,classdirs=DIRS]\n";

    private Main() {
    }

    public static void main(final String[] args) {
        final int code = run(args);
        if (code >= 0) {
            System.exit(code);
        }
    }

    static int run(final String[] args) {
        if (args.length > 0 && "ui".equals(args[0])) {
            return ui(args);
        }
        if (args.length == 0 || !"report".equals(args[0])) {
            System.err.print(USAGE);
            return 2;
        }
        final Report.Options o = new Report.Options();
        String data = "target" + File.separator + "jflp";
        String classes = "target" + File.separator + "classes";
        String out = "target";
        boolean strict = false;
        try {
            for (int i = 1; i < args.length; i++) {
                final String a = args[i];
                if ("--data".equals(a)) {
                    data = arg(args, ++i, a);
                } else if ("--classes".equals(a)) {
                    classes = arg(args, ++i, a);
                } else if ("--out".equals(a)) {
                    out = arg(args, ++i, a);
                } else if ("--heuristic".equals(a)) {
                    o.heuristic = Heuristic.byName(arg(args, ++i, a));
                } else if ("--no-matrix".equals(a)) {
                    o.matrix = false;
                } else if ("--all-lines".equals(a)) {
                    o.allLines = true;
                } else if ("--strict".equals(a)) {
                    strict = true;
                } else {
                    throw new IllegalArgumentException("unknown option " + a);
                }
            }
        } catch (final IllegalArgumentException e) {
            System.err.println("jflp: " + e.getMessage());
            System.err.print(USAGE);
            return 2;
        }
        o.dataDir = new File(data);
        o.outDir = new File(out);
        for (final String d : classes.split(File.pathSeparator)) {
            if (d.length() > 0) {
                o.classDirs.add(new File(d));
            }
        }
        final Report.Summary s;
        try {
            s = Report.run(o);
        } catch (final Exception e) {
            System.err.println("jflp: " + e.getMessage());
            return 1;
        }
        System.out.println("jflp: " + (s.passed + s.failed) + " tests (" + s.failed + " failed), "
                + s.classes + " classes, " + s.lines + " lines -> " + o.outDir);
        for (final String w : s.warnings) {
            System.err.println("jflp: WARNING: " + w);
        }
        return strict && !s.warnings.isEmpty() ? 3 : 0;
    }

    /** {@code ui}: the web UI, which needs Java 8; loaded by name so the rest stays Java 5. */
    private static int ui(final String[] args) {
        final String[] rest = new String[args.length - 1];
        System.arraycopy(args, 1, rest, 0, rest.length);
        try {
            Class.forName("jflp.ui.UiServer").getMethod("main", String[].class).invoke(null, (Object) rest);
            return -1; // keeps running: the server threads hold the JVM
        } catch (final UnsupportedClassVersionError e) {
            System.err.println("jflp: the UI needs Java 8 or newer (running " + System.getProperty("java.version") + ")");
            return 1;
        } catch (final java.lang.reflect.InvocationTargetException e) {
            System.err.println("jflp: " + e.getCause());
            return 1;
        } catch (final Exception e) {
            System.err.println("jflp: cannot start the UI: " + e);
            return 1;
        }
    }

    private static String arg(final String[] args, final int i, final String option) {
        if (i >= args.length) {
            throw new IllegalArgumentException(option + " needs a value");
        }
        return args[i];
    }
}
