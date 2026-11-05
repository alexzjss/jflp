"""Envio automático dos resultados da cobertura para a planilha do Google (Apps Script publicado como app da web).

Cada bug concluído/alterado vira uma linha na caixa de saída (tabela `planilha_outbox` do SQLite); o `Enviador`
manda as linhas pendentes em lotes, com espera crescente quando falha. Nada se perde se o app fechar."""
from __future__ import annotations

import contextlib
import json
import logging
import secrets
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime

from . import bugview as bv
from . import cobertura as cb
from . import queue as jq
from . import status as st
from . import store
from .home import Home, Settings
from .outputs import ranking_rows
from .prepare import bugsjar_label

COLUNAS = ["Projeto", "Bug", "Resumo", "Link JIRA", "Situação", "Última execução", "Tentativas", "Origem",
           "Maven testes", "Maven falhas", "Maven erros", "Testes que falharam", "Bateu com o catálogo?",
           "J1 status", "J1 posição do patch", "J1 topo", "J2 status", "J2 posição do patch", "J2 topo",
           "Veredito J1", "Veredito J2", "Concordância J1×J2", "Motivo", "Perfil"]

LOTE = 100
TIMEOUT_S = 60
ESPERA_INICIAL_S = 30
ESPERA_MAXIMA_S = 900
ESPERA_OCIOSA_S = 60
RECONCILIAR_S = 600
_SITUACAO = {cb.SIT_OK: "OK", cb.SIT_PARCIAL: "parcial", cb.SIT_FALHOU: "falhou", cb.SIT_NAO_EXEC: "não executável",
             cb.SIT_RODANDO: "rodando", cb.SIT_PENDENTE: "pendente"}
_MAX_TXT = 500


# ------------------------------------------------------------------ uma linha da planilha

def _perfil(runs: dict) -> str:
    """moderno / legado / legado offline, pela última tentativa da execução mais representativa."""
    for kind in ("jaguar2", "jaguar1", "maven"):
        cfgs = (runs.get(kind) or {}).get("attempt_configs") or []
        if not cfgs:
            continue
        c = cfgs[-1] or {}
        if (c.get("params") or {}).get("offline"):
            return "legado offline"
        try:
            return "legado" if int(c.get("jdk")) == 7 else "moderno"
        except (TypeError, ValueError):
            return "moderno"
    return ""


def _motivo(bug: dict, runs: dict) -> str:
    if bug["situacao"] == cb.SIT_NAO_EXEC:
        causas = [((r or {}).get("diagnosis") or {}).get("cause") for r in runs.values()]
        return next((c for c in causas if c), "não executável")
    partes = []
    for kind in cb.KINDS:
        if bug["etapas"].get(kind) != cb.SIT_FALHOU:
            continue
        run = runs.get(kind) or {}
        causa = (run.get("diagnosis") or {}).get("cause") or bug["jobs"][kind].get("error") or run.get("status") or "falhou"
        partes.append(f"{bv.KIND_LABEL[kind]}: {str(causa).strip()}")
    return " | ".join(partes)[:_MAX_TXT]


def _topo(b: bv._Bug, kind: str) -> str:
    sc = b.scores(kind)
    rows = ranking_rows(sc)[:1] if sc else []
    return f"{rows[0][0].rsplit('.', 1)[-1]}:{rows[0][1]}" if rows else ""


