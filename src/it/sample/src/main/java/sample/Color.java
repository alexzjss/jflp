package sample;

public enum Color {
    RED,
    GREEN;

    public boolean warm() {
        return this == RED;
    }
}
