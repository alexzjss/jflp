package jflp.rt;

/** Test access to the package-private probe runtime. */
public final class Probes {

    private Probes() {
    }

    public static int allocate(final int count) {
        return R.allocate(count);
    }

    public static int[] collect() {
        return R.collectAndReset();
    }

    public static void reset() {
        R.reset();
    }
}
