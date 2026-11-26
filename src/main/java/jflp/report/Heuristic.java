package jflp.report;

/**
 * The ten suspiciousness formulas of Jaguar and Jaguar2, with the same guards
 * and the same results, except that all arithmetic is done in double.
 * Jaguar2 multiplies counts as int, which overflows on large suites (Zoltar
 * already at cnf * cep above 214748), silently producing wrong rankings.
 */
public enum Heuristic {

    OCHIAI {
        @Override
        public double eval(final double cef, final double cnf, final double cep, final double cnp) {
            return cef > 0 ? cef / Math.sqrt((cef + cnf) * (cef + cep)) : 0;
        }
    },
    TARANTULA {
        @Override
        public double eval(final double cef, final double cnf, final double cep, final double cnp) {
            if (cef <= 0) {
                return 0;
            }
            if (cep <= 0) {
                return 1;
            }
            final double f = cef / (cef + cnf);
            return f / (f + cep / (cep + cnp));
        }
    },
    JACCARD {
        @Override
        public double eval(final double cef, final double cnf, final double cep, final double cnp) {
            return cef > 0 ? cef / (cef + cnf + cep) : 0;
        }
    },
    ZOLTAR {
        @Override
        public double eval(final double cef, final double cnf, final double cep, final double cnp) {
            return cef > 0 ? cef / (cef + cnf + cep + (10000 * cnf * cep / cef)) : 0;
        }
    },
    OP {
        @Override
        public double eval(final double cef, final double cnf, final double cep, final double cnp) {
            return cef - cep / (cep + cnp + 1);
        }
    },
    MINUS {
        @Override
        public double eval(final double cef, final double cnf, final double cep, final double cnp) {
            double susp = 0;
            if (cef > 0) {
                susp = cep > 0 ? (cef / (cef + cnf)) / (cef / (cef + cnf) + cep / (cep + cnp)) : 1;
            }
            final double fMinus = cef + cnf > 0 ? cef / (cef + cnf) : 0;
            final double pMinus = cep + cnp > 0 ? cep / (cep + cnp) : 0;
            final double sMinus = fMinus != 1 ? (1 - fMinus) / (2 - fMinus - pMinus) : 0;
            return susp - sMinus;
        }
    },
    MCCON {
        @Override
        public double eval(final double cef, final double cnf, final double cep, final double cnp) {
            final double f = cef + cnf;
            final double p = cef + cep;
            return f > 0 && p > 0 ? (cef * cef - cnf * cep) / (f * p) : 0;
        }
    },
    DRT {
        @Override
        public double eval(final double cef, final double cnf, final double cep, final double cnp) {
            return cef > 0 ? cef / (1 + cep / (cef + cep + cnf + cnp)) : 0;
        }
    },
    KULCZYNSKI2 {
        @Override
        public double eval(final double cef, final double cnf, final double cep, final double cnp) {
            return cef > 0 ? 0.5 * (cef / (cef + cnf) + cef / (cef + cep)) : 0;
        }
    },
    WONG3 {
        @Override
        public double eval(final double cef, final double cnf, final double cep, final double cnp) {
            final double passed;
            if (cep > 2 && cep <= 10) {
                passed = 2 + 0.1 * (cep - 2);
            } else if (cep > 10) {
                passed = 2.8 + 0.001 * (cep - 10);
            } else {
                passed = cep;
            }
            return cef - passed;
        }
    };

    public abstract double eval(double cef, double cnf, double cep, double cnp);

    public static Heuristic byName(final String name) {
        for (final Heuristic h : values()) {
            if (h.name().equalsIgnoreCase(name)) {
                return h;
            }
        }
        final StringBuilder known = new StringBuilder();
        for (final Heuristic h : values()) {
            known.append(known.length() == 0 ? "" : ", ").append(h.name().toLowerCase());
        }
        throw new IllegalArgumentException("unknown heuristic '" + name + "' (known: " + known + ")");
    }
}
