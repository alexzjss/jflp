package jflp.report;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;

/**
 * The code lines of every measured class, each given a column id.
 * Columns are numbered class by class in class-name order, lines ascending.
 */
final class Model {

    static final class Clazz {
        final String name;
        String source;
        final TreeSet<Integer> lineSet = new TreeSet<Integer>();
        final List<Long> crcs = new ArrayList<Long>();

        int[] lines;
        int colBase;
        int first;
        int[] posOfLine; // index line - first, -1 when not a code line

        Clazz(final String name) {
            this.name = name;
        }

        String packageName() {
            final int i = name.lastIndexOf('/');
            return i < 0 ? "" : name.substring(0, i);
        }

        /** Column of {@code line}, or -1 when it holds no code. */
        int col(final int line) {
            final int i = line - first;
            if (lines.length == 0 || i < 0 || i >= posOfLine.length || posOfLine[i] < 0) {
                return -1;
            }
            return colBase + posOfLine[i];
        }
    }

    final Map<String, Clazz> classes = new TreeMap<String, Clazz>();
    Clazz[] byCol;
    int[] lineOfCol;
    int columns;

    Clazz clazz(final String name) {
        Clazz c = classes.get(name);
        if (c == null) {
            c = new Clazz(name);
            classes.put(name, c);
        }
        return c;
    }

    /** Freezes the class set and assigns columns. */
    void seal() {
        int col = 0;
        for (final Clazz c : classes.values()) {
            c.lines = new int[c.lineSet.size()];
            int i = 0;
            for (final Integer l : c.lineSet) {
                c.lines[i++] = l.intValue();
            }
            c.colBase = col;
            col += c.lines.length;
            if (c.lines.length > 0) {
                c.first = c.lines[0];
                c.posOfLine = new int[c.lines[c.lines.length - 1] - c.first + 1];
                java.util.Arrays.fill(c.posOfLine, -1);
                for (int p = 0; p < c.lines.length; p++) {
                    c.posOfLine[c.lines[p] - c.first] = p;
                }
            }
        }
        columns = col;
        byCol = new Clazz[col];
        lineOfCol = new int[col];
        for (final Clazz c : classes.values()) {
            for (int p = 0; p < c.lines.length; p++) {
                byCol[c.colBase + p] = c;
                lineOfCol[c.colBase + p] = c.lines[p];
            }
        }
    }
}
