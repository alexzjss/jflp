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
        final int[] result = new int[size];
        System.arraycopy(out, 0, result, 0, size);
        return result;
    }

    /** Clears all probes. */
    static void reset() {
        final int n = allocated();
        for (int p = 0; p * PAGE_SIZE < n; p++) {
            java.util.Arrays.fill(PAGES[p], false);
        }
    }
}
