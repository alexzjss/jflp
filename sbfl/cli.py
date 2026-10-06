from __future__ import annotations

import argparse
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config as config_mod
from .errors import StageError
from .build import resolve_tool
from .envcheck import advice
from .power import keep_awake
from .proc import java_exe
from .adapters import get_adapters, NAMES
from .pipeline import process, analyze_dir, java_version, needs_run
from . import campaign
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
    b = cfg.get("bears", "repo")
    if b:
        chk(f"repo bears {b}", (Path(b).expanduser() / ".git").exists())
    for f in cfg.get("manifest", "files"):
        chk(f"manifest {f}", (cfg.base / f).exists() or Path(f).expanduser().exists())
    if cfg.get("gitbugjava", "projects") or shutil.which(cfg.get("gitbugjava", "bin")):
        chk("gitbug-java", shutil.which(cfg.get("gitbugjava", "bin")))
    print("\nPróximo passo: `python -m sbfl selftest` valida a cadeia completa (Git, Maven, Jaguar) num projeto de exemplo.")
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


def cmd_run(cfg, args):
    todo = _bugs(cfg, args)
    retry = {x for x in (args.retry or "").split(",") if x}
    pending = [ab for ab in todo if needs_run(cfg, ab[1], args.force, retry)]
    print(f"{len(todo)} bug(s) selecionados; {len(todo) - len(pending)} já processados (pulados); "
          f"{len(pending)} a executar")
    if args.workers > 1:
        print("AVISO: o agente JaCoCo usa a porta fixa 6300; execuções paralelas na MESMA "
              "máquina tendem a colidir. Use containers/VMs separados ou workers=1.")
    if pending:
        from .envinfo import write as write_env
        print("ambiente registrado em", write_env(cfg))
    deadline = time.time() + args.max_hours * 3600 if args.max_hours else None
    lock = threading.Lock()
    state = {"n": 0, "secs": 0.0, "stop": None, "streak_status": None, "streak": 0}
    seen = Counter()

    def one(ab):
        if state["stop"]:
            return None
        if deadline and time.time() > deadline:
            state["stop"] = f"limite de {args.max_hours}h atingido"
            return None
        a, b = ab
        m = process(cfg, a, b, force=args.force, keep_workdir=args.keep, retry=retry)
        with lock:
            st = m.get("status")
            state["n"] += 1
            state["secs"] += m.get("total_seconds") or 0
            seen[st] += 1
            if campaign.level(m) == 0 and st == state["streak_status"]:
                state["streak"] += 1
            else:
                state["streak_status"], state["streak"] = (st, 1) if campaign.level(m) == 0 else (None, 0)
            if args.abort_after and state["streak"] >= args.abort_after:
                state["stop"] = f"{state['streak']} falhas seguidas com status {st} (problema sistemático?)"
            left = len(pending) - state["n"]
            eta = campaign.fmt_dur(state["secs"] / state["n"] * left / max(args.workers, 1))
            print(f"[{state['n']}/{len(pending)}] {b.key:62s} {st:18s} "
                  f"{m.get('total_seconds', '')}s | ETA {eta}", flush=True)
        return m

    rc = 0
    from contextlib import nullcontext
    try:
        with (nullcontext() if args.no_keep_awake else keep_awake()):
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                list(ex.map(one, pending))
    except KeyboardInterrupt:
        state["stop"], rc = "interrompido (Ctrl-C)", 130
        print("\nInterrompido. Bugs concluídos foram salvos; rode o mesmo comando para continuar.")
    if state["stop"] and rc == 0:
        print(f"\nExecução encerrada: {state['stop']}. Rode o mesmo comando para continuar.")
        rc = 0 if "limite" in state["stop"] else 3
    summary = (f"sbfl run: {state['n']}/{len(pending)} bugs executados"
               + (f"; parada: {state['stop']}" if state["stop"] else "; campanha concluída")
               + "; status: " + ", ".join(f"{k}={v}" for k, v in sorted(seen.items())))
    print(summary)
    if args.notify_cmd:
        try:
            subprocess.run(args.notify_cmd, shell=True, env={**os.environ, "SBFL_SUMMARY": summary}, timeout=60)
        except Exception as e:
            print(f"notify-cmd falhou: {e}", file=sys.stderr)
    return rc


