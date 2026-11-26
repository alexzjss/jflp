package sample;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

/** JUnit 4, run by the vintage engine: reported by RunNotifier and by the platform. */
public class VintageTest {

    @Test
    public void divide() {
        assertEquals(2, new Calc().divide(4, 2));
    }
}
