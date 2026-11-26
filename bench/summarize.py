#!/usr/bin/env python3
"""Rebuilds the comparison table from the raw outputs the harnesses leave in WORK/<bug>/.

results.csv is written incrementally and its columns changed as the harnesses
grew; this recomputes every number from the artifacts themselves, so a table
can always be traced back to files.

Times are test-run times as the runner reports them (JUnitCore's "Time:" for
Defects4J, Maven's "Total time" for Bugs.jar), without JFLP's report step.

Usage: bench/summarize.py --d4j-work DIR --bugsjar-work DIR [--d4j DEFECTS4J_HOME] [--markdown]
"""

import argparse
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sbfl_common import (class_lines, failing, map_to_classes, patch_lines, rank, read_csv,  # noqa: E402
                         rows_matched, same_file, test_order)


def junit_time(log):
    if not os.path.exists(log):
        return None
    t = None
    for line in open(log, errors="replace"):
        m = re.match(r"^Time: ([\d.,]+)", line)
        if m:
            t = float(m.group(1).replace(",", ""))
    return t


def maven_time(log):
    if not os.path.exists(log):
        return None
    t = None
    for line in open(log, errors="replace"):
        m = re.search(r"Total time:\s+(?:(\d+):)?([\d.]+)\s*(min|s)", line)
        if m:
            t = (float(m.group(1) or 0) * 60 + float(m.group(2))) if m.group(3) == "s" or m.group(1) \
                else float(m.group(2)) * 60
    return t


def matrix_failing(matrix):
    if not os.path.exists(matrix):
        return None
    return sum(1 for line in open(matrix) if line.rstrip().endswith("-"))


def compare(row, j2_csv, j2_matrix, d):
    stable = test_order(os.path.join(d, "jflp", "coverageMatrix.tests.csv")) == \
        test_order(os.path.join(d, "jflp_jc", "coverageMatrix.tests.csv"))
    row["order"] = "stable" if stable else "varies"
    jc = os.path.join(d, "jflp_jc")
    if not os.path.exists(j2_csv):
        row["jc_csv"] = "no Jaguar2 CSV"
    elif same_file(j2_csv, os.path.join(jc, "jaguar2.csv")):
        row["jc_csv"] = "identical"
    else:
        m = rows_matched(j2_csv, os.path.join(jc, "jaguar2.csv"))
        row["jc_csv"] = "%d/%d rows" % m if m else "—"
    if not os.path.exists(j2_matrix):
        row["jc_matrix"] = "no Jaguar2 matrix"
    elif same_file(j2_matrix, os.path.join(jc, "coverageMatrix.txt")):
        row["jc_matrix"] = "identical"
    else:
        m = rows_matched(j2_matrix, os.path.join(jc, "coverageMatrix.txt"))
        row["jc_matrix"] = "%d/%d rows" % m if m else "—"


def ranks(row, patch, side, j2_csv, d):
    universe = class_lines(os.path.join(d, "jflp", "coverageMatrix.columns.csv"))
    targets = map_to_classes(patch_lines(patch, side), universe) if os.path.exists(patch) else set()
    if not targets:
        row["rank_jaguar2"] = row["rank_jflp"] = "n/a"
        return
    for key, path in (("rank_jaguar2", j2_csv), ("rank_jflp", os.path.join(d, "jflp", "jaguar2.csv"))):
        row[key] = rank({k: v[4] for k, v in read_csv(path).items()}, universe, targets) \
            if os.path.exists(path) else "—"


