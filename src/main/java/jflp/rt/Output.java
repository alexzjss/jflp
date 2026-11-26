package jflp.rt;

import java.io.BufferedOutputStream;
import java.io.DataOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;

import jflp.data.RawFormat;
import jflp.instr.ClassInfo;

/**
 * The agent's per-JVM coverage file. Every record is flushed when written, so
 * a JVM killed mid-run (timeout, System.exit in a test) keeps all tests that
 * completed before it died.
 */
public final class Output {

    private final File file;
    private DataOutputStream out;

    Output(final File dir) throws IOException {
        if (!dir.isDirectory() && !dir.mkdirs()) {
            throw new IOException("cannot create " + dir);
        }
        String pid = java.lang.management.ManagementFactory.getRuntimeMXBean().getName();
        pid = pid.replaceAll("[^A-Za-z0-9]", "_");
        file = new File(dir, "jflp-" + System.currentTimeMillis() + "-" + pid + "-"
                + Long.toHexString(System.nanoTime()) + RawFormat.SUFFIX);
        out = new DataOutputStream(new BufferedOutputStream(new FileOutputStream(file), 1 << 16));
        out.writeInt(RawFormat.MAGIC);
        out.writeInt(RawFormat.VERSION);
        out.flush();
    }

    File file() {
        return file;
    }

    synchronized void classDefined(final int base, final ClassInfo c) {
        if (out == null) {
            return;
        }
        try {
            out.writeByte(RawFormat.CLASS);
            out.writeInt(base);
            RawFormat.writeString(out, c.name);
            RawFormat.writeString(out, c.source == null ? "" : c.source);
            out.writeLong(c.crc);
            out.writeInt(c.probeCount());
            for (int i = 0; i < c.probeCount(); i++) {
                out.writeInt(c.probeLines[i]);
                out.writeByte(c.probeKinds[i]);
            }
            out.flush();
        } catch (final IOException e) {
            lost(e);
        }
    }

    synchronized void test(final boolean failed, final String name, final int[] ids) {
        if (out == null) {
            return;
        }
        try {
            out.writeByte(RawFormat.TEST);
            out.writeByte(failed ? RawFormat.FAILED : RawFormat.PASSED);
            RawFormat.writeString(out, name);
            RawFormat.writeIds(out, ids);
            out.flush();
        } catch (final IOException e) {
            lost(e);
        }
    }

    synchronized void warn(final String message) {
        if (out == null) {
            return;
        }
        try {
            out.writeByte(RawFormat.WARN);
            RawFormat.writeString(out, message);
            out.flush();
        } catch (final IOException e) {
            lost(e);
        }
    }

    synchronized void close() {
        if (out == null) {
            return;
        }
        try {
            out.writeByte(RawFormat.END);
            out.close();
        } catch (final IOException e) {
            lost(e);
        }
        out = null;
    }

    private void lost(final IOException e) {
        System.err.println("[jflp] ERROR writing " + file + ": " + e + " (coverage of this JVM is incomplete)");
        try {
            out.close();
        } catch (final IOException ignored) {
            // already failing
        }
        out = null;
    }
}
