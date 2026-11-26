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
 * Spectra over code elements other than lines: branch edges ({@code branches=true})
 * and def-use associations ({@code duas=true}). One column per element, ordered by
 * class, line and label. Written in the same layout as the line reports: a CSV
 * like {@code jaguar2.csv} with the element's columns, and a matrix like
 * {@code coverageMatrix.txt} with its {@code .columns.csv} and {@code .tests.csv}.
 */
final class ElementModel {

    /** class -> line -> label -> column */
    final Map<String, TreeMap<Integer, TreeMap<String, Integer>>> classes =
            new TreeMap<String, TreeMap<Integer, TreeMap<String, Integer>>>();
    String[] classOfCol;
    int[] lineOfCol;
    String[] labelOfCol;
    int columns;

    void add(final String cls, final int line, final String label) {
        TreeMap<Integer, TreeMap<String, Integer>> byLine = classes.get(cls);
        if (byLine == null) {
            byLine = new TreeMap<Integer, TreeMap<String, Integer>>();
            classes.put(cls, byLine);
        }
        TreeMap<String, Integer> byLabel = byLine.get(Integer.valueOf(line));
        if (byLabel == null) {
            byLabel = new TreeMap<String, Integer>();
            byLine.put(Integer.valueOf(line), byLabel);
        }
        byLabel.put(label, Integer.valueOf(-1));
    }

    /** Column of an element, or -1; valid after {@link #seal()}. */
    int col(final String cls, final int line, final String label) {
        final TreeMap<Integer, TreeMap<String, Integer>> byLine = classes.get(cls);
        final TreeMap<String, Integer> byLabel = byLine == null ? null : byLine.get(Integer.valueOf(line));
        final Integer c = byLabel == null ? null : byLabel.get(label);
        return c == null ? -1 : c.intValue();
    }

    void seal() {
        final List<Object[]> all = new ArrayList<Object[]>();
        for (final Map.Entry<String, TreeMap<Integer, TreeMap<String, Integer>>> c : classes.entrySet()) {
            for (final Map.Entry<Integer, TreeMap<String, Integer>> l : c.getValue().entrySet()) {
                for (final Map.Entry<String, Integer> e : l.getValue().entrySet()) {
                    e.setValue(Integer.valueOf(all.size()));
                    all.add(new Object[] { c.getKey(), l.getKey(), e.getKey() });
                }
            }
        }
        columns = all.size();
        classOfCol = new String[columns];
        lineOfCol = new int[columns];
        labelOfCol = new String[columns];
        for (int i = 0; i < columns; i++) {
            classOfCol[i] = (String) all.get(i)[0];
            lineOfCol[i] = ((Integer) all.get(i)[1]).intValue();
            labelOfCol[i] = (String) all.get(i)[2];
        }
    }

    /**
     * Label of a DUA: method, variable, definition line and, for a p-use, the
     * line of the edge's target, tab-separated (see {@code writeDuaCsv}).
     */
    static String dua(final Object[] d) {
        return d[0] + "\t" + d[1] + "\t" + d[2] + "\t" + (((Integer) d[3]).intValue() < 0 ? "" : d[3]);
    }

    /** {@code 1:taken}, {@code 2:not-taken}, {@code 1:target-3}: n-th jump or switch on the line, and the edge. */
    static String describe(final int arg) {
        final int jump = arg >>> 16;
        final int edge = arg & 0xFFFF;
        final String e = edge == RawFormat.EDGE_TAKEN ? "taken"
                : edge == RawFormat.EDGE_NOT_TAKEN ? "not-taken" : "target-" + (edge - RawFormat.EDGE_TARGET + 1);
        return jump + ":" + e;
    }

    interface ColumnLine {
        String of(int col);
    }

    /** Fork-style matrix rows, as {@link Matrix} writes for lines. */
    static final class MatrixWriter {
        private final int[] mark;
        private final char[] row;
        private final Writer matrix;
        private final Writer tests;
        private int rows;

        /** prefix: file name prefix (branchMatrix, duaMatrix); header/columnLine: the .columns.csv rows. */
        MatrixWriter(final ElementModel model, final File dir, final String prefix, final String header,
                final ColumnLine columnLine) throws IOException {
            mark = new int[model.columns];
            java.util.Arrays.fill(mark, -1);
            row = new char[model.columns * 2 + 2];
            for (int i = 0; i < model.columns; i++) {
                row[2 * i + 1] = ' ';
            }
            row[row.length - 1] = '\n';
            final Writer columns = Report.writer(new File(dir, prefix + ".columns.csv"));
            try {
                columns.write(header + "\n");
                for (int i = 0; i < model.columns; i++) {
                    columns.write(i + "," + columnLine.of(i) + "\n");
                }
            } finally {
                columns.close();
            }
            matrix = Report.writer(new File(dir, prefix + ".txt"));
            tests = Report.writer(new File(dir, prefix + ".tests.csv"));
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
