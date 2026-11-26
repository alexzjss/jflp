package sample;

import junit.framework.TestCase;

public class LegacyTest extends TestCase {

    public void testSum() {
        assertEquals(1, new Calc().sum(2));
    }

    public void testWarm() {
        assertTrue(Color.GREEN.warm());
    }
}
