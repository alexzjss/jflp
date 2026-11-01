"""Núcleo da cobertura: o lote único que roda Maven, Jaguar 1 e Jaguar 2 em todos os bugs do Bugs.jar,
e a leitura dele como "bugs com etapas" (ok / parcial / falhou / não executável / pendente)."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import queue as jq
from . import status as st
from . import store
from .home import Home, Settings
from .prepare import bugsjar_repo_bugs, is_bugsjar_repo

KINDS = ("maven", "jaguar1", "jaguar2")
SIT_OK, SIT_PARCIAL, SIT_FALHOU, SIT_NAO_EXEC, SIT_RODANDO, SIT_PENDENTE = (
    "ok", "parcial", "falhou", "nao_executavel", "rodando", "pendente")
NOME_LOTE = "Cobertura bugs-dot-jar"
JANELA_RITMO_H = 6
_CHUNK = 500
_run_cache: dict[tuple[str, str], dict] = {}
_SYNC = threading.Lock()     # dois cliques em "Começar cobertura" não criam dois lotes


def _con(home: Home) -> sqlite3.Connection:
    return jq._connect(home)


def batch_id(home: Home) -> int | None:
    """Lote de cobertura (o mais novo, se houver vários); None se ainda não foi criado."""
    with contextlib.closing(_con(home)) as con:
        for r in con.execute("SELECT id, meta FROM batches ORDER BY id DESC"):
            try:
                if (json.loads(r["meta"] or "{}")).get("cobertura"):
                    return r["id"]
            except ValueError:
                continue
    return None


def sincronizar(home: Home, settings: Settings, root: Path) -> dict:
    """Cria o lote de cobertura se faltar e acrescenta os jobs (bug, tipo) que ainda não existem. Idempotente."""
    root = Path(root)
    avisos: list[str] = []
    repos = []
    try:
        pastas = sorted(x for x in root.iterdir() if x.is_dir() and is_bugsjar_repo(x))
    except OSError as e:
        pastas = []
        avisos.append(f"Não foi possível ler {root}: {e}")
    for repo in pastas:
        try:
            achados = bugsjar_repo_bugs(repo)
        except Exception as e:      # noqa: BLE001 - um repositório quebrado não derruba a sincronização
            achados, motivo = [], str(e)
        else:
            motivo = ""
        if not achados:
            avisos.append(f"{repo.name}: nenhum bug encontrado" + (f" ({motivo})" if motivo else "")
                          + "; repositório ignorado.")
            continue
        repos.append(achados)
    total_bugs = sum(len(r) for r in repos)
    n = len(repos)
    novos = 0
    with _SYNC, contextlib.closing(_con(home)) as con, con:
        con.execute("BEGIN IMMEDIATE")      # trava de escrita antes de procurar o lote (vale entre processos)
        bid = batch_id(home)
        if bid is None:
            opts = {"test_scope": "all", "auto_search": True, "archive_checkout": False,
                    "params": {"cobertura": True, "max_attempts": 8}}
            cur = con.execute(
                "INSERT INTO batches(name, created, kinds, options, state, meta) VALUES(?,?,?,?,?,?)",
                (NOME_LOTE, datetime.now().isoformat(timespec="seconds"), json.dumps(list(KINDS)),
                 json.dumps(opts), "active", json.dumps({"cobertura": True, "root": str(root)})))
            bid = cur.lastrowid
        existentes = {(r[0], r[1]) for r in con.execute("SELECT bug_key, kind FROM jobs WHERE batch_id=?", (bid,))}
        base = (con.execute("SELECT MAX(idx) FROM jobs WHERE batch_id=?", (bid,)).fetchone()[0])
        base = 0 if base is None else base + 1
        pedidos = []        # (rank, bug, [kinds faltando])
        for p, lista in enumerate(repos):
            for i, bug in enumerate(lista):
                faltam = [k for k in KINDS if (bug.key, k) not in existentes]
                if faltam:
                    pedidos.append((i * n + p, bug, faltam))
        pedidos.sort(key=lambda x: x[0])
        idx = base
        for _rank, bug, faltam in pedidos:
            for k in faltam:
                con.execute("INSERT INTO jobs(batch_id, idx, bug_key, bug_extra, kind, state) "
                            "VALUES(?,?,?,?,?,'pending')", (bid, idx, bug.key, json.dumps(bug.extra or {}), k))
                idx += 1
                novos += 1
        if novos:       # lote já concluído/cancelado volta a andar com os jobs novos
            con.execute("UPDATE batches SET state='active' WHERE id=? AND state IN ('done','cancelled')", (bid,))
    return {"batch_id": bid, "adicionados": novos, "total_bugs": total_bugs, "avisos": avisos}


def _config_legado(run: dict) -> bool:
    for c in run.get("attempt_configs") or []:
        try:
            if int((c or {}).get("jdk")) == 7:
                return True
        except (TypeError, ValueError):
            continue
    return False


def etapa(job: dict, run: dict | None) -> str:
    """Situação de um job (uma etapa de um bug)."""
    if job.get("forcado") == SIT_NAO_EXEC:
        return SIT_NAO_EXEC
    state = job.get("state")
    if state == "running":
        return SIT_RODANDO
    if state == "pending":
        return SIT_PENDENTE
    if state != "done":
        return SIT_FALHOU       # cancelado
    s = (run or {}).get("status") or ""
    if st.is_success(s) or (s == st.SEM_TESTE_FALHANDO and job.get("kind") == "maven"):
        return SIT_OK
    if s == st.PROJETO_INVALIDO:
        return SIT_NAO_EXEC
    if run and _config_legado(run):
        return SIT_NAO_EXEC
    return SIT_FALHOU


def situacao(etapas: dict) -> str:
    """Situação do bug a partir das etapas. Só é `parcial` (concluído) quando nenhuma etapa está
    pendente ou rodando: algumas ok e outras falhadas/não executáveis."""
    v = list(etapas.values())
    if any(e == SIT_RODANDO for e in v):
        return SIT_RODANDO
    if v and all(e == SIT_OK for e in v):
        return SIT_OK
    if any(e == SIT_PENDENTE for e in v):
        return SIT_PENDENTE
    if any(e == SIT_OK for e in v):
        return SIT_PARCIAL
    if all(e == SIT_NAO_EXEC for e in v):
        return SIT_NAO_EXEC
    return SIT_FALHOU


# ------------------------------------------------------------------ leitura em massa

def _load(home: Home, bid: int) -> tuple[list[dict], dict[str, dict]]:
    """Todos os jobs do lote e as linhas de `runs` dos concluídos (uma consulta + SELECT em blocos)."""
    with contextlib.closing(_con(home)) as con:
        jobs = [dict(r) for r in con.execute("SELECT * FROM jobs WHERE batch_id=? ORDER BY idx", (bid,))]
    paths = list({j["run_rel"] for j in jobs if j["state"] == "done" and j["run_rel"]})
    runs: dict[str, dict] = {}
    if paths:
        with contextlib.closing(store._connect(home)) as con:
            for i in range(0, len(paths), _CHUNK):
                bloco = paths[i:i + _CHUNK]
                marks = ",".join("?" * len(bloco))
                for r in con.execute(f"SELECT * FROM runs WHERE path IN ({marks})", bloco):
                    runs[r["path"]] = dict(r)
    return jobs, runs


def _run_of(home: Home, job: dict, rows: dict[str, dict], detalhe: bool) -> dict | None:
    """Run do job: o status vem da tabela `runs`; o `run.json` só é lido (e guardado) nas falhas."""
    rel = job.get("run_rel")
    if job["state"] != "done" or not rel:
        return None
    row = rows.get(rel)
    run = {"status": (row or {}).get("status")}
    if (detalhe and run["status"] and not st.is_success(run["status"])) or row is None:
        chave = (str(home.db_path), rel)
        if chave not in _run_cache:
            try:
                _run_cache[chave] = store.load_run(home, rel)
            except (OSError, ValueError):
                _run_cache[chave] = {}
        full = _run_cache[chave]
        if full:
            run = {**full, "status": full.get("status") or run["status"]}
    return run


def _montar(home: Home, bid: int, detalhe: bool) -> tuple[list[dict], dict[str, dict]]:
    jobs, rows = _load(home, bid)
    por_bug: dict[str, dict] = {}
    for j in jobs:
        proj_bug = j["bug_key"].split("/", 2)
        b = por_bug.setdefault(j["bug_key"], {
            "key": j["bug_key"], "projeto": proj_bug[1] if len(proj_bug) > 2 else "",
            "bug": proj_bug[-1], "etapas": {}, "jobs": {}, "runs": {}, "origem": None})
        run = _run_of(home, j, rows, detalhe)
        b["etapas"][j["kind"]] = etapa(j, run)
        b["jobs"][j["kind"]] = j
        b["runs"][j["kind"]] = run
        if b["origem"] is None and j.get("origem"):
            b["origem"] = j["origem"]
    out = list(por_bug.values())
    for b in out:
        b["situacao"] = situacao(b["etapas"])
    return out, rows


def tabela(home: Home, bid: int, projeto: str | None = None) -> list[dict]:
    """Bugs do lote COM as execuções (`runs`: kind -> run), para a tabela e os filtros em memória."""
    lista, _ = _montar(home, bid, detalhe=True)
    return [b for b in lista if not projeto or b["projeto"] == projeto]


def filtrar(lista: list[dict], situacoes=None, busca: str | None = None) -> list[dict]:
    """Filtra bugs já montados por situação e por texto (no nome do bug ou na causa do diagnóstico)."""
    q = (busca or "").strip().lower()
    out = []
    for b in lista:
        if situacoes and b["situacao"] not in situacoes:
            continue
        if q:
            causas = " ".join(str(((r or {}).get("diagnosis") or {}).get("cause") or "") for r in b["runs"].values())
            if q not in b["bug"].lower() and q not in causas.lower():
                continue
        out.append(b)
    return out


def bugs(home: Home, bid: int, projeto: str | None = None, situacoes=None, busca: str | None = None) -> list[dict]:
    """Um dict por bug do lote (sem tocar no git), com filtros opcionais."""
    out = filtrar(tabela(home, bid, projeto), situacoes, busca)
    for b in out:
        b.pop("runs", None)
    return out


def _fim(job: dict, rows: dict) -> datetime | None:
    r = rows.get(job.get("run_rel") or "")
    if not r or not r.get("started"):
        return None
    try:
        return datetime.fromisoformat(r["started"]) + timedelta(seconds=r.get("seconds") or 0)
    except ValueError:
        return None


def resumo(home: Home, bid: int) -> dict:
    lista, rows = _montar(home, bid, detalhe=True)
    return _resumir(lista, rows)


def _resumir(lista: list[dict], rows: dict[str, dict]) -> dict:
    por_sit = {s: 0 for s in (SIT_OK, SIT_PARCIAL, SIT_FALHOU, SIT_NAO_EXEC, SIT_RODANDO, SIT_PENDENTE)}
    projetos: dict[str, dict] = {}
    agora = datetime.now()
    fins, abertos = [], 0
    for b in lista:
        s = b["situacao"]
        por_sit[s] += 1
        p = projetos.setdefault(b["projeto"], {"projeto": b["projeto"], "total": 0, "concluidos": 0, **{
            k: 0 for k in (SIT_OK, SIT_PARCIAL, SIT_FALHOU, SIT_NAO_EXEC, SIT_PENDENTE)}})
        p["total"] += 1
        if s in p:
            p[s] += 1
        if s in (SIT_OK, SIT_PARCIAL, SIT_FALHOU, SIT_NAO_EXEC):
            p["concluidos"] += 1
        estados = b["etapas"].values()
        if any(e in (SIT_PENDENTE, SIT_RODANDO) for e in estados):
            abertos += 1
            continue
        tempos = [_fim(j, rows) for j in b["jobs"].values()]
        tempos = [t for t in tempos if t]
        if tempos and max(tempos) >= agora - timedelta(hours=JANELA_RITMO_H):
            fins.append(max(tempos))
    ritmo = restante = None
    if fins:
        horas = min(float(JANELA_RITMO_H), max((agora - min(fins)).total_seconds() / 3600, 0.25))
        ritmo = len(fins) / horas
        restante = round(abertos / ritmo * 3600) if abertos else 0
    concluidos = sum(por_sit[s] for s in (SIT_OK, SIT_PARCIAL, SIT_FALHOU, SIT_NAO_EXEC))
    return {"total": len(lista), "concluidos": concluidos, "por_situacao": por_sit,
            "por_projeto": sorted(projetos.values(), key=lambda p: p["projeto"]),
            "ritmo_h": ritmo, "restante_s": restante}


CONCLUIDAS = (SIT_OK, SIT_PARCIAL, SIT_FALHOU, SIT_NAO_EXEC)


def painel(home: Home, bid: int, n_ultimos: int = 5) -> dict:
    """Tudo que o painel precisa com uma única leitura: o resumo mais `rodando` (bugs com etapa em
    andamento: key, projeto, bug, etapas) e `ultimos` (os N concluídos mais recentes, com `fim`)."""
    lista, rows = _montar(home, bid, detalhe=True)
    out = _resumir(lista, rows)
    out["rodando"] = {b["key"]: {"key": b["key"], "projeto": b["projeto"], "bug": b["bug"],
                                 "etapas": dict(b["etapas"])}
                      for b in lista if SIT_RODANDO in b["etapas"].values()}
    ult = []
    for b in lista:
        if b["situacao"] in CONCLUIDAS:
            fins = [f for f in (_fim(j, rows) for j in b["jobs"].values()) if f]
            ult.append({"key": b["key"], "projeto": b["projeto"], "bug": b["bug"], "situacao": b["situacao"],
                        "fim": max(fins) if fins else None})
    ult.sort(key=lambda u: u["fim"] or datetime.min, reverse=True)
    out["ultimos"] = ult[:n_ultimos]
    return out


# ------------------------------------------------------------------ ações

def _chaves_alvo(home: Home, bid: int, bug_keys) -> list[str]:
    if bug_keys is not None:
        return list(bug_keys)
    lista, _ = _montar(home, bid, detalhe=True)
    return [b["key"] for b in lista if b["situacao"] in (SIT_FALHOU, SIT_PARCIAL)]


def refazer(home: Home, bid: int, bug_keys: list[str] | None = None, so_falhas: bool = True) -> int:
    """Devolve à fila os jobs concluídos dos bugs (só as falhas, por padrão). Jobs rodando não são tocados."""
    alvo = set(_chaves_alvo(home, bid, bug_keys))
    if not alvo:
        return 0
    jobs, rows = _load(home, bid)
    ids = []
    for j in jobs:
        if j["bug_key"] not in alvo or j["state"] not in ("done", "cancelled"):
            continue
        if so_falhas and etapa(j, _run_of(home, j, rows, True)) in (SIT_OK, SIT_NAO_EXEC):
            continue
        ids.append(j["id"])
    with contextlib.closing(_con(home)) as con, con:
        for i in range(0, len(ids), _CHUNK):
            bloco = ids[i:i + _CHUNK]
            marks = ",".join("?" * len(bloco))
            con.execute("UPDATE jobs SET state='pending', run_rel=NULL, error=NULL, seconds=NULL, forcado=NULL, "
                        f"origem=NULL WHERE state IN ('done','cancelled') AND id IN ({marks})", bloco)
        if ids:
            con.execute("UPDATE batches SET state='active' WHERE id=? AND state IN ('done','cancelled')", (bid,))
    return len(ids)


def marcar_nao_executavel(home: Home, bid: int, bug_keys: list[str]) -> int:
    """Marca os bugs como não executáveis: os jobs pendentes viram concluídos e todos ganham `forcado`."""
    n = 0
    with contextlib.closing(_con(home)) as con, con:
        for k in bug_keys:
            cur = con.execute("UPDATE jobs SET forcado='nao_executavel', "
                              "state=CASE WHEN state='pending' THEN 'done' ELSE state END "
                              "WHERE batch_id=? AND bug_key=? AND state IN ('pending','done')", (bid, k))
            n += cur.rowcount
        jq._close_if_finished(con, bid)
    return n


def priorizar(home: Home, bid: int, bug_keys: list[str]) -> int:
    """Move os jobs pendentes dos bugs para o começo da fila (mantendo a ordem dos tipos)."""
    with contextlib.closing(_con(home)) as con, con:
        mn = con.execute("SELECT MIN(idx) FROM jobs WHERE batch_id=?", (bid,)).fetchone()[0] or 0
        pend = []
        for k in bug_keys:
            pend += [r[0] for r in con.execute(
                "SELECT id FROM jobs WHERE batch_id=? AND bug_key=? AND state='pending' ORDER BY idx", (bid, k))]
        base = mn - len(pend)
        for i, jid in enumerate(pend):
            con.execute("UPDATE jobs SET idx=? WHERE id=?", (base + i, jid))
    return len(pend)
