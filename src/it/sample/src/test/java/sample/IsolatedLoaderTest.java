package sample;

import static org.junit.Assert.assertEquals;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;

import org.junit.Test;

/**
 * Loads Calc through a loader that, like Ant's isolated AntClassLoader, only
 * delegates java.* to its parent. Instrumented code must still reach the
 * coverage runtime, or this fails with NoClassDefFoundError.
 */
public class IsolatedLoaderTest {

    static final class Isolated extends ClassLoader {
        Isolated() {
            super(null);
        }

        @Override
        protected Class<?> loadClass(final String name, final boolean resolve) throws ClassNotFoundException {
            if (name.startsWith("java.")) {
                return super.loadClass(name, resolve);
            }
            synchronized (this) {
                Class<?> c = findLoadedClass(name);
                if (c == null) {
                    final byte[] b = read(name.replace('.', '/') + ".class");
                    if (b == null) {
                        throw new ClassNotFoundException(name + " (isolated loader sees only java.* and sample.*)");
                    }
                    c = defineClass(name, b, 0, b.length);
                }
                return c;
            }
        }

        private static byte[] read(final String resource) {
            if (!resource.startsWith("sample/")) {
                return null;
            }
            try {
                final InputStream in = IsolatedLoaderTest.class.getClassLoader().getResourceAsStream(resource);
                if (in == null) {
                    return null;
                }
                final ByteArrayOutputStream out = new ByteArrayOutputStream();
                final byte[] buf = new byte[4096];
                for (int n; (n = in.read(buf)) > 0;) {
                    out.write(buf, 0, n);
                }
                in.close();
                return out.toByteArray();
            } catch (final java.io.IOException e) {
                return null;
            }
        }
    }

    @Test
    public void instrumentedClassRunsInIsolatedLoader() throws Exception {
        final Class<?> calc = new Isolated().loadClass("sample.Calc");
        final Object c = calc.getConstructor().newInstance();
        assertEquals(6, calc.getMethod("sum", int.class).invoke(c, 4));
    }
}
