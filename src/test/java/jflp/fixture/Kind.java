package jflp.fixture;

public enum Kind {
    A, B;

    public int code() {
        return ordinal();
    }
}
