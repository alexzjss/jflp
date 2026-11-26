"""Helpers shared by the benchmark harnesses."""

import csv
import os
import re
import subprocess
import time


def patch_lines(patch, buggy_side):
    """Buggy-version lines touched by a fix, from a unified diff.

    buggy_side is '+' when the diff goes from fixed to buggy (Defects4J) and '-'
    when it goes from buggy to fixed (Bugs.jar, Bears). A hunk that only adds
    code on the fixed side is a fault of omission; it marks the buggy line right
    after the insertion point. Returns {(path, line)}.
    """
    fixed_side = "-" if buggy_side == "+" else "+"
    result = set()
    path = None
    line_no = 0
    pending = False   # fixed-side lines seen with no buggy line yet in this run
    for raw in open(patch, encoding="utf-8", errors="replace"):
        if raw.startswith("+++ ") or raw.startswith("--- "):
            if raw.startswith(buggy_side * 3 + " "):
                p = raw[4:].strip().split("\t")[0]
                path = p[2:] if p[:2] in ("a/", "b/") else p
            continue
        if raw.startswith("diff "):
            continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            line_no = int(m.group(1) if buggy_side == "-" else m.group(2))
            pending = False
            continue
        if path is None or not raw:
            continue
        if raw[0] == buggy_side:
            result.add((path, line_no))
            line_no += 1
            pending = False
        elif raw[0] == fixed_side:
            pending = True
        elif raw[0] == " ":
            if pending:
                result.add((path, line_no))
            pending = False
            line_no += 1
    return result


def class_lines(columns_csv):
    """All code lines, [(vm class name, line)], from coverageMatrix.columns.csv."""
    if not os.path.exists(columns_csv):
        return []
    return [(r[1].replace(".", "/"), int(r[2])) for r in list(csv.reader(open(columns_csv)))[1:]]


def map_to_classes(patch_hits, universe):
    """Buggy (source path, line) pairs -> the (class, line) columns that hold code."""
    targets = set()
    for path, line in patch_hits:
        if not path.endswith(".java"):
            continue
        base = re.sub(r"^.*?/(org|com|net|junit|io|de|fr|nl)/", r"\1/", "/" + path)[:-len(".java")]
        for name, l in universe:
            if l == line and (name == base or name.startswith(base + "$")):
                targets.add((name, l))
    return targets


def run(cmd, cwd, env=None, timeout=3600, log=None):
    start = time.time()
    with open(log, "w") if log else open(os.devnull, "w") as out:
        p = subprocess.run(cmd, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, time.time() - start


def read_csv(path):
    rows = {}
    if os.path.exists(path):
        for r in csv.reader(open(path)):
            if len(r) == 7:
                rows[(r[0], int(r[1]))] = (int(r[2]), int(r[3]), int(r[4]), int(r[5]), float(r[6]))
    return rows


def failing(rows_csv):
    """Failing tests from a coverageMatrix.tests.csv."""
    out = set()
    if os.path.exists(rows_csv):
        for r in list(csv.reader(open(rows_csv)))[1:]:
            if r[1] == "FAIL":
                out.add(r[2])
    return out


def jaguar2_failing(log):
    """Failing tests as printed by JUnit's TextListener: '1) name(Class)'."""
    out = set()
    if os.path.exists(log):
        for line in open(log, errors="replace"):
            m = re.match(r"^\d+\) (.+\(.+\))$", line.rstrip())
            if m:
                out.add(m.group(1))
    return out


def rank(scores, universe, targets):
    """Average rank (ties share the mean position) of the best-ranked target line."""
    best = None
    for t in targets:
        s = scores.get(t, 0.0)
        ahead = sum(1 for u in universe if scores.get(u, 0.0) > s)
        ties = sum(1 for u in universe if scores.get(u, 0.0) == s)
        r = ahead + (ties + 1) / 2.0
        best = r if best is None else min(best, r)
    return best


def matrix_diff(a, b):
    if not (os.path.exists(a) and os.path.exists(b)):
        return None
    cells = up = down = 0
    with open(a) as fa, open(b) as fb:
        for la, lb in zip(fa, fb):
            ra, rb = la.split(), lb.split()
            if len(ra) != len(rb):
                return None
            for x, y in zip(ra[:-1], rb[:-1]):
                if x != y:
                    cells += 1
                    if x == "0":
                        up += 1
                    else:
                        down += 1
    return cells, up, down


def same_file(a, b):
    return os.path.exists(a) and os.path.exists(b) and open(a, "rb").read() == open(b, "rb").read()


