package jflp.report;

import java.io.File;
import java.io.IOException;
import java.io.Writer;
import java.util.ArrayList;
import java.util.List;

/**
 * {@code coverageMatrix.txt} as written by the Jaguar2 fork: one row per
 * counted test in execution order, one {@code "0 "}/{@code "1 "} per code line,
 * then {@code +} (passed) or {@code -} (failed).
 *
 * The fork writes no names, so the matrix cannot be read back on its own;
 * {@code coverageMatrix.columns.csv} (column, class, line) and
 * {@code coverageMatrix.tests.csv} (row, outcome, test) are written next to it.
 * The fork also appends to an existing file across runs; this one is rewritten.
 */
final class Matrix {

    private final int[] order; // matrix position -> column id
    private final int[] mark;
    private final char[] row;
    private final Writer matrix;
    private final Writer tests;
    private int rows;

    Matrix(final Model model, final File dir) throws IOException {
        // Insertion order of the fork's class map: directory listing order, then
        // (normally none) classes seen only at run time, by name.
        final java.util.LinkedHashSet<String> inserted = new java.util.LinkedHashSet<String>();
        for (final String n : model.directoryOrder) {
            if (model.classes.containsKey(n)) {
                inserted.add(n);
            }
        }
        inserted.addAll(model.classes.keySet());
        final List<String> names = new ArrayList<String>(inserted);
        order = new int[model.columns];
        int k = 0;
        for (final String name : HashMapOrder.of(names)) {
            final Model.Clazz c = model.classes.get(name);
            for (int p = 0; p < c.lines.length; p++) {
                order[k++] = c.colBase + p;
            }
        }
        mark = new int[model.columns];
        java.util.Arrays.fill(mark, -1);
        row = new char[model.columns * 2 + 2];
        for (int i = 0; i < model.columns; i++) {
            row[2 * i + 1] = ' ';
        }
        row[row.length - 1] = '\n';

        final Writer columns = Report.writer(new File(dir, "coverageMatrix.columns.csv"));
        try {
            columns.write("column,class,line\n");
            for (int i = 0; i < order.length; i++) {
                final int col = order[i];
                columns.write(i + "," + model.byCol[col].name.replace('/', '.') + "," + model.lineOfCol[col] + "\n");
            }
        } finally {
            columns.close();
        }
        matrix = Report.writer(new File(dir, "coverageMatrix.txt"));
        tests = Report.writer(new File(dir, "coverageMatrix.tests.csv"));
        tests.write("row,outcome,test\n");
    }

    void row(final String name, final boolean failed, final int[] cols, final int n) throws IOException {
        final int r = rows++;
        for (int i = 0; i < n; i++) {
            mark[cols[i]] = r;
        }
        for (int i = 0; i < order.length; i++) {
            row[2 * i] = mark[order[i]] == r ? '1' : '0';
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
