#!/usr/bin/env python3
"""Compares JFLP with the Jaguar2 fork on Defects4J bugs.

For each bug the buggy version is checked out and compiled with Defects4J, then
its tests run three times with the same runner (JUnitCore), the same test list
and the same JDK:

  plain     no tool at all, the baseline for overhead
  jaguar2   the fork: JaCoCo 0.8.7 agent + JaguarTestRunner + CSV exporter
  jflp      JFLP's agent, default line semantics (a line is covered once reached)
  jflp-jc   JFLP's agent with lines=jacoco (should reproduce Jaguar2 exactly)

and once more through Defects4J's own runner (`defects4j test`) with the JFLP
agent in JAVA_TOOL_OPTIONS, to check that path and compare the failing tests
with the ones Defects4J reports.

Results go to WORK/results.csv (one row per bug) and WORK/<bug>/ (raw outputs).

Usage:
  bench/d4j.py --d4j DEFECTS4J_HOME --tools BENCH_TOOLS --jflp target/jflp.jar \
               --work DIR Lang-1 Lang-2 Math-5 ...
  (BENCH_TOOLS is the directory bench/build-jaguar2.sh wrote.)
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys

from sbfl_common import (class_lines, failing, jaguar2_failing, map_to_classes, matrix_diff, patch_lines, rank,
                         read_csv, run, same_file)

FIELDS = [
    "bug", "status", "tests", "failing_d4j", "failing_jaguar2", "failing_jflp", "failing_jflp_jc",
    "failing_d4j_runner_jflp", "time_plain_s", "time_jaguar2_s", "time_jflp_s", "time_jflp_jc_s", "lines",
    "jc_csv_identical", "jc_matrix_identical", "jflp_matrix_cells_differ", "jflp_matrix_0to1",
    "jflp_matrix_1to0", "buggy_lines", "rank_jaguar2", "rank_jflp", "note",
]


def d4j(args, bug_dir, *params, capture=True):
    env = dict(os.environ, PATH=os.path.join(args.d4j, "framework", "bin") + os.pathsep + os.environ["PATH"])
    p = subprocess.run(["defects4j", *params], cwd=bug_dir, env=env, capture_output=capture, text=True)
    if p.returncode != 0:
        raise RuntimeError("defects4j %s failed: %s" % (" ".join(params), (p.stderr or "")[-500:]))
    return p.stdout


def one_bug(args, bug, java):
    project, bug_id = bug.split("-")
    out = os.path.join(args.work, bug)
    src = os.path.join(out, "checkout")
    row = {"bug": bug, "status": "ok", "note": ""}
    os.makedirs(out, exist_ok=True)
    if not os.path.exists(os.path.join(src, ".defects4j.config")):
        shutil.rmtree(src, ignore_errors=True)
        d4j(args, out, "checkout", "-p", project, "-v", bug_id + "b", "-w", src)
    d4j(args, src, "compile")
    classes = os.path.join(src, d4j(args, src, "export", "-p", "dir.bin.classes").strip())
    tests_dir = os.path.join(src, d4j(args, src, "export", "-p", "dir.bin.tests").strip())
    cp = d4j(args, src, "export", "-p", "cp.test").strip()
    tests = d4j(args, src, "export", "-p", "tests.all").split()
    trigger = d4j(args, src, "export", "-p", "tests.trigger").split()
    row["tests"] = len(tests)
    row["failing_d4j"] = len(trigger)
    jaguar2_cp = open(os.path.join(args.tools, "jaguar2.classpath")).read().strip()
    junit_env = dict(os.environ)

    # --- baseline
    d = os.path.join(out, "plain")
    os.makedirs(d, exist_ok=True)
    # Tests read resources by relative path, so every run's working directory is the checkout.
    _, t = run([java, "-cp", cp, "org.junit.runner.JUnitCore", *tests], cwd=src, env=junit_env,
               timeout=args.timeout, log=os.path.join(d, "run.log"))
    row["time_plain_s"] = round(t, 1)

    # --- Jaguar2 fork
    j2 = os.path.join(out, "jaguar2")
    shutil.rmtree(j2, ignore_errors=True)
    os.makedirs(os.path.join(j2, "target"))
    for f in ("jaguar2.csv", "coverageMatrix.txt"):
        if os.path.exists(os.path.join(src, "target", f)):
            os.remove(os.path.join(src, "target", f))
    code, t = run([java, "-javaagent:%s=destfile=%s" % (os.path.join(args.tools, "jacocoagent.jar"),
                                                        os.path.join(j2, "target", "jacoco.exec")),
                   "-Djaguar2.classesDirs=" + classes,
                   # Jaguar2 first: its unrelocated ASM/JaCoCo otherwise lose to an old
                   # asm.jar on the project's class path (Lang, cglib users) and every
                   # test class fails with IncompatibleClassChangeError.
                   "-cp", jaguar2_cp + os.pathsep + cp,
                   "br.usp.each.saeg.jaguar2.junit.JaguarTestRunner", *tests],
                  cwd=src, env=junit_env, timeout=args.timeout, log=os.path.join(j2, "run.log"))
    row["time_jaguar2_s"] = round(t, 1)
    # Jaguar2 writes target/jaguar2.csv and target/coverageMatrix.txt under its working directory.
    for f in ("jaguar2.csv", "coverageMatrix.txt"):
        if os.path.exists(os.path.join(src, "target", f)):
            shutil.move(os.path.join(src, "target", f), os.path.join(j2, "target", f))
    row["failing_jaguar2"] = len(jaguar2_failing(os.path.join(j2, "run.log")))

    # --- JFLP, both line semantics
    for mode, opt in (("jflp", ""), ("jflp_jc", ",lines=jacoco")):
        d = os.path.join(out, mode)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        _, t1 = run([java, "-javaagent:%s=destdir=%s,classdirs=%s%s" % (args.jflp, os.path.join(d, "data"),
                                                                        classes, opt),
                     "-cp", cp, "org.junit.runner.JUnitCore", *tests],
                    cwd=src, env=junit_env, timeout=args.timeout, log=os.path.join(d, "run.log"))
        _, t2 = run([java, "-jar", args.jflp, "report", "--data", os.path.join(d, "data"), "--classes", classes,
                     "--out", d], cwd=d, log=os.path.join(d, "report.log"))
        row["time_%s_s" % mode] = round(t1 + t2, 1)
        row["failing_%s" % mode] = len(failing(os.path.join(d, "coverageMatrix.tests.csv")))

    # --- Defects4J's own runner (Ant) with the agent
    d = os.path.join(out, "d4j_runner")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    env = dict(os.environ, PATH=os.path.join(args.d4j, "framework", "bin") + os.pathsep + os.environ["PATH"])
    env["JAVA_TOOL_OPTIONS"] = (os.environ.get("JAVA_TOOL_OPTIONS", "") + " -javaagent:%s=destdir=%s,classdirs=%s"
                                % (args.jflp, os.path.join(d, "data"), classes)).strip()
    run(["defects4j", "test"], cwd=src, env=env, timeout=args.timeout, log=os.path.join(d, "run.log"))
    run([java, "-jar", args.jflp, "report", "--data", os.path.join(d, "data"), "--classes", classes, "--out", d],
        cwd=d, log=os.path.join(d, "report.log"))
    row["failing_d4j_runner_jflp"] = len(failing(os.path.join(d, "coverageMatrix.tests.csv")))

    # --- comparisons
    row["jc_csv_identical"] = same_file(os.path.join(j2, "target", "jaguar2.csv"),
                                        os.path.join(out, "jflp_jc", "jaguar2.csv"))
    row["jc_matrix_identical"] = same_file(os.path.join(j2, "target", "coverageMatrix.txt"),
                                           os.path.join(out, "jflp_jc", "coverageMatrix.txt"))
    md = matrix_diff(os.path.join(j2, "target", "coverageMatrix.txt"), os.path.join(out, "jflp", "coverageMatrix.txt"))
    if md:
        row["jflp_matrix_cells_differ"], row["jflp_matrix_0to1"], row["jflp_matrix_1to0"] = md
    universe = class_lines(os.path.join(out, "jflp", "coverageMatrix.columns.csv"))
    row["lines"] = len(universe)
    patch = os.path.join(args.d4j, "framework", "projects", project, "patches", "%s.src.patch" % bug_id)
    targets = map_to_classes(patch_lines(patch, "+"), universe)
    row["buggy_lines"] = len(targets)
    if targets:
        for mode, path in (("jaguar2", os.path.join(j2, "target", "jaguar2.csv")),
                           ("jflp", os.path.join(out, "jflp", "jaguar2.csv"))):
            scores = {k: v[4] for k, v in read_csv(path).items()}
            row["rank_" + mode] = rank(scores, universe, targets)
    else:
        row["note"] = "no buggy line holds code (omission fault outside code lines?)"
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--d4j", required=True)
    ap.add_argument("--tools", required=True)
    ap.add_argument("--jflp", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--java", default=os.path.join(os.environ.get("JAVA_HOME", "/usr"), "bin", "java"))
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("bugs", nargs="+")
    args = ap.parse_args()
    args.jflp = os.path.abspath(args.jflp)
    args.work = os.path.abspath(args.work)
    os.makedirs(args.work, exist_ok=True)
    results = os.path.join(args.work, "results.csv")
    done = set()
    if os.path.exists(results):
        done = {r["bug"] for r in csv.DictReader(open(results)) if r["status"] == "ok"}
    new = not os.path.exists(results)
    with open(results, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for bug in args.bugs:
            if bug in done:
                continue
            try:
                row = one_bug(args, bug, args.java)
            except Exception as e:  # one broken bug must not stop the batch, but it is recorded
                row = {"bug": bug, "status": "error", "note": str(e)[:300].replace("\n", " ")}
            w.writerow(row)
            f.flush()
            print(", ".join("%s=%s" % (k, row.get(k, "")) for k in FIELDS if row.get(k, "") != ""), flush=True)


if __name__ == "__main__":
    sys.exit(main())