def linha(home: Home, bug: dict, resumo: str | None = None) -> list:
    """Uma linha na ordem de `COLUNAS`. `bug` é um item de `cobertura.bugs`; `resumo` (do JIRA) evita ler o git."""
    jobs = bug["jobs"]
    primeiro = next(iter(jobs.values()), {})
    extra = primeiro.get("extra") or json.loads(primeiro.get("bug_extra") or "{}")
    b = bv._Bug(home, {"key": bug["key"], "jobs": jobs, "extra": extra})
    runs = {k: b.run(k) for k in jobs}
    ramo = bug["key"].split("/", 2)[-1]
    if resumo is None:
        resumo = bv.resumo_bugsjar(b.repo, b.ramo)["resumo"] if b.repo else ""
    fins = [r.get("finished") or r.get("started") or "" for r in runs.values() if r]
    tentativas = max([int(r.get("attempts") or len(r.get("attempt_configs") or []) or 0)
                      for r in runs.values() if r] or [0])
    mv = runs.get("maven")
    testes = (mv or {}).get("tests") or {}
    falhas_txt = bate = ""
    if mv is not None and b.status("maven") is not None:
        try:
            falhas = [f["test"] for f in b.falhas_maven()]
        except (OSError, ValueError):
            falhas = []
        falhas_txt = "; ".join(falhas)[:_MAX_TXT]
        rodou = st.is_success(b.status("maven") or "") or b.status("maven") == st.SEM_TESTE_FALHANDO
        if not rodou:
            bate = ""       # o Maven não chegou a rodar os testes: não há o que comparar
        elif not falhas:
            bate = "não reproduziu"
        else:
            cat = b.catalogo()
            estado = bv.esperado_maven(falhas, cat["tests"] if cat else None)["estado"]
            bate = {"iguais": "sim", "diferentes": "não", "sem_catalogo": "sem catálogo"}[estado]
    jag, vers = {}, {}
    for k in ("jaguar1", "jaguar2"):
        if not b.job(k):
            jag[k], vers[k] = ["", "", ""], ""
            continue
        cel = bv._celula(b, k)
        code, pos = b.posicao(k)
        m = pos["melhor"] if pos else None
        jag[k] = [cel["label"] or cel["texto"], f"{m['expected']:g}" if m else "", _topo(b, k)]
        sim, rot = bv.VEREDITOS[code]
        vers[k] = f"{sim} {rot}"
    conc = ""
    if b.job("jaguar1") and b.job("jaguar2"):
        c = bv._concordancia(b, bv.TOP_N)
        if c:
            conc = f"{c['texto_comuns']} em comum" + (", mesma classe no topo" if c["mesma_classe_topo"] else "")
    return [bug["projeto"], bugsjar_label(ramo), resumo, bv.jira_url(bugsjar_label(ramo)),
            _SITUACAO.get(bug["situacao"], bug["situacao"]), max(fins, default=""), tentativas,
            bug.get("origem") or "app",
            testes.get("total", "") if mv else "", testes.get("failed", "") if mv else "",
            testes.get("error", "") if mv else "", falhas_txt, bate,
            *jag["jaguar1"], *jag["jaguar2"], vers["jaguar1"], vers["jaguar2"], conc,
            _motivo(bug, runs), _perfil(runs)]


# ------------------------------------------------------------------ caixa de saída

_log = logging.getLogger("jflp.planilha")
_falhas: dict[str, dict[str, str]] = {}     # por banco: chave do bug -> erro ao montar a linha
_falhas_lock = threading.Lock()


def _con(home: Home) -> sqlite3.Connection:
    con = jq._connect(home)
    con.execute("CREATE TABLE IF NOT EXISTS planilha_outbox("
                "bug_key TEXT PRIMARY KEY, linha TEXT, atualizado TEXT, enviado TEXT, assinatura TEXT)")
    if "assinatura" not in {r[1] for r in con.execute("PRAGMA table_info(planilha_outbox)")}:
        try:
            con.execute("ALTER TABLE planilha_outbox ADD COLUMN assinatura TEXT")
        except sqlite3.OperationalError:    # outra conexão migrou primeiro
            pass
    return con


def assinatura(bug: dict) -> str:
    """Impressão digital barata do estado de um bug (situação + etapas + origem): muda quando a linha deve mudar."""
    return json.dumps([bug["situacao"], sorted(bug["etapas"].items()), bug.get("origem") or ""],
                      ensure_ascii=False)


def falhas_linha(home: Home) -> dict:
    """{"n": bugs cuja linha não pôde ser montada, "erro": último motivo}."""
    with _falhas_lock:
        f = _falhas.get(str(home.db_path)) or {}
        return {"n": len(f), "erro": next(reversed(f.values()), None)}


def _registrar_falha(home: Home, chave: str, e: BaseException | None) -> None:
    with _falhas_lock:
        f = _falhas.setdefault(str(home.db_path), {})
        f.pop(chave, None)
        if e is not None:
            f[chave] = f"{chave}: {e!r}"[-300:]
    if e is not None:
        _log.warning("não foi possível montar a linha de %s: %r", chave, e)


