package jflp.report;

import java.io.File;
import java.io.IOException;
import java.io.Writer;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import jflp.data.RawFormat;

/**
 * Branch spectra ({@code branches=true}): one column per edge of every
 * conditional jump and switch, ordered by class, line and position on the line.
 * Written in the same layout as the line reports: {@code branches.csv} as
 * {@code jaguar2.csv} with a branch column, and {@code branchMatrix.txt} with its
 * {@code .columns.csv} and {@code .tests.csv}.
 */
final class BranchModel {

    /** class -> (line << 32 | arg) -> column */
    final Map<String, TreeMap<Long, Integer>> classes = new TreeMap<String, TreeMap<Long, Integer>>();
    String[] classOfCol;
    int[] lineOfCol;
    int[] argOfCol;
    int columns;

    void add(final String cls, final int line, final int arg) {
        TreeMap<Long, Integer> m = classes.get(cls);
        if (m == null) {
            m = new TreeMap<Long, Integer>();
            classes.put(cls, m);
        }
        m.put(Long.valueOf(key(line, arg)), Integer.valueOf(-1));
    }

    /** Column of a branch, or -1; valid after {@link #seal()}. */
    int col(final String cls, final int line, final int arg) {
        final TreeMap<Long, Integer> m = classes.get(cls);
        final Integer c = m == null ? null : m.get(Long.valueOf(key(line, arg)));
        return c == null ? -1 : c.intValue();
    }

    void seal() {
        final List<Object[]> all = new ArrayList<Object[]>();
        for (final Map.Entry<String, TreeMap<Long, Integer>> c : classes.entrySet()) {
            for (final Map.Entry<Long, Integer> b : c.getValue().entrySet()) {
                b.setValue(Integer.valueOf(all.size()));
                all.add(new Object[] { c.getKey(), b.getKey() });
            }
        }
        columns = all.size();
        classOfCol = new String[columns];
        lineOfCol = new int[columns];
        argOfCol = new int[columns];
        for (int i = 0; i < columns; i++) {
            classOfCol[i] = (String) all.get(i)[0];
            final long k = ((Long) all.get(i)[1]).longValue();
            lineOfCol[i] = (int) (k >>> 32);
            argOfCol[i] = (int) k;
        }
    }

    /** {@code 1:taken}, {@code 2:not-taken}, {@code 1:target-3}: n-th jump or switch on the line, and the edge. */
    static String describe(final int arg) {
        final int jump = arg >>> 16;
        final int edge = arg & 0xFFFF;
        final String e = edge == RawFormat.EDGE_TAKEN ? "taken"
                : edge == RawFormat.EDGE_NOT_TAKEN ? "not-taken" : "target-" + (edge - RawFormat.EDGE_TARGET + 1);
        return jump + ":" + e;
    }

    private static long key(final int line, final int arg) {
        return ((long) line << 32) | (arg & 0xFFFFFFFFL);
    }

    /** Fork-style matrix rows, as {@link Matrix} writes for lines. */
    static final class MatrixWriter {
        private final int[] mark;
        private final char[] row;
        private final Writer matrix;
        private final Writer tests;
        private int rows;

        MatrixWriter(final BranchModel model, final File dir) throws IOException {
            mark = new int[model.columns];
            java.util.Arrays.fill(mark, -1);
            row = new char[model.columns * 2 + 2];
            for (int i = 0; i < model.columns; i++) {
                row[2 * i + 1] = ' ';
            }
            row[row.length - 1] = '\n';
            final Writer columns = Report.writer(new File(dir, "branchMatrix.columns.csv"));
            try {
                columns.write("column,class,line,branch\n");
                for (int i = 0; i < model.columns; i++) {
                    columns.write(i + "," + model.classOfCol[i].replace('/', '.') + "," + model.lineOfCol[i] + ","
                            + describe(model.argOfCol[i]) + "\n");
                }
            } finally {
                columns.close();
            }
            matrix = Report.writer(new File(dir, "branchMatrix.txt"));
            tests = Report.writer(new File(dir, "branchMatrix.tests.csv"));
            tests.write("row,outcome,test\n");
        }

        void row(final String name, final boolean failed, final int[] cols, final int n) throws IOException {
            final int r = rows++;
            for (int i = 0; i < n; i++) {
                mark[cols[i]] = r;
            }
            for (int i = 0; i < mark.length; i++) {
                row[2 * i] = mark[i] == r ? '1' : '0';
            }
            row[row.length - 2] = failed ? '-' : '+';
            matrix.write(row);
            tests.write(r + "," + (failed ? "FAIL" : "PASS") + "," + Csv.quote(name) + "\n");
        }

        void close() throws IOException {
            try {
                matrix.close();
            } finally {
                tests.close();
            }
        }
    }
}