def d4j_rows(work, d4j_home):
    for bug in sorted(os.listdir(work)):
        d = os.path.join(work, bug)
        if not os.path.isdir(os.path.join(d, "jflp")):
            continue
        project, bug_id = bug.split("-")
        trigger = os.path.join(d4j_home, "framework", "projects", project, "trigger_tests", bug_id)
        row = {"benchmark": "Defects4J", "bug": bug}
        row["failing_expected"] = sum(1 for l in open(trigger) if l.startswith("--- ")) if os.path.exists(trigger) else "?"
        j2 = os.path.join(d, "jaguar2", "target")
        row["failing_jaguar2"] = matrix_failing(os.path.join(j2, "coverageMatrix.txt"))
        row["failing_jflp"] = len(failing(os.path.join(d, "jflp", "coverageMatrix.tests.csv")))
        row["failing_jflp_d4j_runner"] = len(failing(os.path.join(d, "d4j_runner", "coverageMatrix.tests.csv")))
        row["tests"] = len(test_order(os.path.join(d, "jflp", "coverageMatrix.tests.csv")) or [])
        for mode, log in (("plain", "plain/run.log"), ("jaguar2", "jaguar2/run.log"), ("jflp", "jflp/run.log"),
                          ("jflp_jc", "jflp_jc/run.log")):
            row["time_" + mode] = junit_time(os.path.join(d, log))
        compare(row, os.path.join(j2, "jaguar2.csv"), os.path.join(j2, "coverageMatrix.txt"), d)
        patch = os.path.join(d4j_home, "framework", "projects", project, "patches", "%s.src.patch" % bug_id)
        ranks(row, patch, "+", os.path.join(j2, "jaguar2.csv"), d)
        yield row


def bugsjar_rows(work):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from bugsjar import bugsjar_failing
    for bug in sorted(os.listdir(work)):
        d = os.path.join(work, bug)
        if not os.path.isdir(os.path.join(d, "jflp")):
            continue
        meta = os.path.join(d, "checkout", ".bugs-dot-jar")
        row = {"benchmark": "Bugs.jar", "bug": bug}
        row["failing_expected"] = len(bugsjar_failing(os.path.join(meta, "test-results.txt")))
        j2 = os.path.join(d, "jaguar2")
        row["failing_jaguar2"] = matrix_failing(os.path.join(j2, "coverageMatrix.txt"))
        row["failing_jflp"] = len(failing(os.path.join(d, "jflp", "coverageMatrix.tests.csv")))
        row["failing_jflp_d4j_runner"] = ""
        row["tests"] = len(test_order(os.path.join(d, "jflp", "coverageMatrix.tests.csv")) or [])
        for mode, log in (("plain", "plain.log"), ("jaguar2", "jaguar2.log"), ("jflp", "jflp/run.log"),
                          ("jflp_jc", "jflp_jc/run.log")):
            row["time_" + mode] = maven_time(os.path.join(d, log))
        compare(row, os.path.join(j2, "jaguar2.csv"), os.path.join(j2, "coverageMatrix.txt"), d)
        ranks(row, os.path.join(meta, "developer-patch.diff"), "-", os.path.join(j2, "jaguar2.csv"), d)
        yield row


COLUMNS = ["benchmark", "bug", "tests", "failing_expected", "failing_jaguar2", "failing_jflp",
           "failing_jflp_d4j_runner", "time_plain", "time_jaguar2", "time_jflp", "time_jflp_jc", "rank_jaguar2",
           "rank_jflp", "order", "jc_csv", "jc_matrix"]


def fmt(v):
    if v is None or v == "":
        return "—"
    if isinstance(v, float):
        return ("%.1f" % v).rstrip("0").rstrip(".") if v != int(v) or v < 1000 else "%d" % v
    return str(v)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--d4j-work")
    ap.add_argument("--d4j")
    ap.add_argument("--bugsjar-work")
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args()
    rows = []
    if args.d4j_work:
        rows += list(d4j_rows(args.d4j_work, args.d4j))
    if args.bugsjar_work:
        rows += list(bugsjar_rows(args.bugsjar_work))
    if args.markdown:
        print("| " + " | ".join(COLUMNS) + " |")
        print("|" + "---|" * len(COLUMNS))
        for r in rows:
            print("| " + " | ".join(fmt(r.get(c)) for c in COLUMNS) + " |")
    else:
        w = csv.DictWriter(sys.stdout, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: fmt(r.get(c)) for c in COLUMNS})


if __name__ == "__main__":
    sys.exit(main())
