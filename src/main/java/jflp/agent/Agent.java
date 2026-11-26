package jflp.agent;

import java.io.File;
import java.lang.instrument.Instrumentation;
import java.lang.reflect.Method;
import java.util.jar.JarFile;

/**
 * {@code -javaagent:jflp.jar[=options]} entry point.
 *
 * The jar must be on the boot class path so that instrumented classes reach
 * the probe runtime from any class loader, including isolated ones. Java 6+
 * appends this jar there before any other jflp class is resolved; Java 5 has
 * no such call and relies on the manifest's {@code Boot-Class-Path}, which
 * only works while the jar keeps the name {@code jflp.jar}.
 */
public final class Agent {

    private Agent() {
    }

    public static void premain(final String args, final Instrumentation inst) throws Exception {
        appendSelfToBootClassPath(inst);
        final Class<?> c;
        try {
            c = Class.forName("jflp.agent.AgentMain", true, null);
        } catch (final ClassNotFoundException e) {
            throw new IllegalStateException("[jflp] the agent jar is not on the boot class path;"
                    + " on Java 5 the jar must be named jflp.jar", e);
        }
        c.getMethod("start", String.class, Instrumentation.class).invoke(null, args, inst);
    }

    private static void appendSelfToBootClassPath(final Instrumentation inst) throws Exception {
        if (Agent.class.getClassLoader() == null) {
            return; // the manifest's Boot-Class-Path already did it
        }
        final Method append;
        try {
            append = Instrumentation.class.getMethod("appendToBootstrapClassLoaderSearch", JarFile.class);
        } catch (final NoSuchMethodException java5) {
            return;
        }
        final File jar = new File(Agent.class.getProtectionDomain().getCodeSource().getLocation().toURI());
        append.invoke(inst, new JarFile(jar));
    }
}
