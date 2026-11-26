package jflp.fixture;

/** Instrumented by the tests; line numbers are asserted, so keep the layout. */
public class Target {

    public int throwsOnLine8(final int a, final int b) {
        final int unused = 0;
        final int q = a / b;
        return q + unused;
    }

    public int branchy(final int v) {
        if (v > 0) {
            return 1;
        }
        return -1;
    }

    private Target(final String s) {
        super();
    }

    public Target() {
        this("x");
    }
}
