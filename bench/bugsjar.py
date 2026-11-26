#!/usr/bin/env python3
"""Compares JFLP with the Jaguar2 fork on Bugs.jar bugs (Maven projects).

Each bug is a branch of a Bugs.jar project repository. The bug's module is
built once with the build JDK; its tests then run with Surefire four times,
always forked on the test JDK (the fork needs Java 11+):

  plain     no tool, the baseline
  jaguar2   the fork, set up as its Execution.md says: a copy of the module POM
            (jaguar2-pom.xml) with the Jaguar2 dependencies and Surefire listener,
            plus JaCoCo 0.8.7 prepare-agent and -Djaguar2.classesDirs
  jflp      JFLP's agent from JAVA_TOOL_OPTIONS; the project POM is untouched
  jflp-jc   the same with lines=jacoco

Results go to WORK/results.csv; raw outputs to WORK/<bug>/.

Usage:
  bench/bugsjar.py --repo CLONE_OF_A_BUGSJAR_PROJECT --tools BENCH_TOOLS --jflp target/jflp.jar \\
      --build-jdk JDK8_HOME --test-jdk JDK11_HOME --work DIR MATH-1005 MATH-1021 ...
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

from sbfl_common import (class_lines, failing, map_to_classes, matrix_diff, patch_lines, rank, read_csv, run,
                         same_file)

FIELDS = [
    "bug", "status", "module", "failing_bugsjar", "failing_jaguar2", "failing_jflp", "failing_jflp_jc",
    "tests_jflp", "time_plain_s", "time_jaguar2_s", "time_jflp_s", "time_jflp_jc_s", "lines",
    "jc_csv_identical", "jc_matrix_identical", "jflp_matrix_cells_differ", "jflp_matrix_0to1",
    "jflp_matrix_1to0", "buggy_lines", "rank_jaguar2", "rank_jflp", "note",
]

POM_NS = "http://maven.apache.org/POM/4.0.0"
JAGUAR2_ARTIFACTS = ["jaguar2-junit4", "jaguar2-jacoco-provider", "jaguar2-csv-exporter", "jaguar2-core"]
LISTENER = "br.usp.each.saeg.jaguar2.junit.JaguarJUnitRunListener"


def jaguar2_pom(pom, out):
    """The module POM plus what Jaguar2's Execution.md asks for."""
    ET.register_namespace("", POM_NS)
    tree = ET.parse(pom)
    root = tree.getroot()
    ns = "{%s}" % POM_NS if root.tag.startswith("{") else ""

    def child(parent, name):
        c = parent.find(ns + name)
        if c is None:
            c = ET.SubElement(parent, ns + name)
        return c

    deps = child(root, "dependencies")
    for a in JAGUAR2_ARTIFACTS:
        d = ET.SubElement(deps, ns + "dependency")
        for k, v in (("groupId", "br.usp.each.saeg"), ("artifactId", a), ("version", "0.0.3-SNAPSHOT"),
                     ("scope", "test")):
            ET.SubElement(d, ns + k).text = v
    plugins = child(child(root, "build"), "plugins")
    surefire = None
    for p in plugins.findall(ns + "plugin"):
        a = p.find(ns + "artifactId")
        if a is not None and a.text == "maven-surefire-plugin":
            surefire = p
    if surefire is None:
        surefire = ET.SubElement(plugins, ns + "plugin")
        ET.SubElement(surefire, ns + "artifactId").text = "maven-surefire-plugin"
    conf = child(surefire, "configuration")
    prop = ET.SubElement(child(conf, "properties"), ns + "property")
    ET.SubElement(prop, ns + "name").text = "listener"
    ET.SubElement(prop, ns + "value").text = LISTENER
    arg = conf.find(ns + "argLine")
    if arg is not None and "argLine" not in (arg.text or ""):
        # A fixed argLine would drop the JaCoCo agent that prepare-agent puts in ${argLine}.
        arg.text = "${argLine} " + (arg.text or "")
    tree.write(out, xml_declaration=True, encoding="UTF-8")


def bugsjar_failing(results_txt):
    """Failing tests listed in Bugs.jar's recorded Maven output."""
    out = set()
    section = False
    for line in open(results_txt, errors="replace"):
        if re.match(r"^(Failed tests|Tests in error):", line):
            section = True
            continue
        if section:
            m = re.match(r"^\s+(\w+)\(([\w.$]+)\)", line)
            if m:
                out.add("%s(%s)" % (m.group(1), m.group(2)))
            elif not line.strip() or line.startswith("Tests run:"):
                section = False
    return out


def module_of(patch):
    for line in open(patch, errors="replace"):
        if line.startswith("--- a/") and "/src/main/java/" in "/" + line[6:]:
            p = line[6:].strip()
            return p[:p.index("src/main/java/")].rstrip("/")
    return ""


