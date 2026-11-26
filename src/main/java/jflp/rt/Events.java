package jflp.rt;

/**
 * Receives test events from hooked framework classes through the holder field
 * ({@code holder.$jflpEvents.equals(new Object[] {event, argument})}), which
 * every class loader can link, unlike a call into {@code jflp.rt}.
 */
final class Events {

    @Override
    public boolean equals(final Object o) {
        final Object[] e = (Object[]) o;
        final String event = (String) e[0];
        final Object arg = e[1];
        if ("j4Started".equals(event)) {
            T.j4Started(arg);
        } else if ("j4Finished".equals(event)) {
            T.j4Finished(arg);
        } else if ("j4Failure".equals(event)) {
            T.j4Failure(arg);
        } else if ("j4Assumption".equals(event)) {
            T.j4Assumption(arg);
        } else if ("j3Started".equals(event)) {
            T.j3Started(arg);
        } else if ("j3Finished".equals(event)) {
            T.j3Finished(arg);
        } else if ("j3Failure".equals(event)) {
            T.j3Failure(arg);
        }
        return false;
    }

    @Override
    public int hashCode() {
        return 0;
    }
}