def _bugs_do_lote(home: Home, chaves: list[str] | None) -> list[dict]:
    """Bugs do lote de cobertura no formato de `cobertura.bugs`, com as execuções (só os `chaves`, se dadas)."""
    bid = cb.batch_id(home)
    if bid is None:
        return []
    if chaves is None:
        return cb.bugs(home, bid)
    alvo = list(dict.fromkeys(chaves))
    with contextlib.closing(jq._connect(home)) as con:
        marks = ",".join("?" * len(alvo))
        jobs = [dict(r) for r in con.execute(
            f"SELECT * FROM jobs WHERE batch_id=? AND bug_key IN ({marks}) ORDER BY idx", (bid, *alvo))]
    paths = list({j["run_rel"] for j in jobs if j["state"] == "done" and j["run_rel"]})
    rows: dict[str, dict] = {}
    if paths:
        with contextlib.closing(store._connect(home)) as con:
            marks = ",".join("?" * len(paths))
            rows = {r["path"]: dict(r) for r in con.execute(f"SELECT * FROM runs WHERE path IN ({marks})", paths)}
    por_bug: dict[str, dict] = {}
    for j in jobs:
        partes = j["bug_key"].split("/", 2)
        b = por_bug.setdefault(j["bug_key"], {
            "key": j["bug_key"], "projeto": partes[1] if len(partes) > 2 else "", "bug": partes[-1],
            "etapas": {}, "jobs": {}, "origem": None})
        b["etapas"][j["kind"]] = cb.etapa(j, cb._run_of(home, j, rows, True))
        b["jobs"][j["kind"]] = j
        if b["origem"] is None and j.get("origem"):
            b["origem"] = j["origem"]
    ativas = cb.kinds_ativos(home)
    for b in por_bug.values():
        b["situacao"] = cb.situacao(b["etapas"], ativas)
    return list(por_bug.values())


def _ler(home: Home, chaves: list[str] | None) -> dict[str, tuple[str, str, str]]:
    """{chave: (linha, atualizado, assinatura)} das linhas já na caixa de saída (só as pedidas; None = todas).

    Deve ser lido ANTES de tirar o retrato do estado dos bugs: assim quem gravar entre as duas leituras é detectado."""
    out = {}
    with contextlib.closing(_con(home)) as con:
        if chaves is None:
            for r in con.execute("SELECT bug_key, linha, atualizado, assinatura FROM planilha_outbox"):
                out[r[0]] = (r[1], r[2], r[3])
            return out
        for i in range(0, len(chaves), 500):
            bloco = chaves[i:i + 500]
            marks = ",".join("?" * len(bloco))
            for r in con.execute(f"SELECT bug_key, linha, atualizado, assinatura FROM planilha_outbox "
                                 f"WHERE bug_key IN ({marks})", bloco):
                out[r[0]] = (r[1], r[2], r[3])
    return out


def _gravar(home: Home, bugs: list[dict], ant: dict | None = None, tentativas: int = 3) -> int:
    """Monta as linhas SEM transação de escrita aberta e grava em transações curtas (blocos de 100).

    Anti-sobrescrita: cada gravação só vale se `atualizado` ainda é o que foi lido antes de montar a linha; se outro
    escritor mexeu no meio, o bug é remontado a partir do estado atual (na última tentativa grava de qualquer jeito).
    `ant` é a leitura da caixa de saída feita ANTES do retrato dos `bugs`; sem ela, lê aqui."""
    pend = {b["key"]: b for b in bugs}
    n = 0
    for t in range(tentativas):
        if not pend:
            break
        ultima = t == tentativas - 1
        if ant is None:
            ant = _ler(home, list(pend))
        prontas = []
        for chave, bug in pend.items():
            try:
                anterior = json.loads(ant[chave][0])[2] if chave in ant else None
            except (ValueError, IndexError, TypeError):
                anterior = None
            try:
                row = linha(home, bug, resumo=anterior or None)
            except Exception as e:      # noqa: BLE001 - uma linha impossível de montar não trava as outras
                _registrar_falha(home, chave, e)
                continue
            _registrar_falha(home, chave, None)
            prontas.append((chave, json.dumps(row, ensure_ascii=False), assinatura(bug)))
        conflitos = []
        agora = datetime.now().isoformat()
        for i in range(0, len(prontas), LOTE):
            with contextlib.closing(_con(home)) as con, con:
                for j, (chave, texto, ass) in enumerate(prontas[i:i + LOTE]):
                    marca, velho = f"{agora}#{i + j:06d}", ant.get(chave, (None, None, None))[1]
                    if velho is None:
                        cur = con.execute("INSERT OR IGNORE INTO planilha_outbox(bug_key, linha, atualizado, enviado, "
                                          "assinatura) VALUES(?,?,?,NULL,?)", (chave, texto, marca, ass))
                    elif ultima:
                        cur = con.execute("UPDATE planilha_outbox SET linha=?, atualizado=?, enviado=NULL, "
                                          "assinatura=? WHERE bug_key=?", (texto, marca, ass, chave))
                    else:
                        cur = con.execute("UPDATE planilha_outbox SET linha=?, atualizado=?, enviado=NULL, "
                                          "assinatura=? WHERE bug_key=? AND atualizado=?",
                                          (texto, marca, ass, chave, velho))
                    if cur.rowcount:
                        n += 1
                    else:
                        conflitos.append(chave)
        ant = _ler(home, conflitos) if conflitos else None      # primeiro a caixa de saída, depois o retrato
        pend = {b["key"]: b for b in _bugs_do_lote(home, conflitos)} if conflitos else {}
    return n


