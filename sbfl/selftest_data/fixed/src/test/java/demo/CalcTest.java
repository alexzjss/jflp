package demo;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class CalcTest {
    private final Calc c = new Calc();

    @Test
    public void testAbsPositive() {
        assertEquals(5, c.abs(5));
    }

    @Test
    public void testAbsNegative() {
        assertEquals(5, c.abs(-5));
    }

    @Test
    public void testTwice() {
        assertEquals(8, c.twice(4));
    }

    @Test
    public void testSign() {
        assertEquals(-1, c.sign(-9));
        assertEquals(1, c.sign(3));
        assertEquals(0, c.sign(0));
    }
}
