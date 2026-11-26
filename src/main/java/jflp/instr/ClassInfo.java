package jflp.instr;

/**
 * What a class contributes to the spectra: its probes and the source line of each.
 * Produced identically by the agent (at load time) and by the report (for classes
 * that no test loaded), so both sides agree on every class's line set.
 */
public final class ClassInfo {

    /** VM name, e.g. {@code org/foo/Bar$Inner}. */
    public final String name;

    /** Source file name from the class file, or {@code null} when compiled without it. */
    public final String source;

    public final long crc;

    /** Source line of each probe, indexed by local probe id. */
    public final int[] probeLines;

    public final byte[] probeKinds;

    public ClassInfo(final String name, final String source, final long crc,
            final int[] probeLines, final byte[] probeKinds) {
        this.name = name;
        this.source = source;
        this.crc = crc;
        this.probeLines = probeLines;
        this.probeKinds = probeKinds;
    }

    public int probeCount() {
        return probeLines.length;
    }

    /** Package in VM form ({@code org/foo}), empty for the default package. */
    public String packageName() {
        final int i = name.lastIndexOf('/');
        return i < 0 ? "" : name.substring(0, i);
    }
}
