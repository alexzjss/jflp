package sample;

public class Calc {

    private static final int LIMIT;

    static {
        LIMIT = 100;
    }

    public int max(final int a, final int b) {
        if (a > b) {
            return a;
        }
        return a; // fault: should return b
    }

    public int divide(final int a, final int b) {
        final int q = a / b;
        return q;
    }

    public int clamp(final int v) {
        return v > LIMIT ? LIMIT : v;
    }

    public Runnable task(final int[] box) {
        return new Runnable() {
            public void run() {
                box[0]++;
            }
        };
    }

    public int sum(final int n) {
        int s = 0;
        for (int i = 0; i < n; i++) {
            s += i;
        }
        return s;
    }

    public int safeDivide(final int a, final int b) {
        try {
            return a / b;
        } catch (final ArithmeticException e) {
            return 0;
        }
    }
}
