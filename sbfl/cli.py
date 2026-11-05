"""CLI `jflp`: doctor, ferramentas, bugs, execuções, lotes, exportação e interface web."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import cobertura as cb
from . import planilha
from . import status as st
from .adapters.base import Bug
from .compare import compare_runs
from .dataset import export
from .errors import StageError
from .execute import RunOptions, execute
from .home import Home, Settings, apply_work_dir, load_settings
from .importar import importar_pipeline
from .instance import MSG_OUTRA_INSTANCIA, WorkerLock
from .planilha import Enviador, enfileirar_todos, garantir_token, linhas_total, pendentes
from .prepare import bug_from_key, list_bugs, local_bug, prepare
from .proc import NO_WINDOW
from .queue import Worker, WorkerPool, active_batch_ids, batch_status, create_batch, delete_batch
from .store import list_runs, load_run, reindex, run_dir
from .toolchain.detect import find_jdks, find_mavens, wsl_status
from .toolchain.download import DownloadError, fetch, plan_jaguar1, plan_jdk, plan_maven
from .toolchain.profiles import PROFILES, MissingTool, resolve

KINDS = ("maven", "jaguar2", "jaguar1")


def parse_bug(settings: Settings, spec: str, module: str | None = None) -> Bug:
    """`local:<pasta>`, `local:<url>@<commit>`, uma pasta existente, `URL@commit` ou uma chave
    `benchmark/projeto/id`. `module` (opcional) vale só para pastas/URL locais."""
    if spec.startswith("local:"):
        return local_bug(spec[len("local:"):], module)
    if Path(spec.strip('"')).expanduser().is_dir():
        return local_bug(spec.strip('"'), module)
    try:
        return bug_from_key(settings, spec)
    except ValueError as e:
        try:                     # não é uma chave conhecida: talvez URL@commit
            return local_bug(spec, module)
        except ValueError:
            raise e from None


def _mb(n) -> str:
    return f"{n / 2**20:.0f} MB" if n else "tamanho desconhecido"


def _ver(v) -> str:
    return ".".join(str(n) for n in v)


# ------------------------------------------------------------------ ambiente

def cmd_doctor(home: Home, settings: Settings, args) -> int:
    print(f"Pasta do JFLP: {home.root}")
    real = home.virtualized_root()
    if real:
        print(f"[!!] O JFLP foi aberto de dentro de outro aplicativo (ex.: terminal do Claude) e o Windows grava "
              f"os dados em {real}. Abra pelo atalho JFLP Studio para usar a pasta normal.")
    jdks, mavens = find_jdks(home, settings), find_mavens(home, settings)
    print("JDKs:" if jdks else "[!!] Nenhum JDK encontrado.")
    for j in jdks:
        print(f"  [OK] JDK {j.major} ({j.version}) em {j.home} [{j.source}]")
    print("Mavens:" if mavens else "[!!] Nenhum Maven encontrado.")
    for m in mavens:
        print(f"  [OK] Maven {_ver(m.version)} em {m.home} [{m.source}]")
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=20,
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
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
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
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
    prep = prepare(home, settings, bug, log, refresh=args.refresh)
    print(f"[OK] {bug.key} preparado em {prep.pristine}")
    print(f"     módulo: {prep.module}; linhas defeituosas conhecidas: {len(prep.truth)}")
    return 0


def _print_log(line: str) -> None:
    if str(line).startswith("# jflp"):
        print(line)


def _opts(args) -> RunOptions:
    return RunOptions(
        test_scope="all" if args.all_tests else None,
        fixes_add=frozenset(args.fix), fixes_off=frozenset(args.no_fix),
        jdk=args.jdk, maven=args.maven, auto_search=not args.no_search,
        archive_checkout=False if args.no_archive else None, refresh=args.refresh, log_cb=_print_log)


def _unexpected_error(home: Home, bug: Bug, exc: Exception, before: set[str]) -> int:
    """Erro inesperado no execute: mensagem em português, traceback em work/erros e código 1."""
    log = home.work / "erros" / f"{datetime.now():%Y%m%d-%H%M%S-%f}.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("".join(traceback.format_exception(exc)), encoding="utf-8")
    except OSError:
        log = None
    print(f"[XX] {bug.key}: erro inesperado ({type(exc).__name__}): {exc}")
    try:   # execute encerra a execução como `desconhecido` antes de relançar
        new = [r["path"] for r in list_runs(home, bug.benchmark, bug.project, bug.bug_id)
               if r["path"] not in before]
    except Exception:  # noqa: BLE001
        new = []
    if new:
        print(f"     execução: {new[0]}")
    if log is not None:
        print(f"     detalhes (traceback): {log}")
    return 1


def cmd_run(home: Home, settings: Settings, args) -> int:
    try:
        bug = parse_bug(settings, args.bug, getattr(args, "module", None))
    except ValueError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    lock = WorkerLock(home)      # a mesma trava do worker: nunca duas fontes de execução ao mesmo tempo
    if not lock.acquire():
        print(f"erro: {MSG_OUTRA_INSTANCIA}.", file=sys.stderr)
        return 1
    try:
        return _run_locked(home, settings, args, bug)
    finally:
        lock.release()


def _run_locked(home: Home, settings: Settings, args, bug: Bug) -> int:
    before = {r["path"] for r in list_runs(home, bug.benchmark, bug.project, bug.bug_id)}
    try:
        rel = execute(home, settings, bug, args.kind, _opts(args))
    except Exception as e:  # noqa: BLE001 - qualquer falha inesperada vira mensagem + log
        return _unexpected_error(home, bug, e, before)
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
    home, aviso = apply_work_dir(home, settings)     # o worker usa a mesma pasta de trabalho da interface
    if aviso:
        print(f"aviso: {aviso}")
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
    if args.action == "cancel":     # só tira os jobs pendentes; o job em andamento se cancela na interface
        batch_status(home, args.id)
        Worker(home, settings).cancel(args.id)
        print(f"[OK] jobs pendentes do lote {args.id} cancelados")
        return 0
    if args.action == "delete":
        try:
            delete_batch(home, args.id)
        except ValueError as e:
            print(f"erro: {e}", file=sys.stderr)
            return 1
        print(f"[OK] lote {args.id} excluído do histórico (as execuções gravadas ficam)")
        return 0
    # run: worker em primeiro plano até a fila esvaziar
    lock = WorkerLock(home)
    if not lock.acquire():
        print(f"erro: {MSG_OUTRA_INSTANCIA}.", file=sys.stderr)
        return 1
    # planilha configurada: como no app, cada bug da cobertura que muda vai para a caixa de saída e é enviado
    enviador = Enviador(home, settings) if (settings.get("planilha", "url", "") or "").strip() else None
    aviso = (lambda k: (planilha.enfileirar(home, [k]), enviador.acordar())) if enviador else None
    worker = WorkerPool(home, settings, getattr(args, "workers", None) or 1, lock,   # a trava passa para o pool
                        on_bug_change=aviso)
    seen: set[int] = set()
    if enviador:
        enviador.start()
    worker.start()
    try:
        time.sleep(1.5)   # deixa o worker recuperar jobs interrompidos antes de olhar a fila
        _wait_batches(home, seen)
    except KeyboardInterrupt:
        print("\nInterrompido: aguardando o job atual terminar (Ctrl+C de novo para sair já)...")
        worker.stop(kill=False)
        try:
            worker.join()
        except KeyboardInterrupt:
            print("Encerrando os processos em andamento...")
            worker.stop()     # mata o Maven; o job volta para a fila
            worker.join(15)
        if enviador:
            enviador.stop()
        return 130
    worker.stop(kill=False)
    worker.join(timeout=10)
    if enviador:
        enviador.stop()
        enviador.join(timeout=30)
        try:
            enviador.ciclo()        # última rodada de envio antes de sair
        except Exception as e:      # noqa: BLE001 - a fila já terminou; o envio fica para a próxima
            print(f"aviso: envio final à planilha falhou: {e}")
    for i in sorted(seen):
        _print_batch(batch_status(home, i))
    err = worker.status()["last_error"]
    if err:
        print(f"aviso: último erro do worker: {err}")
    return 0


# --------------------------------------------------------------- cobertura

_SIT_ROTULO = {cb.SIT_OK: "OK", cb.SIT_PARCIAL: "parcial", cb.SIT_FALHOU: "falhou",
               cb.SIT_NAO_EXEC: "não executável", cb.SIT_RODANDO: "rodando", cb.SIT_PENDENTE: "pendente"}


def _tempo(segundos: int) -> str:
    h, m = divmod(int(segundos) // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min"


def _cobertura_bid(home: Home) -> int | None:
    bid = cb.batch_id(home)
    if bid is None:
        print("erro: ainda não há cobertura; rode `jflp cobertura sincronizar <pasta>` primeiro.",
              file=sys.stderr)
    return bid


def cmd_cobertura(home: Home, settings: Settings, args) -> int:
    home, aviso = apply_work_dir(home, settings)     # mesma pasta de trabalho que a interface web usa
    if aviso:
        print(f"aviso: {aviso}")
    acao = args.action
    if acao == "sincronizar":
        pasta = args.pasta or settings.get("bugsjar", "root")
        if not pasta:
            print("erro: informe a pasta da coleção ou defina bugsjar.root no config.toml.", file=sys.stderr)
            return 1
        if not Path(pasta).expanduser().is_dir():
            print(f"erro: a pasta da coleção não existe: {pasta}", file=sys.stderr)
            return 1
        r = cb.sincronizar(home, settings, Path(pasta).expanduser())
        for av in r["avisos"]:
            print(f"aviso: {av}")
        print(f"[OK] cobertura (lote {r['batch_id']}): {r['total_bugs']} bugs; {r['adicionados']} jobs novos")
        return 0
    if acao == "importar":
        r = importar_pipeline(home, settings, Path(args.pasta).expanduser())
        print(f"[OK] importados: {r['maven']} Maven, {r['jaguar1']} Jaguar 1; ignorados: {r['ignorados']}")
        for e in r["erros"]:
            print(f"erro: {e}", file=sys.stderr)
        return 1 if r["erros"] else 0
    if acao == "planilha":
        return _cobertura_planilha(home, settings, args)
    bid = _cobertura_bid(home)
    if bid is None:
        return 1
    if acao == "status":
        r = cb.resumo(home, bid)
        ps = r["por_situacao"]
        print(f"Cobertura: {r['concluidos']} de {r['total']} bugs concluídos")
        print("  " + "; ".join(f"{_SIT_ROTULO[s]} {ps[s]}" for s in ps))
        for p in r["por_projeto"]:
            print(f"  {p['projeto']}: {p['concluidos']} de {p['total']} concluídos "
                  f"(OK {p[cb.SIT_OK]}, parcial {p[cb.SIT_PARCIAL]}, falhou {p[cb.SIT_FALHOU]}, "
                  f"não executável {p[cb.SIT_NAO_EXEC]}, pendente {p[cb.SIT_PENDENTE]})")
        ativas = cb.kinds_ativos(home)
        for k, c in r["por_etapa"].items():
            print(f"  etapa {k}{'' if k in ativas else ' (fora de cobertura.kinds)'}: OK {c[cb.SIT_OK]}, "
                  f"falhou {c[cb.SIT_FALHOU]}, não executável {c[cb.SIT_NAO_EXEC]}, rodando {c[cb.SIT_RODANDO]}, "
                  f"pendente {c[cb.SIT_PENDENTE]}")
        if r["ritmo_h"] is not None:
            print(f"Ritmo: {r['ritmo_h']:.1f} bugs por hora")
        if r["restante_s"] is not None:
            print(f"Restante estimado: {_tempo(r['restante_s'])}")
        return 0
    if acao == "refazer":
        n = cb.refazer(home, bid, None, so_falhas=not args.todos)
        print(f"[OK] {n} job(s) devolvido(s) à fila; rode `jflp batch run` para processá-los.")
        return 0
    return 1


def _cobertura_planilha(home: Home, settings: Settings, args) -> int:
    url = (settings.get("planilha", "url", "") or "").strip()
    if not args.reenviar:
        token = garantir_token(settings)
        print(f"Token (propriedade JFLP_TOKEN no Apps Script): {token or '(ainda não gerado)'}")
        print("Planilha: " + ("URL configurada" if url else "URL não configurada (tela Ambiente)"))
        print(f"Linhas na caixa de saída: {linhas_total(home)}; pendentes de envio: {pendentes(home)}")
        return 0
    if not url:
        print("erro: a URL do app da web da planilha não está configurada (tela Ambiente, seção Planilha).",
              file=sys.stderr)
        return 1
    if _cobertura_bid(home) is None:
        return 1
    n = enfileirar_todos(home)
    print(f"{n} linha(s) na fila de envio; enviando...")
    envio = Enviador(home, settings)
    anterior, parados = pendentes(home), 0
    while True:
        envio.ciclo()           # um ciclo manda até LOTE linhas; sem espera entre eles
        erro = envio.status()["erro"]
        if erro:
            print(f"erro: {erro}", file=sys.stderr)
            print(f"Ainda faltam {pendentes(home)} linha(s); rode o comando de novo depois de corrigir.",
                  file=sys.stderr)
            return 1
        atual = pendentes(home)
        if atual == 0:
            break
        parados = 0 if atual < anterior else parados + 1
        if parados >= 3:        # três ciclos seguidos sem andar: para em vez de girar à toa
            print(f"erro: o envio não avançou em 3 ciclos seguidos; faltam {atual} linha(s).", file=sys.stderr)
            return 1
        anterior = atual
    print(f"[OK] {n} linha(s) enviada(s) para a planilha")
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
    return serve(home, port, not args.no_browser, args.window, args.app)


def cmd_atalho(home: Home, settings: Settings, args) -> int:
    from . import desktop
    try:
        paths = desktop.remove_shortcuts() if args.remover else desktop.create_shortcuts()
    except OSError as e:
        print(f"erro: não foi possível {'remover' if args.remover else 'criar'} o atalho: {e}", file=sys.stderr)
        return 2
    if args.remover:
        print("Atalho removido de:" if paths else "Nenhum atalho do JFLP Studio encontrado.")
    else:
        print("Atalho JFLP Studio criado em:")
    for p in paths:
        print(f"  {p}")
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
    p.add_argument("--app", action="store_true",
                   help="abre numa janela de app do Edge/Chrome/Chromium e encerra ao fechá-la")
    p = add("atalho", cmd_atalho, "cria o atalho JFLP Studio na Área de Trabalho e no Menu Iniciar")
    p.add_argument("--remover", action="store_true", help="remove os atalhos")
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
    p.add_argument("--refresh", action="store_true", help="refaz a cópia pristine do zero")
    p = add("run", cmd_run, "executa um bug (chave, pasta local ou URL@commit)")
    p.add_argument("kind", choices=KINDS)
    p.add_argument("bug", help="chave benchmark/projeto/id, pasta local ou URL@commit")
    p.add_argument("--module", help="subpasta com pom.xml (pasta local ou URL@commit)")
    p.add_argument("--all-tests", action="store_true", help="roda todos os testes (padrão: relevantes)")
    p.add_argument("--fix", action="append", default=[], metavar="ID",
                   help="acrescenta uma correção às detectadas (repita para várias)")
    p.add_argument("--no-fix", action="append", default=[], metavar="ID")
    p.add_argument("--jdk", type=int)
    p.add_argument("--maven")
    p.add_argument("--no-search", action="store_true", help="sem busca automática de correções")
    p.add_argument("--no-archive", action="store_true", help="não arquiva o checkout")
    p.add_argument("--refresh", action="store_true", help="refaz a cópia pristine do zero antes de rodar")
    p = add("runs", cmd_runs, "lista execuções gravadas")
    p.add_argument("bug", nargs="?")
    p = add("compare", cmd_compare, "compara duas execuções")
    p.add_argument("rel_a")
    p.add_argument("rel_b")
    p = add("batch", cmd_batch, "lotes: create, run, status, cancel, delete")
    bs = p.add_subparsers(dest="action", required=True, metavar="ação")
    c = bs.add_parser("create", help="cria um lote")
    c.add_argument("name")
    c.add_argument("kinds", help="tipos separados por vírgula, ex.: maven,jaguar2")
    c.add_argument("bugs", nargs="+")
    r = bs.add_parser("run", help="roda a fila em primeiro plano até esvaziar")
    r.add_argument("--workers", type=int, default=None, help="jobs em paralelo (padrão: 1)")
    s = bs.add_parser("status", help="situação de um lote")
    s.add_argument("id", type=int)
    s = bs.add_parser("cancel", help="cancela os jobs pendentes de um lote")
    s.add_argument("id", type=int)
    s = bs.add_parser("delete", help="exclui um lote encerrado do histórico")
    s.add_argument("id", type=int)
    p = add("cobertura", cmd_cobertura, "cobertura do bugs-dot-jar: sincronizar, status, importar, refazer, planilha")
    cs = p.add_subparsers(dest="action", required=True, metavar="ação")
    s = cs.add_parser("sincronizar", help="cria o lote de cobertura e acrescenta os bugs novos da coleção")
    s.add_argument("pasta", nargs="?", help="pasta da coleção (padrão: bugsjar.root)")
    cs.add_parser("status", help="resumo da cobertura: bugs por situação e por projeto")
    s = cs.add_parser("importar", help="importa os resultados do pipeline PowerShell")
    s.add_argument("pasta", help="pasta relatorios_cobertura (<projeto>/<bug>/summary.json)")
    s = cs.add_parser("refazer", help="devolve à fila os bugs com falha (ou todos, com --todos)")
    s.add_argument("--todos", action="store_true", help="refaz todos os jobs concluídos, não só as falhas")
    s = cs.add_parser("planilha", help="envia linhas à planilha do Google")
    s.add_argument("--reenviar", action="store_true", help="reenvia todas as linhas da cobertura")
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
    if args.cmd == "ui":
        from . import desktop
        desktop.ensure_streams(home)      # pythonw não tem console: a saída vai para <home>/ui.log
    try:
        settings = load_settings(home)
    except ValueError as e:         # config.toml inválido: mensagem com linha/coluna, sem traceback
        print(f"erro: {e}", file=sys.stderr)
        return 1
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
