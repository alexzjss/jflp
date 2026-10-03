from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config as config_mod
from .adapters import get_adapters
from .pipeline import process, analyze_dir, java_version
from .proc import make_env


def _bugs(cfg, args):
    names = None if args.benchmark == "all" else [args.benchmark]
    out = []
    for a in get_adapters(cfg, names):
        for b in a.list_bugs(args.project, args.bug):
            out.append((a, b))
    return out[: args.limit] if args.limit else out


def cmd_doctor(cfg, args):
    ok = True

    def chk(name, cond, hint=""):
        nonlocal ok
        print(f"[{'OK' if cond else 'XX'}] {name}" + ("" if cond else f"  -> {hint}"))
        ok &= bool(cond)

    lib = cfg.path("paths", "jaguar_lib")
    chk("jaguar_lib/jacocoagent.jar", (lib / "jacocoagent.jar").exists(), f"não achei em {lib}")
    chk("br.usp.each.saeg.jaguar.core.jar", (lib / "br.usp.each.saeg.jaguar.core.jar").exists())
    chk("git", shutil.which("git"))
    chk("mvn", shutil.which(cfg.get("java", "mvn")) or shutil.which("mvn.cmd"), "instale Maven")
    print("java:", java_version(cfg), "(Jaguar/JaCoCo antigos costumam exigir Java 8)")
    d4j = cfg.get("defects4j", "home")
    if d4j:
        chk("defects4j", (Path(d4j).expanduser() / "framework/bin/defects4j").exists())
    for r in cfg.get("bugsjar", "repos"):
        chk(f"repo bugs.jar {r}", (Path(r).expanduser() / ".git").exists())
    return 0 if ok else 1


def cmd_list(cfg, args):
    for _, b in _bugs(cfg, args):
        print(b.key)
    return 0


def cmd_run(cfg, args):
    todo = _bugs(cfg, args)
    print(f"{len(todo)} bug(s) selecionados")
    if args.workers > 1:
        print("AVISO: o agente JaCoCo usa a porta fixa 6300; execuções paralelas na MESMA "
              "máquina tendem a colidir. Use containers/VMs separados ou workers=1.")

    def one(ab):
        a, b = ab
        m = process(cfg, a, b, force=args.force, keep_workdir=args.keep)
        print(f"{b.key:70s} {m.get('status'):18s} {m.get('total_seconds', '')}s", flush=True)
        return m

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(one, todo))
    return 0


def cmd_analyze(cfg, args):
    n = 0
    for mp in cfg.results.glob("*/*/*/meta.json"):
        d = mp.parent
        if list(d.glob("jaguar_*.xml")) and (d / "truth.json").exists():
            print(f"{d.relative_to(cfg.results)}: {analyze_dir(cfg, d)}")
            n += 1
    print(f"{n} relatório(s) reavaliados")
    return 0


def cmd_aggregate(cfg, args):
    from .aggregate import aggregate
    for p in aggregate(cfg.results, cfg.results / "_agregado"):
        print("gerado:", p)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sbfl")
    ap.add_argument("-c", "--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("doctor", cmd_doctor), ("list", cmd_list), ("run", cmd_run),
                     ("analyze", cmd_analyze), ("aggregate", cmd_aggregate)]:
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        if name in ("list", "run"):
            p.add_argument("--benchmark", default="all", choices=["all", "d4j", "bugsjar"])
            p.add_argument("--project")
            p.add_argument("--bug")
            p.add_argument("--limit", type=int)
        if name == "run":
            p.add_argument("--force", action="store_true", help="refaz bugs já processados")
            p.add_argument("--keep", action="store_true", help="mantém o checkout em work/")
            p.add_argument("--workers", type=int, default=1)
    args = ap.parse_args(argv)
    cfg = config_mod.load(args.config)
    return args.fn(cfg, args)
