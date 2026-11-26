package sample;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.RepeatedTest;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class JupiterCalcTest {

    @Test
    void maxFirst() {
        assertEquals(5, new Calc().max(5, 3));
    }

    @Test
    void maxSecond() {
        assertEquals(7, new Calc().max(2, 7)); // fails: the fault in Calc.max
    }

    @ParameterizedTest
    @ValueSource(ints = { 1, 3 })
    void sum(final int n) {
        assertEquals(n * (n - 1) / 2, new Calc().sum(n));
    }

    @RepeatedTest(2)
    void clamp() {
        assertEquals(100, new Calc().clamp(500));
    }

    @Test
    void skippedByAssumption() {
        assumeTrue(false);
        new Calc().sum(10);
    }

    @Disabled
    @Test
    void disabled() {
        new Calc().sum(10);
    }
}
