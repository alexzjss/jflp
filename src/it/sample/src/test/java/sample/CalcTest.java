package sample;

import static org.junit.Assert.assertEquals;
import static org.junit.Assume.assumeTrue;

import org.junit.Ignore;
import org.junit.Test;

public class CalcTest {

    @Test
    public void maxFirst() {
        assertEquals(5, new Calc().max(5, 3));
    }

    @Test
    public void maxSecond() {
        assertEquals(7, new Calc().max(2, 7));
    }

    @Test
    public void divideByZero() {
        new Calc().divide(1, 0);
    }

    @Test
    public void clampAndTask() {
        assertEquals(100, new Calc().clamp(500));
        final int[] box = new int[1];
        new Calc().task(box).run();
        assertEquals(1, box[0]);
        assertEquals(true, Color.RED.warm());
    }

    @Test
    public void loops() {
        assertEquals(3, new Calc().sum(3));
        assertEquals(0, new Calc().safeDivide(1, 0));
    }

    @Test
    public void skippedByAssumption() {
        assumeTrue(false);
        new Calc().sum(10);
    }

    @Ignore
    @Test
    public void ignored() {
        new Calc().sum(10);
    }
}