def enfileirar(home: Home, bug_keys: list[str]) -> None:
    """Monta a linha de cada bug e a põe (ou atualiza) na caixa de saída como não enviada."""
    if bug_keys:
        chaves = list(bug_keys)
        ant = _ler(home, chaves)
        _gravar(home, _bugs_do_lote(home, chaves), ant)


def _em_blocos(home: Home, bugs: list[dict], ant: dict) -> int:
    return sum(_gravar(home, bugs[i:i + LOTE], ant) for i in range(0, len(bugs), LOTE))


def enfileirar_todos(home: Home) -> int:
    """Reenvio completo: todas as linhas do lote de cobertura voltam à caixa de saída. Devolve quantas."""
    ant = _ler(home, None)
    return _em_blocos(home, _bugs_do_lote(home, None), ant)


def reconciliar(home: Home) -> int:
    """Enfileira os bugs que ainda não estão na caixa de saída ou cujo estado (situação, etapas, origem) mudou
    desde a última linha gravada: cobre a carga inicial (todos os bugs, os não rodados como pendente) e mudanças
    feitas fora dos workers (importação, refazer, cancelar)."""
    ant = _ler(home, None)
    lista = _bugs_do_lote(home, None)
    return _em_blocos(home, [b for b in lista if (ant.get(b["key"]) or (None, None, None))[2] != assinatura(b)], ant)


def pendentes(home: Home) -> int:
    with contextlib.closing(_con(home)) as con:
        return con.execute("SELECT COUNT(*) FROM planilha_outbox WHERE enviado IS NULL").fetchone()[0]


def linhas_total(home: Home) -> int:
    with contextlib.closing(_con(home)) as con:
        return con.execute("SELECT COUNT(*) FROM planilha_outbox").fetchone()[0]


# ------------------------------------------------------------------ configuração

def garantir_token(settings: Settings) -> str:
    """Gera (e grava no config.toml) o token se estiver vazio, com ou sem URL: o passo a passo manda colá-lo no
    Apps Script antes de implantar. Devolve o token."""
    token = settings.get("planilha", "token", "") or ""
    if not token:
        token = secrets.token_urlsafe(24)
        settings.set("planilha", "token", token)
        settings.save()
    return token


def salvar_url(settings: Settings, url: str) -> None:
    """Grava a URL do app da web; na primeira vez gera o token."""
    settings.set("planilha", "url", (url or "").strip())
    garantir_token(settings)
    settings.save()


# ------------------------------------------------------------------ envio

def post_http(url: str, body: dict) -> dict:
    """POST JSON ao app da web do Apps Script (segue o 302 para googleusercontent.com); exige resposta JSON."""
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "text/plain; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        texto = r.read().decode("utf-8", "replace")
    try:
        resp = json.loads(texto)
    except ValueError:
        raise ValueError("a planilha respondeu com uma página em vez de JSON "
                         "(confira a URL e se o acesso do app da web é \"qualquer pessoa\")") from None
    if not isinstance(resp, dict):
        raise ValueError("resposta inesperada da planilha")
    return resp