def cmd_selftest(cfg, args):
    from .selftest import run
    return run(cfg)


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
    import json as _json
    from .adapters.bugsjar import configured_repos
    repos = list(configured_repos(cfg))
    if cfg.get("bears", "repo"):
        repos.append(Path(cfg.get("bears", "repo")).expanduser())
    for f in cfg.get("manifest", "files"):
        fp = Path(f).expanduser()
        fp = fp if fp.is_absolute() else cfg.base / fp
        if fp.exists():
            for line in fp.read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    r = Path(_json.loads(line).get("repo", "")).expanduser()
                    if (r / ".git").exists():
                        repos.append(r)
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


def cmd_fetch(cfg, args):
    from .adapters.bugsjar import fetch_repos
    for proj, action in fetch_repos(cfg, args.project):
        print(f"{proj}: {action}")
    return 0


def cmd_status(cfg, args):
    totals = {}
    names = None if args.benchmark == "all" else [args.benchmark]
    for a in get_adapters(cfg, names):
        try:
            for b in a.list_bugs(args.project):
                totals[(b.benchmark, b.project)] = totals.get((b.benchmark, b.project), 0) + 1
        except Exception:
            pass
    rows = [r for r in campaign.load_rows(cfg.results)
            if (args.benchmark == "all" or r["benchmark"] == args.benchmark)
            and (not args.project or r["project"] == args.project)]
    md = campaign.render_markdown(campaign.summarize(rows, totals))
    print(md)
    if args.md:
        Path(args.md).write_text(md, encoding="utf-8")
        print(f"salvo em {args.md}")
    return 0


def cmd_inspect(cfg, args):
    from .inspect import find_dir, render
    d = find_dir(cfg.results, args.bug)
    if d is None:
        print(f"nenhum resultado para '{args.bug}' em {cfg.results}", file=sys.stderr)
        return 1
    print(render(d, args.top))
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
                     ("fetch", cmd_fetch), ("clean", cmd_clean), ("status", cmd_status), ("inspect", cmd_inspect),
                     ("selftest", cmd_selftest), ("triage", cmd_triage),
                     ("analyze", cmd_analyze), ("aggregate", cmd_aggregate)]:
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        if name in ("list", "run", "status", "triage"):
            p.add_argument("--benchmark", default="all", choices=["all", *NAMES])
            p.add_argument("--project")
        if name in ("list", "run"):
            p.add_argument("--bug")
            p.add_argument("--limit", type=int)
            p.add_argument("--sample-per-project", type=int, metavar="N",
                           help="N bugs aleatórios por projeto (estimar taxa de sucesso e tempo)")
            p.add_argument("--seed", type=int, default=0)
        if name == "list":
            p.add_argument("--summary", action="store_true", help="só a contagem por projeto")
        if name in ("status", "triage"):
            p.add_argument("--md", metavar="ARQUIVO", help="salva a tabela em Markdown")
        if name == "fetch":
            p.add_argument("--project")
        if name == "clean":
            p.add_argument("--all", action="store_true", help="remove também work/_clones e work/_selftest")
        if name == "inspect":
            p.add_argument("bug", help="id do bug ou benchmark/projeto/bug")
            p.add_argument("--top", type=int, default=10)
        if name == "run":
            p.add_argument("--force", action="store_true", help="refaz bugs já processados")
            p.add_argument("--keep", action="store_true", help="mantém o checkout em work/")
            p.add_argument("--workers", type=int, default=1)
            p.add_argument("--no-keep-awake", action="store_true",
                           help="não impede o computador de dormir durante a execução")
            p.add_argument("--retry", metavar="STATUS[,STATUS]",
                           help="refaz só os bugs com esses status (ex.: build_failed,jaguar_crash)")
            p.add_argument("--max-hours", type=float, metavar="H",
                           help="não inicia novos bugs depois de H horas (termina o atual)")
            p.add_argument("--abort-after", type=int, metavar="N",
                           help="para se N bugs seguidos falharem com o mesmo status (problema sistemático)")
            p.add_argument("--notify-cmd", metavar="CMD",
                           help="comando executado ao final; o resumo vai na variável SBFL_SUMMARY")
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
