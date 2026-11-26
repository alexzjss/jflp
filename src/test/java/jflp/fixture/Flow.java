package jflp.fixture;

/** Instrumented by the tests; line numbers are asserted, so keep the layout. */
public class Flow {

    public int f(final int x) {
        int y = 0;
        if (x > 0) {
            y = x;
        }
        return y;
    }
}
