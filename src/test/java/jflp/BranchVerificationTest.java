package jflp;

import static org.junit.Assert.assertTrue;

import java.io.File;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import jflp.instr.Instrumenter;
import jflp.rt.Probes;

import org.junit.Test;

/**
 * Branch probes add jump targets, and with them stack map frames computed by
 * jflp. A wrong frame makes the JVM reject the class at link time, so every
 * class of real jars is instrumented, defined and initialised here; the JVM's
 * own verifier is the judge. Errors other than verification (a class whose
 * static initialiser needs something absent) are the code's own business.
 */
public class BranchVerificationTest {

    @Test
    public void instrumentedThirdPartyClassesVerify() throws Exception {
        final Map<String, byte[]> classes = new HashMap<String, byte[]>();
        for (final String jar : System.getProperty("java.class.path").split(File.pathSeparator)) {
            if (jar.matches(".*(junit-4|asm-9|asm-tree-9|asm-commons-9|hamcrest-core|org.jacoco.core).*\\.jar")) {
                for (final byte[] b : LineSetVsJaCoCoTest.classesOf(new File(jar))) {
                    final Instrumenter.Analysis a = Instrumenter.analyze(b, true);
                    if (a != null) {
                        final int base = Probes.allocate(a.info.probeCount());
                        classes.put(a.info.name.replace('/', '.'), Instrumenter.instrument(a, base));
                    }
                }
            }
        }
        assertTrue("corpus not found", classes.size() > 500);
        final ClassLoader loader = new ClassLoader(BranchVerificationTest.class.getClassLoader()) {
            @Override
            protected synchronized Class<?> loadClass(final String name, final boolean resolve)
                    throws ClassNotFoundException {
                final byte[] b = classes.get(name);
                if (b == null) {
                    return super.loadClass(name, resolve);
                }
                Class<?> c = findLoadedClass(name);
                if (c == null) {
                    c = defineClass(name, b, 0, b.length);
                }
                return c;
            }
        };
        final List<String> rejected = new ArrayList<String>();
        for (final String name : classes.keySet()) {
            try {
                Class.forName(name, true, loader);
            } catch (final VerifyError e) {
                rejected.add(name + ": " + e.getMessage());
            } catch (final ClassFormatError e) {
                rejected.add(name + ": " + e.getMessage());
            } catch (final Throwable e) {
                // initialisation failures unrelated to bytecode validity
            }
        }
        assertTrue(rejected.size() + " of " + classes.size() + " rejected: "
                + rejected.subList(0, Math.min(5, rejected.size())), rejected.isEmpty());
    }
}
