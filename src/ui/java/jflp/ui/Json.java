package jflp.ui;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Minimal JSON writer and parser: Map, List, String, Number, Boolean, null. */
final class Json {

    private Json() {
    }

    static String write(final Object v) {
        final StringBuilder b = new StringBuilder();
        write(b, v);
        return b.toString();
    }

    private static void write(final StringBuilder b, final Object v) {
        if (v == null) {
            b.append("null");
        } else if (v instanceof String) {
            string(b, (String) v);
        } else if (v instanceof Boolean) {
            b.append(v.toString());
        } else if (v instanceof Double || v instanceof Float) {
            final double d = ((Number) v).doubleValue();
            b.append(Double.isNaN(d) || Double.isInfinite(d) ? "null" : Double.toString(d));
        } else if (v instanceof Number) {
            b.append(v.toString());
        } else if (v instanceof Map) {
            b.append('{');
            boolean first = true;
            for (final Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (!first) {
                    b.append(',');
                }
                first = false;
                string(b, String.valueOf(e.getKey()));
                b.append(':');
                write(b, e.getValue());
            }
            b.append('}');
        } else if (v instanceof Iterable) {
            b.append('[');
            boolean first = true;
            for (final Object o : (Iterable<?>) v) {
                if (!first) {
                    b.append(',');
                }
                first = false;
                write(b, o);
            }
            b.append(']');
        } else if (v instanceof Object[]) {
            final List<Object> l = new ArrayList<>();
            for (final Object o : (Object[]) v) {
                l.add(o);
            }
            write(b, l);
        } else {
            string(b, v.toString());
        }
    }

    private static void string(final StringBuilder b, final String s) {
        b.append('"');
        for (int i = 0; i < s.length(); i++) {
            final char c = s.charAt(i);
            switch (c) {
            case '"':
                b.append("\\\"");
                break;
            case '\\':
                b.append("\\\\");
                break;
            case '\n':
                b.append("\\n");
                break;
            case '\r':
                b.append("\\r");
                break;
            case '\t':
                b.append("\\t");
                break;
            default:
                if (c < 0x20 || c == 0x2028 || c == 0x2029) {
                    b.append(String.format("\\u%04x", (int) c));
                } else {
                    b.append(c);
                }
            }
        }
        b.append('"');
    }

    static Object parse(final String s) {
        final Parser p = new Parser(s);
        p.ws();
        final Object v = p.value();
        p.ws();
        if (p.i != s.length()) {
            throw new IllegalArgumentException("trailing characters at " + p.i);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> object(final String s) {
        final Object v = parse(s);
        if (!(v instanceof Map)) {
            throw new IllegalArgumentException("expected a JSON object");
        }
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        final String s;
        int i;

        Parser(final String s) {
            this.s = s;
        }

        void ws() {
            while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
                i++;
            }
        }

        Object value() {
            if (i >= s.length()) {
                throw new IllegalArgumentException("unexpected end of JSON");
            }
            final char c = s.charAt(i);
            if (c == '{') {
                return obj();
            }
            if (c == '[') {
                return arr();
            }
            if (c == '"') {
                return str();
            }
            if (s.startsWith("true", i)) {
                i += 4;
                return Boolean.TRUE;
            }
            if (s.startsWith("false", i)) {
                i += 5;
                return Boolean.FALSE;
            }
            if (s.startsWith("null", i)) {
                i += 4;
                return null;
            }
            return num();
        }

        Map<String, Object> obj() {
            final Map<String, Object> m = new LinkedHashMap<>();
            i++;
            ws();
            if (s.charAt(i) == '}') {
                i++;
                return m;
            }
            while (true) {
                ws();
                final String k = str();
                ws();
                expect(':');
                ws();
                m.put(k, value());
                ws();
                if (s.charAt(i) == ',') {
                    i++;
                    continue;
                }
                expect('}');
                return m;
            }
        }

        List<Object> arr() {
            final List<Object> l = new ArrayList<>();
            i++;
            ws();
            if (s.charAt(i) == ']') {
                i++;
                return l;
            }
            while (true) {
                ws();
                l.add(value());
                ws();
                if (s.charAt(i) == ',') {
                    i++;
                    continue;
                }
                expect(']');
                return l;
            }
        }

        String str() {
            expect('"');
            final StringBuilder b = new StringBuilder();
            while (true) {
                final char c = s.charAt(i++);
                if (c == '"') {
                    return b.toString();
                }
                if (c == '\\') {
                    final char e = s.charAt(i++);
                    switch (e) {
                    case 'n':
                        b.append('\n');
                        break;
                    case 't':
                        b.append('\t');
                        break;
                    case 'r':
                        b.append('\r');
                        break;
                    case 'b':
                        b.append('\b');
                        break;
                    case 'f':
                        b.append('\f');
                        break;
                    case 'u':
                        b.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                        i += 4;
                        break;
                    default:
                        b.append(e);
                    }
                } else {
                    b.append(c);
                }
            }
        }

        Object num() {
            final int start = i;
            while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) {
                i++;
            }
            final String n = s.substring(start, i);
            if (n.isEmpty()) {
                throw new IllegalArgumentException("unexpected character '" + s.charAt(start) + "' at " + start);
            }
            if (n.indexOf('.') < 0 && n.indexOf('e') < 0 && n.indexOf('E') < 0) {
                return Long.valueOf(n);
            }
            return Double.valueOf(n);
        }

        void expect(final char c) {
            if (i >= s.length() || s.charAt(i) != c) {
                throw new IllegalArgumentException("expected '" + c + "' at " + i);
            }
            i++;
        }
    }
}
