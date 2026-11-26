package jflp.rt;

/**
 * Probe storage. Instrumented code calls {@link #h(int)} with a global probe id.
 *
 * Probes live in fixed-size pages so that a page, once published, never moves:
 * a thread holding a page reference can never write into a stale copy, which
 * a single growable array would allow.
 */
public final class R {

    static final int PAGE_BITS = 16;
    static final int PAGE_SIZE = 1 << PAGE_BITS;
    static final int PAGE_MASK = PAGE_SIZE - 1;

    static final boolean[][] PAGES = new boolean[1 << 15][];

    private static int allocated;

    /**
     * Per-class probe arrays for classes instrumented by JaCoCo's instrumenter
     * ({@code lines=jacoco}), which wants a {@code boolean[]} of its own per
     * class. Their probes still get global ids, {@code arrayBase[k] + index}.
     */
    private static boolean[][] arrays = new boolean[64][];
    private static int[] arrayBase = new int[64];
    private static int arrayCount;

    private R() {
    }

    /** Marks probe {@code id} as executed. Called from instrumented code. */
    public static void h(final int id) {
        PAGES[id >>> PAGE_BITS][id & PAGE_MASK] = true;
    }

    /** Reserves {@code count} consecutive probe ids and returns the first one. */
    public static synchronized int allocate(final int count) {
        final int base = allocated;
        final long end = (long) base + count;
        if (end > (long) PAGES.length * PAGE_SIZE) {
            throw new IllegalStateException("probe space exhausted");
        }
        for (int p = base >>> PAGE_BITS; p <= (int) ((end - 1) >>> PAGE_BITS) && count > 0; p++) {
            if (PAGES[p] == null) {
                PAGES[p] = new boolean[PAGE_SIZE];
            }
        }
        allocated = (int) end;
        return base;
    }

    /** Registers a JaCoCo probe array whose probes have ids {@code base ..}; returns its index. */
    public static synchronized int register(final int base, final int count) {
        if (arrayCount == arrays.length) {
            final boolean[][] a = new boolean[arrayCount * 2][];
            System.arraycopy(arrays, 0, a, 0, arrayCount);
            arrays = a;
            final int[] b = new int[arrayCount * 2];
            System.arraycopy(arrayBase, 0, b, 0, arrayCount);
            arrayBase = b;
        }
        arrays[arrayCount] = new boolean[count];
        arrayBase[arrayCount] = base;
        return arrayCount++;
    }

    /** Probe array {@code index}. Called once per class by JaCoCo's {@code $jacocoInit}. */
    public static synchronized boolean[] a(final int index) {
        return arrays[index];
    }

    static synchronized int allocated() {
        return allocated;
    }

    /** Returns the ids of all executed probes in ascending order and clears them. */
    static int[] collectAndReset() {
        final int n = allocated();
        int[] out = new int[64];
        int size = 0;
        for (int p = 0; p * PAGE_SIZE < n; p++) {
            final boolean[] page = PAGES[p];
            final int limit = Math.min(PAGE_SIZE, n - p * PAGE_SIZE);
            for (int i = 0; i < limit; i++) {
                if (page[i]) {
                    page[i] = false;
                    if (size == out.length) {
                        final int[] grown = new int[size * 2];
                        System.arraycopy(out, 0, grown, 0, size);
                        out = grown;
                    }
                    out[size++] = (p << PAGE_BITS) | i;
                }
            }
        }
        final boolean[][] arr;
        final int[] base;
        final int count;
        synchronized (R.class) {
            arr = arrays;
            base = arrayBase;
            count = arrayCount;
        }
        for (int k = 0; k < count; k++) {
            final boolean[] a = arr[k];
            for (int i = 0; i < a.length; i++) {
                if (a[i]) {
                    a[i] = false;
                    if (size == out.length) {
                        final int[] grown = new int[size * 2];
                        System.arraycopy(out, 0, grown, 0, size);
                        out = grown;
                    }
                    out[size++] = base[k] + i;
                }
            }
        }
        // Array probes come after page probes of other classes: keep ids ascending.
        final int[] result = new int[size];
        System.arraycopy(out, 0, result, 0, size);
        java.util.Arrays.sort(result);
        return result;
    }

    /** Clears all probes. */
    static void reset() {
        final int n = allocated();
        for (int p = 0; p * PAGE_SIZE < n; p++) {
            java.util.Arrays.fill(PAGES[p], false);
        }
        synchronized (R.class) {
            for (int k = 0; k < arrayCount; k++) {
                java.util.Arrays.fill(arrays[k], false);
            }
        }
    }
}