def one_bug(args, issue):
    out = os.path.join(args.work, issue)
    src = os.path.join(out, "checkout")
    row = {"bug": issue, "status": "ok", "note": ""}
    os.makedirs(out, exist_ok=True)
    branches = subprocess.run(["git", "-C", args.repo, "branch", "-r"], capture_output=True, text=True).stdout.split()
    match = [b for b in branches if re.search(r"bugs-dot-jar_%s_" % re.escape(issue), b)]
    if not match:
        raise RuntimeError("no branch for " + issue)
    if not os.path.isdir(src):
        subprocess.run(["git", "-C", args.repo, "worktree", "add", "-f", "--detach", src, match[0]], check=True,
                       capture_output=True)
    patch = os.path.join(src, ".bugs-dot-jar", "developer-patch.diff")
    module = module_of(patch)
    row["module"] = module or "."
    mod_dir = os.path.join(src, module)
    classes = os.path.join(mod_dir, "target", "classes")
    row["failing_bugsjar"] = len(bugsjar_failing(os.path.join(src, ".bugs-dot-jar", "test-results.txt")))

    mvn_env = dict(os.environ, JAVA_HOME=args.build_jdk)
    common = ["-B", "-Drat.skip", "-Dcheckstyle.skip", "-Danimal.sniffer.skip", "-Denforcer.skip"]
    build = ["mvn", *common, "-DskipTests", "install"] + (["-pl", module, "-am"] if module else [])
    code, _ = run(build, cwd=src, env=mvn_env, timeout=args.timeout, log=os.path.join(out, "build.log"))
    if code != 0:
        raise RuntimeError("build failed, see %s" % os.path.join(out, "build.log"))
    test = ["mvn", *common, "surefire:test", "-Djvm=" + os.path.join(args.test_jdk, "bin", "java"),
            "-Dmaven.test.failure.ignore=true", "-DfailIfNoTests=false"]

    _, t = run(test, cwd=mod_dir, env=mvn_env, timeout=args.timeout, log=os.path.join(out, "plain.log"))
    row["time_plain_s"] = round(t, 1)

    # --- Jaguar2 fork
    j2_pom = os.path.join(mod_dir, "jaguar2-pom.xml")
    jaguar2_pom(os.path.join(mod_dir, "pom.xml"), j2_pom)
    for f in ("jaguar2.csv", "coverageMatrix.txt", "jacoco.exec"):
        if os.path.exists(os.path.join(mod_dir, "target", f)):
            os.remove(os.path.join(mod_dir, "target", f))
    _, t = run(["mvn", *common, "-f", j2_pom, "org.jacoco:jacoco-maven-plugin:0.8.7:prepare-agent", *test[len(common) + 1:],
                "-Djaguar2.classesDirs=" + classes],
               cwd=mod_dir, env=mvn_env, timeout=args.timeout, log=os.path.join(out, "jaguar2.log"))
    row["time_jaguar2_s"] = round(t, 1)
    j2 = os.path.join(out, "jaguar2")
    shutil.rmtree(j2, ignore_errors=True)
    os.makedirs(j2)
    for f in ("jaguar2.csv", "coverageMatrix.txt"):
        if os.path.exists(os.path.join(mod_dir, "target", f)):
            shutil.move(os.path.join(mod_dir, "target", f), os.path.join(j2, f))

    # --- JFLP
    for mode, opt in (("jflp", ""), ("jflp_jc", ",lines=jacoco")):
        d = os.path.join(out, mode)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        env = dict(mvn_env)
        env["JAVA_TOOL_OPTIONS"] = (os.environ.get("JAVA_TOOL_OPTIONS", "") + " -javaagent:%s=destdir=%s,classdirs=%s%s"
                                    % (args.jflp, os.path.join(d, "data"), classes, opt)).strip()
        _, t1 = run(test, cwd=mod_dir, env=env, timeout=args.timeout, log=os.path.join(d, "run.log"))
        _, t2 = run([os.path.join(args.test_jdk, "bin", "java"), "-jar", args.jflp, "report", "--data",
                     os.path.join(d, "data"), "--classes", classes, "--out", d], cwd=d,
                    log=os.path.join(d, "report.log"))
        row["time_%s_s" % mode] = round(t1 + t2, 1)
        row["failing_%s" % mode] = len(failing(os.path.join(d, "coverageMatrix.tests.csv")))
    tests_csv = os.path.join(out, "jflp", "coverageMatrix.tests.csv")
    row["tests_jflp"] = max(0, len(open(tests_csv).readlines()) - 1) if os.path.exists(tests_csv) else 0
    j2_matrix = os.path.join(j2, "coverageMatrix.txt")
    if os.path.exists(j2_matrix):
        row["failing_jaguar2"] = sum(1 for l in open(j2_matrix) if l.rstrip().endswith("-"))

    # --- comparisons
    row["jc_csv_identical"] = same_file(os.path.join(j2, "jaguar2.csv"), os.path.join(out, "jflp_jc", "jaguar2.csv"))
    row["jc_matrix_identical"] = same_file(j2_matrix, os.path.join(out, "jflp_jc", "coverageMatrix.txt"))
    md = matrix_diff(j2_matrix, os.path.join(out, "jflp", "coverageMatrix.txt"))
    if md:
        row["jflp_matrix_cells_differ"], row["jflp_matrix_0to1"], row["jflp_matrix_1to0"] = md
    universe = class_lines(os.path.join(out, "jflp", "coverageMatrix.columns.csv"))
    row["lines"] = len(universe)
    targets = map_to_classes(patch_lines(patch, "-"), universe)
    row["buggy_lines"] = len(targets)
    if targets:
        for mode, path in (("jaguar2", os.path.join(j2, "jaguar2.csv")), ("jflp", os.path.join(out, "jflp", "jaguar2.csv"))):
            if os.path.exists(path):
                row["rank_" + mode] = rank({k: v[4] for k, v in read_csv(path).items()}, universe, targets)
    else:
        row["note"] = "no buggy line holds code"
    return row


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
                row = one_bug(args, bug)
            except Exception as e:  # one broken bug must not stop the batch, but it is recorded
                row = {"bug": bug, "status": "error", "note": str(e)[:300].replace("\n", " ")}
            w.writerow(row)
            f.flush()
            print(", ".join("%s=%s" % (k, row.get(k, "")) for k in FIELDS if row.get(k, "") != ""), flush=True)


if __name__ == "__main__":
    sys.exit(main())
