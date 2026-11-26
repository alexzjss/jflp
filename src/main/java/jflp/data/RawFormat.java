package jflp.data;

import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.UnsupportedEncodingException;

/**
 * Binary layout of the per-JVM coverage files written by the agent.
 *
 * <pre>
 * file    := MAGIC VERSION record* END?
 * record  := CLASS base:int name:str source:str crc:long n:int (line:int kind:byte){n}
 *          | TEST outcome:byte name:str count:varint (delta:varint){count}
 *          | WARN message:str
 * </pre>
 *
 * A missing END means the JVM died before shutdown; every record before that
 * point is complete because records are flushed one at a time.
 */
public final class RawFormat {

    public static final int MAGIC = 0x4A464C50; // "JFLP"
    public static final int VERSION = 1;

    public static final byte CLASS = 'C';
    public static final byte TEST = 'T';
    public static final byte WARN = 'W';
    public static final byte END = 'E';

    public static final byte PASSED = 0;
    public static final byte FAILED = 1;

    /** Probe kind: execution reached the start of a source line. */
    public static final byte KIND_LINE = 0;

    public static final String SUFFIX = ".jflp";

    private RawFormat() {
    }

    public static void writeVarInt(final DataOutputStream out, int v) throws IOException {
        while ((v & ~0x7F) != 0) {
            out.writeByte((v & 0x7F) | 0x80);
            v >>>= 7;
        }
        out.writeByte(v);
    }

    public static int readVarInt(final DataInputStream in) throws IOException {
        int v = 0;
        for (int shift = 0; shift < 35; shift += 7) {
            final int b = in.readUnsignedByte();
            v |= (b & 0x7F) << shift;
            if ((b & 0x80) == 0) {
                return v;
            }
        }
        throw new IOException("malformed varint");
    }

    public static void writeString(final DataOutputStream out, final String s) throws IOException {
        final byte[] b = utf8(s);
        writeVarInt(out, b.length);
        out.write(b);
    }

    public static String readString(final DataInputStream in) throws IOException {
        final int len = readVarInt(in);
        if (len < 0) {
            throw new IOException("malformed string length");
        }
        final byte[] b = new byte[len];
        in.readFully(b);
        return new String(b, "UTF-8");
    }

    /** Ids are written as deltas because they arrive sorted and are usually dense. */
    public static void writeIds(final DataOutputStream out, final int[] ids) throws IOException {
        writeVarInt(out, ids.length);
        int prev = 0;
        for (int i = 0; i < ids.length; i++) {
            writeVarInt(out, ids[i] - prev);
            prev = ids[i];
        }
    }

    public static int[] readIds(final DataInputStream in) throws IOException {
        final int n = readVarInt(in);
        final int[] ids = new int[n];
        int prev = 0;
        for (int i = 0; i < n; i++) {
            prev += readVarInt(in);
            ids[i] = prev;
        }
        return ids;
    }

    static boolean isEof(final IOException e) {
        return e instanceof EOFException;
    }

    private static byte[] utf8(final String s) {
        try {
            return s.getBytes("UTF-8");
        } catch (final UnsupportedEncodingException e) {
            throw new IllegalStateException(e);
        }
    }
}
