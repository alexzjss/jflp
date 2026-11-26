package jflp.fixture;

/** Instrumented by the tests; line numbers are asserted, so keep the layout. */
public class Branchy {

    public int both(final boolean a, final boolean b) {
        if (a && b) {
            return 1;
        }
        return 0;
    }

    public int pick(final int k) {
        switch (k) {
        case 1:
            return 10;
        case 2:
        case 3:
            return 20;
        default:
            return 0;
        }
    }

    public long wide(final long x, final double y, final Object o) {
        return o == null ? x : (long) y; // long/double locals in the frames of the trampolines
    }
}
