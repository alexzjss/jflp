"""CLI `jflp`: doctor, ferramentas, bugs, execuções, lotes, exportação e interface web."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import status as st
from .adapters.base import Bug
from .compare import compare_runs
from .dataset import export
from .errors import StageError
from .execute import RunOptions, execute
from .home import Home, Settings, load_settings
from .prepare import bug_from_key, list_bugs, local_bug, prepare
from .queue import Worker, active_batch_ids, batch_status, create_batch
from .store import list_runs, load_run, reindex, run_dir
from .toolchain.detect import find_jdks, find_mavens, wsl_status
from .toolchain.download import DownloadError, fetch, plan_jaguar1, plan_jdk, plan_maven
from .toolchain.profiles import PROFILES, MissingTool, resolve

KINDS = ("maven", "jaguar2", "jaguar1")


def parse_bug(settings: Settings, spec: str) -> Bug:
    """`local:<pasta>`, `local:<url>@<commit>` ou uma chave `benchmark/projeto/id`."""
    if spec.startswith("local:"):
        return local_bug(spec[len("local:"):])
    return bug_from_key(settings, spec)


def _mb(n) -> str:
    return f"{n / 2**20:.0f} MB" if n else "tamanho desconhecido"


def _ver(v) -> str:
    return ".".join(str(n) for n in v)


# ------------------------------------------------------------------ ambiente

def cmd_doctor(home: Home, settings: Settings, args) -> int:
    print(f"Pasta do JFLP: {home.root}")
    jdks, mavens = find_jdks(home, settings), find_mavens(home, settings)
    print("JDKs:" if jdks else "[!!] Nenhum JDK encontrado.")
    for j in jdks:
        print(f"  [OK] JDK {j.major} ({j.version}) em {j.home} [{j.source}]")
    print("Mavens:" if mavens else "[!!] Nenhum Maven encontrado.")
    for m in mavens:
        print(f"  [OK] Maven {_ver(m.version)} em {m.home} [{m.source}]")
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=20)
        print(f"[OK] {r.stdout.strip()}" if r.returncode == 0 else "[XX] git não funcionou.")
    except (OSError, subprocess.SubprocessError):
        print("[XX] git não encontrado no PATH.")
    w = wsl_status(settings)
    print(f"[{'OK' if w.installed and w.d4j else '!!'}] WSL/Defects4J: {w.message}")
    missing = False
    for profile in PROFILES:
        try:
            te = resolve(profile, home, settings)
            print(f"[OK] perfil {profile}: JDK {te.jdk.major} ({te.jdk.home}), "
                  f"Maven {_ver(te.maven.version)} ({te.maven.home})")
        except MissingTool as e:
            missing = True
            print(f"[XX] perfil {profile}: {e.message}")
    return 1 if missing else 0


def _jaguar1_lib(home: Home, settings: Settings) -> Path:
    return Path(settings.get("jaguar1", "lib") or (home.tools / "jaguar1" / "lib"))


def cmd_tools(home: Home, settings: Settings, args) -> int:
    if args.action == "list":
        for j in find_jdks(home, settings):
            print(f"jdk {j.major}  {j.version}  {j.home}  [{j.source}]")
        for m in find_mavens(home, settings):
            print(f"maven {_ver(m.version)}  {m.home}  [{m.source}]")
        lib = _jaguar1_lib(home, settings)
        print(f"jaguar1  {lib}  [{'instalado' if (lib / 'jacocoagent.jar').is_file() else 'ausente'}]")
        return 0
    try:
        if args.what == "jdk":
            plans = [plan_jdk(home, int(args.value))]
        elif args.what == "maven":
            plans = [plan_maven(home, args.value)]
        else:
            plans = plan_jaguar1(home)
    except (DownloadError, ValueError) as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    for d in plans:
        print(f"{d.name}\n  {d.url}\n  {_mb(d.size)} -> {d.dest}")
    if not args.yes and input("Baixar? [s/N] ").strip().lower() not in ("s", "sim", "y"):
        print("Cancelado.")
        return 1
    for d in plans:
        last = [0.0]

        def progress(done, total, d=d):
            if time.monotonic() - last[0] > 1:
                last[0] = time.monotonic()
                print(f"\r  {d.name}: {_mb(done)}" + (f" de {_mb(total)}" if total else ""),
                      end="", flush=True)

        try:
            dest = fetch(d, progress)
        except DownloadError as e:
            print(f"\nerro: {e}", file=sys.stderr)
            return 1
        print(f"\n[OK] {d.name} instalado em {dest}")
    return 0


def cmd_fetch(home: Home, settings: Settings, args) -> int:
    """Clona (ou atualiza) cada projeto do Bugs.jar configurado."""
    root, projects = settings.get("bugsjar", "root"), settings.get("bugsjar", "projects")
    if not root or not projects:
        print("erro: defina bugsjar.root e bugsjar.projects no config.toml antes de usar `fetch`.",
              file=sys.stderr)
        return 1
    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    template = settings.get("bugsjar", "url_template")
    for proj in projects:
        if args.projeto and proj != args.projeto:
            continue
        dest = root / proj
        if (dest / ".git").exists():
            cmd, action = ["git", "-C", str(dest), "fetch", "--all", "--prune", "-q"], "atualizado"
        else:
            cmd = ["git", "-c", "core.longpaths=true", "clone", "-q",
                   template.format(project=proj), str(dest)]
            action = "clonado"
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print(f"erro: {proj}: {r.stderr.strip()[:300]}", file=sys.stderr)
            return 1
        print(f"{proj}: {action}")
    return 0


# --------------------------------------------------------------------- bugs

def cmd_bugs(home: Home, settings: Settings, args) -> int:
    bugs = list_bugs(settings, args.benchmark, args.projeto)
    for b in bugs:
        print(b.key)
    print(f"total: {len(bugs)}")
    return 0


def cmd_prepare(home: Home, settings: Settings, args) -> int:
    bug = parse_bug(settings, args.bug)
    log = home.work / "prepare.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    prep = prepare(home, settings, bug, log)
    print(f"[OK] {bug.key} preparado em {prep.pristine}")
    print(f"     módulo: {prep.module}; linhas defeituosas conhecidas: {len(prep.truth)}")
    return 0


def _print_log(line: str) -> None:
    if str(line).startswith("# jflp"):
        print(line)


def _opts(args) -> RunOptions:
    return RunOptions(
        test_scope="all" if args.all_tests else None,
        fixes_on=frozenset(args.fix) or None, fixes_off=frozenset(args.no_fix),
        jdk=args.jdk, maven=args.maven, auto_search=not args.no_search,
        archive_checkout=False if args.no_archive else None, log_cb=_print_log)


def cmd_run(home: Home, settings: Settings, args) -> int:
    bug = parse_bug(settings, args.bug)
    rel = execute(home, settings, bug, args.kind, _opts(args))
    run = load_run(home, rel)
    status = run.get("status", "desconhecido")
    print(f"{'[OK]' if st.is_success(status) else '[XX]'} {bug.key} ({args.kind}): {status}")
    print(f"     execução: {rel}")
    cause = (run.get("diagnosis") or {}).get("cause")
    if cause and not st.is_success(status):
        print(f"     causa: {cause}")
    for w in run.get("warnings") or []:
        print(f"     aviso: {w}")
    return 0 if st.is_success(status) else 1


def cmd_runs(home: Home, settings: Settings, args) -> int:
    if args.bug:
        bug = parse_bug(settings, args.bug)
        rows = list_runs(home, bug.benchmark, bug.project, bug.bug_id)
    else:
        rows = list_runs(home)
    for r in rows:
        print(f"{r['path']}  {r['kind']}  {r['status']}  {r.get('started') or ''}")
    print(f"total: {len(rows)}")
    return 0


def cmd_compare(home: Home, settings: Settings, args) -> int:
    res = compare_runs(home, args.rel_a, args.rel_b)
    print("Testes:", res["tests"])
    if res["spectra"] is None:
        print("Espectros: indisponíveis (uma das execuções não tem relatório do Jaguar).")
    else:
        print("Espectros:", res["spectra"])
    print("Defeito em A:", res["faults_a"])
    print("Defeito em B:", res["faults_b"])
    return 0


# -------------------------------------------------------------------- lotes

def _wait_batches(home: Home, seen: set[int]) -> None:
    """Acompanha a fila até não restar job pendente ou em andamento."""
    last = None
    while True:
        ids = active_batch_ids(home)
        seen.update(ids)
        if not ids:
            return
        parts = []
        for i in ids:
            c = batch_status(home, i)["counts"]
            parts.append(f"lote {i}: {c['done']} prontos, {c['running']} em andamento, "
                         f"{c['pending']} pendentes")
        text = "; ".join(parts)
        if text != last:
            print(text, flush=True)
            last = text
        time.sleep(2)


def _print_batch(s: dict) -> None:
    c = s["counts"]
    print(f"lote {s['id']} '{s['name']}' ({s['state']}): {s['total']} jobs; {c['done']} prontos, "
          f"{c['running']} em andamento, {c['pending']} pendentes, {c['cancelled']} cancelados")
    for row in s["summary"]:
        print(f"  {row['benchmark']}/{row['project']} {row['kind']}: {row['bugs']} bugs, "
              f"taxa de sucesso {row['taxa_sucesso']:.0%}, falhas {row['falhas']}")


def cmd_batch(home: Home, settings: Settings, args) -> int:
    if args.action == "create":
        kinds = [k for k in args.kinds.split(",") if k]
        bad = [k for k in kinds if k not in KINDS]
        if bad or not kinds:
            print(f"erro: tipos inválidos {bad or args.kinds}; use {', '.join(KINDS)}", file=sys.stderr)
            return 1
        bugs = [parse_bug(settings, b) for b in args.bugs]
        bid = create_batch(home, args.name, bugs, kinds, {})
        print(f"[OK] lote {bid} criado: {len(bugs)} bug(s) x {len(kinds)} tipo(s)")
        return 0
    if args.action == "status":
        _print_batch(batch_status(home, args.id))
        return 0
    # run: worker em primeiro plano até a fila esvaziar
    worker = Worker(home, settings)
    seen: set[int] = set()
    worker.start()
    try:
        time.sleep(1.5)   # deixa o worker recuperar jobs interrompidos antes de olhar a fila
        _wait_batches(home, seen)
    except KeyboardInterrupt:
        print("\nInterrompido: aguardando o job atual terminar (Ctrl+C de novo para sair já)...")
        worker.stop()
        try:
            worker.join()
        except KeyboardInterrupt:
            pass
        return 130
    worker.stop()
    worker.join(timeout=10)
    for i in sorted(seen):
        _print_batch(batch_status(home, i))
    err = worker.status()["last_error"]
    if err:
        print(f"aviso: último erro do worker: {err}")
    return 0


def cmd_export(home: Home, settings: Settings, args) -> int:
    if bool(args.batch) == bool(args.bugs):
        print("erro: informe --batch ID ou uma lista de bugs.", file=sys.stderr)
        return 1
    bugs = [parse_bug(settings, b) for b in args.bugs] if args.bugs else None
    dest = export(home, args.name, bugs=bugs, batch_id=args.batch)
    print(f"[OK] dataset exportado em {dest}")
    return 0


def cmd_recipes(home: Home, settings: Settings, args) -> int:
    files = sorted(home.recipes.glob("*/*.toml")) if home.recipes.is_dir() else []
    if args.alvo:
        files = [f for f in files if f"{f.parent.name}/{f.stem}" == args.alvo]
        if not files:
            print(f"Nenhuma receita para {args.alvo}.")
            return 1
        print(files[0].read_text(encoding="utf-8"))
        return 0
    for f in files:
        n = f.read_text(encoding="utf-8").count("[[recipe]]")
        print(f"{f.parent.name}/{f.stem}: {n} receita(s)")
    if not files:
        print(f"Nenhuma receita em {home.recipes}.")
    return 0


def cmd_reindex(home: Home, settings: Settings, args) -> int:
    print(f"{reindex(home)} execução(ões) reindexada(s).")
    return 0


def cmd_ui(home: Home, settings: Settings, args) -> int:
    try:
        from .web.app import serve
    except ImportError:
        print("A interface web ainda não disponível nesta instalação.")
        return 1
    port = args.port if args.port is not None else int(settings.get("ui", "port", 0) or 0)
    serve(home, port, not args.no_browser, args.window)
    return 0


# ----------------------------------------------------------------- selftest

def cmd_selftest(home: Home, settings: Settings, args) -> int:
    from . import selftest
    tmp = Path(tempfile.mkdtemp(prefix="jflp self test "))   # com espaço, de propósito
    ok = True

    def chk(cond, msg):
        nonlocal ok
        print(f"[{'OK' if cond else 'XX'}] {msg}")
        ok &= bool(cond)

    try:
        proj = tmp / "selftest_data"
        shutil.copytree(selftest.DATA / "fixed", proj)
        shutil.copy(selftest.DATA / "buggy" / selftest.CALC, proj / selftest.CALC)
        bug = local_bug(str(proj))
        kinds = ["maven", "jaguar2"]
        if (_jaguar1_lib(home, settings) / "jacocoagent.jar").is_file():
            kinds.append("jaguar1")
        else:
            print("[!!] Jaguar 1 ausente: teste do Jaguar 1 ignorado.")
        for kind in kinds:
            rel = execute(home, settings, bug, kind, RunOptions(auto_search=False, archive_checkout=False))
            run = load_run(home, rel)
            chk(st.is_success(run["status"]), f"{kind}: status {run['status']}")
            if kind == "jaguar2":
                csv = run_dir(home, rel) / "outputs" / "jaguar2.csv"
                want = f"demo/Calc,{selftest.bug_line()},"
                text = csv.read_text(encoding="utf-8") if csv.is_file() else ""
                chk(want in text, f"jaguar2.csv contém a linha defeituosa ({want})")
    except (MissingTool, StageError, ValueError) as e:
        chk(False, f"não foi possível rodar o autoteste: {getattr(e, 'message', None) or e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("Autoteste concluído com sucesso." if ok else "Autoteste falhou.")
    return 0 if ok else 1


# --------------------------------------------------------------------- main

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="jflp", description="JFLP Studio: localização de falhas com Jaguar")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="comando")

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        return p

    p = add("ui", cmd_ui, "abre a interface web")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--window", action="store_true", help="abre em janela própria (pywebview)")
    add("doctor", cmd_doctor, "mostra JDKs, Mavens e o perfil resolvido de cada tipo")
    p = add("tools", cmd_tools, "lista ou baixa JDK, Maven e Jaguar 1")
    ts = p.add_subparsers(dest="action", required=True, metavar="ação")
    ts.add_parser("list", help="ferramentas encontradas")
    g = ts.add_parser("get", help="baixa uma ferramenta")
    g.add_argument("what", choices=["jdk", "maven", "jaguar1"])
    g.add_argument("value", nargs="?", help="versão do JDK (major) ou do Maven")
    g.add_argument("--yes", action="store_true", help="não pergunta antes de baixar")
    p = add("fetch", cmd_fetch, "clona/atualiza os projetos do Bugs.jar configurados")
    p.add_argument("projeto", nargs="?")
    p = add("bugs", cmd_bugs, "lista os bugs de um benchmark")
    p.add_argument("benchmark", choices=["d4j", "bugsjar"])
    p.add_argument("projeto", nargs="?")
    p = add("prepare", cmd_prepare, "prepara (faz o checkout de) um bug")
    p.add_argument("bug")
    p = add("run", cmd_run, "executa um bug")
    p.add_argument("kind", choices=KINDS)
    p.add_argument("bug")
    p.add_argument("--all-tests", action="store_true", help="roda todos os testes (padrão: relevantes)")
    p.add_argument("--fix", action="append", default=[], metavar="ID")
    p.add_argument("--no-fix", action="append", default=[], metavar="ID")
    p.add_argument("--jdk", type=int)
    p.add_argument("--maven")
    p.add_argument("--no-search", action="store_true", help="sem busca automática de correções")
    p.add_argument("--no-archive", action="store_true", help="não arquiva o checkout")
    p = add("runs", cmd_runs, "lista execuções gravadas")
    p.add_argument("bug", nargs="?")
    p = add("compare", cmd_compare, "compara duas execuções")
    p.add_argument("rel_a")
    p.add_argument("rel_b")
    p = add("batch", cmd_batch, "lotes: create, run, status")
    bs = p.add_subparsers(dest="action", required=True, metavar="ação")
    c = bs.add_parser("create", help="cria um lote")
    c.add_argument("name")
    c.add_argument("kinds", help="tipos separados por vírgula, ex.: maven,jaguar2")
    c.add_argument("bugs", nargs="+")
    bs.add_parser("run", help="roda a fila em primeiro plano até esvaziar")
    s = bs.add_parser("status", help="situação de um lote")
    s.add_argument("id", type=int)
    p = add("export", cmd_export, "exporta um dataset")
    p.add_argument("name")
    p.add_argument("--batch", type=int)
    p.add_argument("bugs", nargs="*")
    p = add("recipes", cmd_recipes, "lista ou mostra receitas")
    p.add_argument("alvo", nargs="?", metavar="benchmark/projeto")
    add("reindex", cmd_reindex, "reconstrói o índice de execuções")
    add("selftest", cmd_selftest, "valida o ambiente com um projeto de exemplo")
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):   # console legado do Windows não deve derrubar a saída em PT
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = _parser()
    args = ap.parse_args(argv)
    if args.cmd == "tools" and args.action == "get" and args.what != "jaguar1" and not args.value:
        ap.error("informe a versão (ex.: `tools get jdk 11`)")
    home = Home.default().ensure()
    settings = load_settings(home)
    try:
        return args.fn(home, settings, args)
    except StageError as e:
        print(f"erro ({e.status}): {e.detail}", file=sys.stderr)
        return 2
    except MissingTool as e:
        print(f"erro: {e.message}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"erro: ferramenta ou arquivo não encontrado: {e.filename or e}", file=sys.stderr)
        return 2
