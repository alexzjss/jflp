package jflp.data;

import java.io.BufferedInputStream;
import java.io.DataInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;

/** Streams the records of one agent coverage file. */
public final class RawReader {

    public interface Visitor {
        void classRecord(int base, String name, String source, long crc, int[] lines, byte[] kinds)
                throws IOException;

        void testRecord(boolean failed, String name, int[] ids) throws IOException;

        void warning(String message);
    }

    private RawReader() {
    }

    /**
     * Reads {@code file}. Returns {@code true} when the file ended cleanly and
     * {@code false} when it was cut short (the JVM died); records before the
     * cut are still delivered.
     */
    public static boolean read(final File file, final Visitor v) throws IOException {
        final DataInputStream in = new DataInputStream(
                new BufferedInputStream(new FileInputStream(file), 1 << 16));
        try {
            if (in.readInt() != RawFormat.MAGIC) {
                throw new IOException(file + " is not a jflp coverage file");
            }
            final int version = in.readInt();
            if (version != RawFormat.VERSION) {
                throw new IOException(file + " has format version " + version + ", expected " + RawFormat.VERSION);
            }
            while (true) {
                final int tag = in.read();
                if (tag == -1) {
                    return false;
                }
                try {
                    switch (tag) {
                    case RawFormat.CLASS: {
                        final int base = in.readInt();
                        final String name = RawFormat.readString(in);
                        final String source = RawFormat.readString(in);
                        final long crc = in.readLong();
                        final int n = in.readInt();
                        final int[] lines = new int[n];
                        final byte[] kinds = new byte[n];
                        for (int i = 0; i < n; i++) {
                            lines[i] = in.readInt();
                            kinds[i] = in.readByte();
                        }
                        v.classRecord(base, name, source.length() == 0 ? null : source, crc, lines, kinds);
                        break;
                    }
                    case RawFormat.TEST: {
                        final boolean failed = in.readByte() == RawFormat.FAILED;
                        final String name = RawFormat.readString(in);
                        v.testRecord(failed, name, RawFormat.readIds(in));
                        break;
                    }
                    case RawFormat.WARN:
                        v.warning(RawFormat.readString(in));
                        break;
                    case RawFormat.END:
                        return true;
                    default:
                        throw new IOException(file + ": corrupt record tag " + tag);
                    }
                } catch (final IOException e) {
                    if (RawFormat.isEof(e)) {
                        return false; // partial last record of a killed JVM
                    }
                    throw e;
                }
            }
        } finally {
            in.close();
        }
    }
}
