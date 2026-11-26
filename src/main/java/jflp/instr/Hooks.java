package jflp.instr;

import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.tree.ClassNode;
import org.objectweb.asm.tree.InsnList;
import org.objectweb.asm.tree.MethodNode;

/**
 * Test boundaries come from the test frameworks themselves, not from a build
 * listener, so no build file has to change and any launcher works (Surefire,
 * Ant, Gradle, IDE, Defects4J's runner).
 *
 * <ul>
 * <li>JUnit 4 (and JUnit 3 run by a JUnit 4 runner, and the JUnit 5 vintage
 * engine): {@code RunNotifier}.</li>
 * <li>JUnit 3 run directly (Ant, Surefire's junit3 provider): {@code TestResult}.</li>
 * <li>JUnit 5 (Jupiter and any other engine of the JUnit Platform): every class
 * implementing the platform's public {@code EngineExecutionListener}, which all
 * engines report through.</li>
 * <li>TestNG: every class implementing {@code ITestListener}, and the interface's
 * own default methods (TestNG 7), in case no listener overrides them.</li>
 * </ul>
 *
 * Adapters between the two report the same test twice, nested; the runtime
 * ignores a start for the test that is already running.
 */
public final class Hooks {

    private Hooks() {
    }

    /** Returns the hooked class, or {@code null} when {@code name} is not a hook point. */
    public static byte[] hook(final String name, final byte[] classFile) {
        if ("org/junit/runner/notification/RunNotifier".equals(name)) {
            return rewrite(classFile, new String[][] {
                { "fireTestStarted", "(Lorg/junit/runner/Description;)V", "j4Started" },
                { "fireTestFinished", "(Lorg/junit/runner/Description;)V", "j4Finished" },
                { "fireTestFailure", "(Lorg/junit/runner/notification/Failure;)V", "j4Failure" },
                { "fireTestAssumptionFailed", "(Lorg/junit/runner/notification/Failure;)V", "j4Assumption" },
            });
        }
        if ("junit/framework/TestResult".equals(name)) {
            return rewrite(classFile, new String[][] {
                { "startTest", "(Ljunit/framework/Test;)V", "j3Started" },
                { "endTest", "(Ljunit/framework/Test;)V", "j3Finished" },
                { "addError", "(Ljunit/framework/Test;Ljava/lang/Throwable;)V", "j3Failure" },
                { "addFailure", "(Ljunit/framework/Test;Ljunit/framework/AssertionFailedError;)V", "j3Failure" },
            });
        }
        if ("org/testng/ITestListener".equals(name) || !name.startsWith("java/") && !name.startsWith("sun/")
                && !name.startsWith("jdk/") && implementsInterface(classFile, "org/testng/ITestListener")) {
            return rewrite(classFile, new String[][] {
                { "onTestStart", "(Lorg/testng/ITestResult;)V", "ngStarted" },
                { "onTestSuccess", "(Lorg/testng/ITestResult;)V", "ngSuccess" },
                { "onTestFailure", "(Lorg/testng/ITestResult;)V", "ngFailure" },
                { "onTestFailedButWithinSuccessPercentage", "(Lorg/testng/ITestResult;)V", "ngFailure" },
                { "onTestFailedWithTimeout", "(Lorg/testng/ITestResult;)V", "ngFailure" },
                { "onTestSkipped", "(Lorg/testng/ITestResult;)V", "ngSkipped" },
            });
        }
        if (!name.startsWith("java/") && !name.startsWith("sun/") && !name.startsWith("jdk/")
                && implementsInterface(classFile, "org/junit/platform/engine/EngineExecutionListener")) {
            return rewrite(classFile, new String[][] {
                { "executionStarted", "(Lorg/junit/platform/engine/TestDescriptor;)V", "j5Started" },
                { "executionFinished", "(Lorg/junit/platform/engine/TestDescriptor;"
                        + "Lorg/junit/platform/engine/TestExecutionResult;)V", "j5Finished", "2" },
            });
        }
        return null;
    }

    /** True for a class (not an interface) that declares {@code iface} among its interfaces. */
    private static boolean implementsInterface(final byte[] classFile, final String iface) {
        final ClassReader r = new ClassReader(classFile);
        for (final String i : r.getInterfaces()) {
            if (iface.equals(i)) {
                return (r.getAccess() & org.objectweb.asm.Opcodes.ACC_INTERFACE) == 0;
            }
        }
        return false;
    }

    /** points: {method, descriptor, event[, "2" when the event also takes argument 2]}. */
    private static byte[] rewrite(final byte[] classFile, final String[][] points) {
        final ClassNode cn = new ClassNode();
        new ClassReader(classFile).accept(cn, 0);
        for (final Object o : cn.methods) {
            final MethodNode m = (MethodNode) o;
            for (final String[] p : points) {
                if (p[0].equals(m.name) && p[1].equals(m.desc) && m.instructions.size() > 0) {
                    final InsnList call = new InsnList();
                    m.maxStack += p.length > 3 ? Access.event(call, p[2], 1, 2) : Access.event(call, p[2], 1);
                    m.instructions.insert(call);
                }
            }
        }
        final ClassWriter cw = new ClassWriter(0);
        cn.accept(cw);
        return cw.toByteArray();
    }
}
