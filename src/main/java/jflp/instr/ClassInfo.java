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

    /**
     * Per probe, what within its line it stands for: 0 for line probes; for
     * branch probes {@code (jump << 16) | edge}, where jump is the 1-based index of
     * the conditional jump or switch on the line and edge is
     * {@link jflp.data.RawFormat#EDGE_TAKEN}, {@link jflp.data.RawFormat#EDGE_NOT_TAKEN}
     * or, for a switch, {@code EDGE_TARGET + n} for its n-th distinct target.
     */
    public final int[] probeArgs;

    /** For DUA probes: {method name+descriptor, variable, def line, target line}; null otherwise. */
    public final Object[][] probeDuas;

    public ClassInfo(final String name, final String source, final long crc,
            final int[] probeLines, final byte[] probeKinds) {
        this(name, source, crc, probeLines, probeKinds, new int[probeLines.length]);
    }

    public ClassInfo(final String name, final String source, final long crc,
            final int[] probeLines, final byte[] probeKinds, final int[] probeArgs) {
        this(name, source, crc, probeLines, probeKinds, probeArgs, new Object[probeLines.length][]);
    }

    public ClassInfo(final String name, final String source, final long crc, final int[] probeLines,
            final byte[] probeKinds, final int[] probeArgs, final Object[][] probeDuas) {
        this.name = name;
        this.source = source;
        this.crc = crc;
        this.probeLines = probeLines;
        this.probeKinds = probeKinds;
        this.probeArgs = probeArgs;
        this.probeDuas = probeDuas;
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
