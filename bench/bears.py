#!/usr/bin/env python3
"""Compares JFLP with the Jaguar2 fork on Bears bugs (Maven projects, 2017-2019).

Each Bears bug is a branch of bears-bugs/bears-benchmark; its buggy version is
the commit "Changes in the tests" (buggy code plus the tests that expose it), and
bears.json on the branch holds the fix (patchDiff, buggy -> fixed), the failing
tests and the failing module. The bug's module is then measured exactly as in
bench/bugsjar.py (plain, Jaguar2, JFLP, JFLP lines=jacoco).

Usage:
  bench/bears.py --repo CLONE_OF_BEARS_BENCHMARK --tools BENCH_TOOLS --jflp target/jflp.jar \\
      --build-jdk JDK8_HOME --test-jdk JDK11_HOME --work DIR Bears-1 Bears-23 ...
"""

import argparse
import csv
import json
import os
import subprocess
import sys

import bugsjar

FIELDS = [f if f != "failing_bugsjar" else "failing_bears" for f in bugsjar.FIELDS]


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout


def one_bug(args, bug_id, branch):
    out = os.path.join(args.work, bug_id)
    src = os.path.join(out, "checkout")
    row = {"bug": bug_id, "status": "ok", "note": ""}
    os.makedirs(out, exist_ok=True)
    meta_path = os.path.join(out, "bears.json")
    if not os.path.isdir(src):
        git(args.repo, "fetch", "-q", "--depth", "6", "origin", branch)
        meta = git(args.repo, "show", "FETCH_HEAD:bears.json")
        open(meta_path, "w").write(meta)
        log = git(args.repo, "log", "--format=%H %s", "FETCH_HEAD").splitlines()
        buggy = [l.split()[0] for l in log if " Changes in the tests" in l]
        fixed = [l.split()[0] for l in log if " Human patch" in l]
        if not buggy or not fixed:
            raise RuntimeError("no 'Changes in the tests'/'Human patch' commits on " + branch)
        # bears.json's patchDiff only holds counts; the fix is the diff between the two commits.
        open(os.path.join(out, "patch.diff"), "w").write(git(args.repo, "diff", buggy[0], fixed[0], "--", "*.java"))
        git(args.repo, "worktree", "add", "-f", "--detach", src, buggy[0])
    meta = json.load(open(meta_path))
    patch = os.path.join(out, "patch.diff")
    # failingModule is /root/workspace/<owner>/<repo>/<build id>[/<module>]
    parts = meta["tests"]["failingModule"].strip("/").split("/")
    module = "/".join(parts[5:])
    row["failing_bears"] = len(meta["tests"].get("failureDetails", []))
    return bugsjar.measure(args, row, out, src, module, patch)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tools", required=True)
    ap.add_argument("--jflp", required=True)
    ap.add_argument("--build-jdk", required=True)
    ap.add_argument("--test-jdk", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--timeout", type=int, default=5400)
    ap.add_argument("bugs", nargs="+")
    args = ap.parse_args()
    args.jflp = os.path.abspath(args.jflp)
    args.work = os.path.abspath(args.work)
    args.repo = os.path.abspath(args.repo)
    branches = {b["bugId"]: b["bugBranch"]
                for b in json.load(open(os.path.join(args.repo, "scripts", "data", "bug_id_and_branch.json")))}
    os.makedirs(args.work, exist_ok=True)
    results = os.path.join(args.work, "results.csv")
    new = not os.path.exists(results)
    with open(results, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for bug in args.bugs:
            try:
                row = one_bug(args, bug, branches[bug])
            except Exception as e:  # one broken bug must not stop the batch, but it is recorded
                row = {"bug": bug, "status": "error", "note": str(e)[:300].replace("\n", " ")}
            w.writerow(row)
            f.flush()
            print(", ".join("%s=%s" % (k, row.get(k, "")) for k in FIELDS if row.get(k, "") != ""), flush=True)


if __name__ == "__main__":
    sys.exit(main())
