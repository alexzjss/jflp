package demo;

public class Calc {
    public int abs(int x) {
        if (x < 0) {
            return x;
        }
        return x;
    }

    public int twice(int x) {
        return x * 2;
    }

    public int sign(int x) {
        if (x > 0) {
            return 1;
        }
        if (x < 0) {
            return -1;
        }
        return 0;
    }
}
