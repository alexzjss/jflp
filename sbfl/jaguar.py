"""Execução do JaguarRunner com timeout e laço de recuperação de crashes."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .proc import run, make_env, java_exe

TEST_RE = re.compile(r"Test (\S+?)\(([\w.$]+)\) : (Passed|Failed)")
OUT_RE = re.compile(r"Output xml created at: (.+\.xml)\s*$")
NOISE = r"to classFilesCache|Time to (analyze|receive|read|collect)|#lines = \d+"


def write_test_list(path: Path, classes: list[str]) -> None:
    # UTF-8 SEM BOM e sem CRLF: `Out-File -Encoding utf8` do PowerShell põe BOM e
    # isso pode corromper o primeiro nome de classe.
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(classes) + "\n")


def build_cmd(cfg, ctx, tf: Path, out_base: str, heuristic: str) -> list:
    lib = cfg.path("paths", "jaguar_lib")
    sep = os.pathsep
    cp = sep.join([str(lib / "*"), ctx.classes_dir, ctx.tests_dir, *ctx.extra_cp])
    return [
        java_exe(cfg, ctx.java_home),
        f"-javaagent:{lib / 'jacocoagent.jar'}=output=tcpserver,port=6300",
        "-cp", cp,
        "br.usp.each.saeg.jaguar.core.cli.JaguarRunner",
        "-p", ".", "-c", ctx.classes_dir, "-t", ctx.tests_dir,
        "-tf", str(tf), "-h", heuristic,
        "-ot", cfg.get("jaguar", "output_type"), "-o", out_base,
        "-l", cfg.get("jaguar", "log_level"),
    ]


def _find_xml(project_dir: Path, tail: list[str], out_base: str) -> Path | None:
    for line in reversed(tail):
        m = OUT_RE.search(line.strip())
        if m:
            p = Path(m.group(1).strip())
            p = p if p.is_absolute() else project_dir / p
            if p.exists():
                return p
    p = project_dir / ".jaguar" / f"{out_base}.xml"
    return p if p.exists() else None


def run_jaguar(cfg, ctx, out_dir: Path, heuristic: str | None = None) -> dict:
    heuristic = heuristic or cfg.get("jaguar", "heuristic")
    out_base = f"sbfl_{heuristic}"
    log = out_dir / "jaguar.log"
    tf = ctx.project_dir / ".sbfl_tests.txt"
    classes = list(ctx.test_classes)
    excluded: list[str] = []
    last_seen = None
    meta = {"attempts": 0, "excluded_classes": excluded, "analysis_exceptions": 0}
    stale = ctx.project_dir / ".jaguar" / f"{out_base}.xml"
    for attempt in range(cfg.get("jaguar", "max_retries") + 1):
        meta["attempts"] = attempt + 1
        if stale.exists():
            stale.unlink()
        remaining = [c for c in classes if c not in excluded]
        if not remaining:
            meta["status"] = "no_tests"
            return meta
        write_test_list(tf, remaining)
        r = run(build_cmd(cfg, ctx, tf, out_base, heuristic), cwd=ctx.project_dir,
                env=make_env(cfg, ctx.java_home), timeout=cfg.get("jaguar", "timeout_s"), log_path=log,
                drop=NOISE,
                count={"analysis_exceptions": r"Exception during analysis"},
                collect={"test": r"(Test \S+?\([\w.$]+\) : (?:Passed|Failed))"})
        meta["analysis_exceptions"] += r["counts"]["analysis_exceptions"]
        meta["seconds"] = meta.get("seconds", 0) + r["seconds"]
        events = [TEST_RE.search(t) for t in r["collected"]["test"]]
        events = [(m.group(1), m.group(2), m.group(3)) for m in events if m]
        failed = [f"{c}#{n}" for n, c, s in events if s == "Failed"]
        meta["failed_tests"] = failed[:200]
        xml = _find_xml(ctx.project_dir, r["tail"], out_base)
        if xml:
            # classes que não são testes de verdade (abstract/sem @Test) viram
            # `initializationError` "falho" e poluem o espectro: remove e repete.
            bad = sorted({c for n, c, s in events if n == "initializationError" and c in remaining})
            if bad and attempt < cfg.get("jaguar", "max_retries"):
                excluded += bad
                continue
            shutil.copy(xml, out_dir / f"jaguar_{heuristic}.xml")
            meta["status"] = "ok"
            return meta
        if r["timed_out"]:
            meta["status"] = "jaguar_timeout"
            return meta
        # crash: descobre o culpado pelo último teste que terminou
        if not events:
            culprit, last = remaining[0], None
        else:
            last = events[-1][1]
            base = last if last in remaining else last.split("$", 1)[0]
            if base not in remaining:
                meta["status"] = "jaguar_crash"
                meta["tail"] = "".join(r["tail"][-30:])
                return meta
            i = remaining.index(base)
            nxt = remaining[i + 1] if i + 1 < len(remaining) else None
            culprit = base if (base == last_seen or nxt is None) else nxt
            last = base
        last_seen = last
        excluded.append(culprit)
    meta["status"] = "jaguar_crash"
    meta["tail"] = "".join(r["tail"][-30:])
    return meta
