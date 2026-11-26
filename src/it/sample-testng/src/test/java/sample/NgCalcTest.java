package sample;

import static org.testng.Assert.assertEquals;

import org.testng.SkipException;
import org.testng.annotations.DataProvider;
import org.testng.annotations.Test;

public class NgCalcTest {

    @Test
    public void maxFirst() {
        assertEquals(new Calc().max(5, 3), 5);
    }

    @Test
    public void maxSecond() {
        assertEquals(new Calc().max(2, 7), 7); // fails: the fault in Calc.max
    }

    @DataProvider
    public Object[][] sizes() {
        return new Object[][] { { 1 }, { 3 } };
    }

    @Test(dataProvider = "sizes")
    public void sum(final int n) {
        assertEquals(new Calc().sum(n), n * (n - 1) / 2);
    }

    @Test
    public void skipped() {
        throw new SkipException("not counted, like a failed assumption");
    }
}
