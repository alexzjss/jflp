package jflp.report;

final class Xml {

    private Xml() {
    }

    /** Escapes an attribute value as the JDK's XMLStreamWriter does. */
    static String escape(final String s) {
        final StringBuilder b = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            final char c = s.charAt(i);
            switch (c) {
            case '&':
                b.append("&amp;");
                break;
            case '<':
                b.append("&lt;");
                break;
            case '>':
                b.append("&gt;");
                break;
            case '"':
                b.append("&quot;");
                break;
            default:
                b.append(c);
            }
        }
        return b.toString();
    }
}
