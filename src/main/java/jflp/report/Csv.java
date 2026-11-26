package jflp.report;

final class Csv {

    private Csv() {
    }

    /** RFC 4180 quoting; test names of parameterized tests contain commas. */
    static String quote(final String s) {
        if (s.indexOf(',') < 0 && s.indexOf('"') < 0 && s.indexOf('\n') < 0 && s.indexOf('\r') < 0) {
            return s;
        }
        return '"' + s.replace("\"", "\"\"") + '"';
    }
}
