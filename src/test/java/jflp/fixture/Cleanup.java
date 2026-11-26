package jflp.fixture;

/** Instrumented by the tests; line numbers are asserted, so keep the layout. */
public class Cleanup {

    public int closed;

    public int run(final boolean fail) {
        try {
            if (fail) {
                throw new IllegalStateException();
            }
            return 1;
        } finally {
            closed++;
        }
    }
}