class Enviador(threading.Thread):
    """Manda em segundo plano as linhas pendentes da caixa de saída; falha nunca perde linha nem derruba o app.

    Ao começar (com a URL configurada) e a cada `reconciliar_cada` segundos, enfileira os bugs que faltam ou mudaram."""

    def __init__(self, home: Home, settings: Settings, post=None, *, espera_inicial: float = ESPERA_INICIAL_S,
                 espera_maxima: float = ESPERA_MAXIMA_S, espera_ociosa: float = ESPERA_OCIOSA_S,
                 reconciliar_cada: float | None = RECONCILIAR_S, relogio=time.monotonic):
        super().__init__(name="jflp-planilha", daemon=True)
        self.home, self.settings = home, settings
        self.post = post or post_http
        self._inicial, self._maxima, self._ociosa = espera_inicial, espera_maxima, espera_ociosa
        self._recon_cada, self._relogio = reconciliar_cada, relogio
        self._espera = espera_inicial
        self._proximo = 0.0         # antes disso (relógio monotônico) não reenvia, salvo `acordar(forcar=True)`
        self._prox_recon = 0.0
        self._forcar = False
        self._ev = threading.Event()
        self._parar = threading.Event()
        self._lock = threading.Lock()
        self._ultimo_ok: str | None = None
        self._erro: str | None = None
        self._erro_recon: str | None = None

    def acordar(self, forcar: bool = False) -> None:
        """Manda agora. Em espera por falha, só `forcar=True` (ex.: "Reenviar tudo") ignora o prazo da espera."""
        if forcar:
            self._forcar = True
        self._ev.set()

    def stop(self) -> None:
        self._parar.set()
        self._ev.set()

    def status(self) -> dict:
        try:
            n = pendentes(self.home)
        except sqlite3.Error:
            n = 0
        f = falhas_linha(self.home)
        with self._lock:
            return {"ultimo_ok": self._ultimo_ok, "pendentes": n, "erro": self._erro,
                    "configurada": bool(self.settings.get("planilha", "url", "")),
                    "falhas_linha": f["n"], "erro_linha": self._erro_recon or f["erro"]}

    def _reconciliar(self, agora: float) -> None:
        if not self._recon_cada or agora < self._prox_recon:
            return
        self._prox_recon = agora + self._recon_cada
        try:
            reconciliar(self.home)
            erro = None
        except Exception as e:      # noqa: BLE001 - não impede o envio do que já está na fila
            erro = (str(e) or repr(e))[-300:]
            _log.warning("reconciliação da planilha falhou: %r", e)
        with self._lock:
            self._erro_recon = erro

    def ciclo(self) -> float:
        """Um passo de envio. Devolve quantos segundos esperar antes do próximo (0 = há mais para mandar)."""
        url = (self.settings.get("planilha", "url", "") or "").strip()
        if not url:
            return self._ociosa
        agora = self._relogio()
        forcar, self._forcar = self._forcar, False
        if not forcar and agora < self._proximo:
            return self._proximo - agora
        self._reconciliar(agora)
        try:
            token = garantir_token(self.settings)
            with contextlib.closing(_con(self.home)) as con:
                rows = con.execute("SELECT bug_key, linha, atualizado FROM planilha_outbox WHERE enviado IS NULL "
                                   "ORDER BY atualizado LIMIT ?", (LOTE,)).fetchall()
            if not rows:
                return self._ociosa
            body = {"token": token, "colunas": COLUNAS,
                    "linhas": [[r["bug_key"], *json.loads(r["linha"])] for r in rows]}
            resp = self.post(url, body)
            if not isinstance(resp, dict) or not resp.get("ok"):
                erro = resp.get("erro") if isinstance(resp, dict) else None
                raise RuntimeError("a planilha recusou o envio" + (f" ({erro})" if erro else ""))
        except Exception as e:      # noqa: BLE001 - qualquer falha mantém a fila e tenta de novo mais tarde
            with self._lock:
                self._erro = (str(e) or repr(e))[-300:]
                espera, self._espera = self._espera, min(self._espera * 2, self._maxima)
                self._proximo = self._relogio() + espera
            return espera
        agora_iso = datetime.now().isoformat(timespec="seconds")
        with contextlib.closing(_con(self.home)) as con, con:
            for r in rows:      # só marca se a linha não mudou durante o envio
                con.execute("UPDATE planilha_outbox SET enviado=? WHERE bug_key=? AND atualizado=?",
                            (agora_iso, r["bug_key"], r["atualizado"]))
        with self._lock:
            self._ultimo_ok, self._erro, self._espera, self._proximo = agora_iso, None, self._inicial, 0.0
        if resp.get("cabecalho_mudou"):     # a planilha trocou o cabeçalho: as linhas antigas ficaram desalinhadas
            try:
                enfileirar_todos(self.home)
                self._forcar = True
                return 0
            except Exception as e:      # noqa: BLE001
                _log.warning("reenvio após mudança de cabeçalho falhou: %r", e)
        return 0 if len(rows) >= LOTE else self._ociosa

    def run(self) -> None:
        try:
            garantir_token(self.settings)       # o token precisa existir antes da URL (vai no Apps Script)
        except Exception as e:      # noqa: BLE001
            _log.warning("não foi possível gravar o token da planilha: %r", e)
        while not self._parar.is_set():
            self._ev.clear()
            try:
                espera = self.ciclo()
            except Exception as e:      # noqa: BLE001 - a thread não morre
                with self._lock:
                    self._erro = repr(e)[-300:]
                espera = self._inicial
            if espera > 0:
                self._ev.wait(espera)
