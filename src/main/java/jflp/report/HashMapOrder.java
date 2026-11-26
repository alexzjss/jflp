package jflp.report;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

/**
 * Iteration order of a {@code java.util.HashMap} (Java 8+ implementation)
 * filled with the given keys in the given order.
 *
 * The coverage matrix of the Jaguar2 fork lays out its columns in the order of
 * JaCoCo's {@code CoverageBuilder}, a HashMap keyed by class name. Computing
 * that order explicitly, instead of iterating a real HashMap, gives the same
 * columns whatever JVM runs the report (Java 5-7 HashMaps order differently).
 *
 * Keys that share a bucket keep insertion order. The fork inserted classes in
 * file-system listing order, which is not reproducible; callers insert in
 * name order, so only such bucket collisions can differ from a fork run.
 */
final class HashMapOrder {

    private HashMapOrder() {
    }

    static List<String> of(final List<String> keysInInsertionOrder) {
        int capacity = 16;
        while (keysInInsertionOrder.size() > capacity * 3 / 4) {
            capacity <<= 1;
        }
        final int mask = capacity - 1;
        final List<int[]> slots = new ArrayList<int[]>(); // {bucket, insertion index}
        for (int i = 0; i < keysInInsertionOrder.size(); i++) {
            final int h = keysInInsertionOrder.get(i).hashCode();
            slots.add(new int[] { (h ^ (h >>> 16)) & mask, i });
        }
        Collections.sort(slots, new Comparator<int[]>() {
            public int compare(final int[] a, final int[] b) {
                return a[0] != b[0] ? (a[0] < b[0] ? -1 : 1) : (a[1] < b[1] ? -1 : a[1] == b[1] ? 0 : 1);
            }
        });
        final List<String> out = new ArrayList<String>(slots.size());
        for (final int[] s : slots) {
            out.add(keysInInsertionOrder.get(s[1]));
        }
        return out;
    }
}
