from __future__ import annotations

import argparse
import random
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from . import config as config_mod
from .errors import StageError
from .build import resolve_tool
from .envcheck import advice
from .proc import java_exe
from .adapters import get_adapters, NAMES
from .proc import make_env


def _bugs(cfg, args):
    names = None if args.benchmark == "all" else [args.benchmark]
    out = []
    for a in get_adapters(cfg, names):
        try:
            found = a.list_bugs(args.project, args.bug)
        except Exception as e:   # um benchmark não configurado não derruba os outros
            if args.benchmark != "all":
                raise
            print(f"[{a.name}] indisponível, ignorado ({type(e).__name__})", file=sys.stderr)
            continue
        out += [(a, b) for b in found]
    n = getattr(args, "sample_per_project", None)
    if n:
        rng, by = random.Random(args.seed), {}
        for ab in out:
            by.setdefault((ab[1].benchmark, ab[1].project), []).append(ab)
        out = [ab for k in sorted(by) for ab in sorted(rng.sample(by[k], min(n, len(by[k]))),
                                                      key=lambda x: x[1].bug_id)]
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
    def out(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=make_env(cfg))
            return (r.stdout or "") + (r.stderr or "")
        except Exception:
            return ""

    java_txt = out([java_exe(cfg), "-version"])
    mvn_txt = out([resolve_tool(cfg.get("java", "mvn")), "-v"])
    help_txt = out([java_exe(cfg), "-cp", str(lib / "*"),
                    "br.usp.each.saeg.jaguar.core.cli.JaguarRunner", "--help"]) if lib.exists() else ""
    cfg.work.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(cfg.work).free / 2**30
    for level, msg in advice(java_txt, mvn_txt, help_txt, str(cfg.work), free):
        print(f"[{ {'ok': 'OK', 'warn': '!!', 'fail': 'XX'}[level] }] {msg}")
        ok &= level != "fail"
    d4j = cfg.get("defects4j", "home")
    if d4j:
        chk("defects4j", (Path(d4j).expanduser() / "framework/bin/defects4j").exists())
    from .adapters.bugsjar import configured_repos
    bj = configured_repos(cfg)
    if cfg.get("bugsjar", "repos") or cfg.get("bugsjar", "root"):
        chk("repositórios do Bugs.jar encontrados", bj, "rode `python -m sbfl fetch` ou ajuste [bugsjar] root/repos")
        for r in bj:
            print(f"      - {r.name}")
    return 0 if ok else 1


def cmd_list(cfg, args):
    bugs = _bugs(cfg, args)
    if args.summary:
        for (bench, proj), n in sorted(Counter((b.benchmark, b.project) for _, b in bugs).items()):
            print(f"{bench}/{proj}: {n}")
        print(f"total: {len(bugs)}")
    else:
        for _, b in bugs:
            print(b.key)
    return 0


def cmd_triage(cfg, args):
    from . import triage
    rep = triage.build_report(cfg.results, None if args.benchmark == "all" else args.benchmark, args.project)
    md = triage.render_markdown(rep)
    print(md)
    if args.md:
        Path(args.md).write_text(md, encoding="utf-8")
        print(f"salvo em {args.md}")
    return 0


def _known_repos(cfg) -> list[Path]:
    from .adapters.bugsjar import configured_repos
    repos = list(configured_repos(cfg))
    return repos


def _size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0


def cmd_clean(cfg, args):
    """Remove sobras de execuções interrompidas (checkouts em work/) e poda worktrees órfãos."""
    removed, freed = 0, 0
    if cfg.work.exists():
        for child in sorted(cfg.work.iterdir()):
            if child.name in ("_clones", "_selftest") and not args.all:
                continue
            freed += _size(child)
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    for r in _known_repos(cfg):
        subprocess.run(["git", "-C", str(r), "worktree", "prune"], capture_output=True)
    print(f"{removed} item(ns) removido(s) de {cfg.work}, {freed / 2**20:.0f} MB liberados; worktrees podados.")
    return 0


def cmd_compare(cfg, args):
    from .compare import run_compare
    args.limit = None
    found = _bugs(cfg, args)
    if not found:
        print(f"nenhum bug '{args.bug}' encontrado", file=sys.stderr)
        return 1
    if len(found) > 1:
        print("mais de um bug casa com esse id; use --benchmark e --project:\n  "
              + "\n  ".join(b.key for _, b in found), file=sys.stderr)
        return 1
    a, b = found[0]
    print(run_compare(cfg, a, b))
    return 0


def cmd_fetch(cfg, args):
    from .adapters.bugsjar import fetch_repos
    for proj, action in fetch_repos(cfg, args.project):
        print(f"{proj}: {action}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jflp")
    ap.add_argument("-c", "--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("doctor", cmd_doctor), ("list", cmd_list), ("fetch", cmd_fetch),
                     ("compare", cmd_compare), ("clean", cmd_clean), ("triage", cmd_triage)]:
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        if name in ("list", "triage", "compare"):
            p.add_argument("--benchmark", default="all", choices=["all", *NAMES])
            p.add_argument("--project")
        if name == "list":
            p.add_argument("--bug")
            p.add_argument("--limit", type=int)
            p.add_argument("--sample-per-project", type=int, metavar="N",
                           help="N bugs aleatórios por projeto")
            p.add_argument("--seed", type=int, default=0)
            p.add_argument("--summary", action="store_true", help="só a contagem por projeto")
        if name == "triage":
            p.add_argument("--md", metavar="ARQUIVO", help="salva a tabela em Markdown")
        if name == "compare":
            p.add_argument("bug", help="id do bug")
        if name == "fetch":
            p.add_argument("--project")
        if name == "clean":
            p.add_argument("--all", action="store_true", help="remove também work/_clones e work/_selftest")
    args = ap.parse_args(argv)
    cfg = config_mod.load(args.config)
    try:
        return args.fn(cfg, args)
    except StageError as e:
        print(f"erro ({e.status}): {e.detail}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"erro: ferramenta ou arquivo não encontrado: {e.filename or e}", file=sys.stderr)
        return 2
