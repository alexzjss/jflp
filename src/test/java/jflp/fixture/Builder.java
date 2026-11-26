package jflp.fixture;

/** Instrumented by the tests; line numbers are asserted, so keep the layout. */
public class Builder {

    public Object make(final boolean a) {
        return new StringBuilder(a ? "x" : "y");
    }
}
