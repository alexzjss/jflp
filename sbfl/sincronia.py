"""Sincronização entre computadores pela planilha (Apps Script como hub, protocolo versão 2).

Cada computador sobe o pacote (zip) das execuções que produziu, puxa os pacotes dos outros e os importa como execuções
normais (`origem: "remoto"`), e reserva cada bug antes de rodá-lo para que dois computadores não façam o mesmo trabalho.
Sem rede, sem URL ou com o Apps Script antigo, o app roda como antes (a sincronia só se desliga)."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import logging
import os
import secrets
import shutil
import socket
import sqlite3
import stat
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import cobertura as cb
from . import planilha
from . import queue as jq
from . import store
from .adapters.base import Bug
from .home import Home, Settings

TAMANHO_PARTE = 8 * 1024 * 1024         # bytes por pedaço do pacote (≈ 11 MiB em base64)
PARTES_POR_CICLO = 4                    # pedaços enviados por ciclo do Sincronizador
TENTATIVAS_OBSOLETO = 5                 # o hub diz "obsoleto" (talvez a linha ainda não chegou): desiste depois disso
ESPERA_INICIAL_S = 30
ESPERA_MAXIMA_S = 900
ESPERA_OCIOSA_S = 60
EXCLUIDOS = ("outputs/jacoco.exec", ".writer.json")
AVISO_ANTIGO = ("O Apps Script da planilha é antigo: atualize-o (Implantar > Gerenciar implantações > Nova versão) "
                "para sincronizar entre computadores.")

_log = logging.getLogger("jflp.sincronia")


class HubAntigo(RuntimeError):
    """O Apps Script não conhece o protocolo versão 2 (responde `formato`)."""


class PacoteInvalido(ValueError):
    """Pacote corrompido ou malicioso (sha256 diferente, caminho fora da pasta, link simbólico, sem run.json)."""


# ------------------------------------------------------------------ hub

class Hub:
    """Cliente do protocolo v2. `post(url, body) -> dict` é injetável (testes)."""

    def __init__(self, url: str, token: str, post=None):
        self.url, self.token = url, token
        self.post = post or planilha.post_http

    def _chamar(self, acao: str, **campos) -> dict:
        resp = self.post(self.url, {"token": self.token, "acao": acao, **campos})
        if not isinstance(resp, dict):
            raise RuntimeError("resposta inesperada da planilha")
        try:
            versao = int(resp.get("versao") or 0)
        except (TypeError, ValueError):
            versao = 0
        if versao < 2 and resp.get("erro") == "formato":
            raise HubAntigo(AVISO_ANTIGO)
        if not resp.get("ok"):
            erro = resp.get("erro")
            raise RuntimeError("a planilha recusou o pedido" + (f" ({erro})" if erro else ""))
        return resp

    def puxar(self, maquina: str, cursor: int, limite: int | None = None) -> dict:
        extra = {"limite": limite} if limite else {}
        return self._chamar("puxar", maquina=maquina, cursor=cursor, **extra)

    def pacote(self, maquina: str, chave: str, kind: str, run_rel: str, sha256: str, parte: int, partes: int,
               dados: bytes) -> dict:
        return self._chamar("pacote", maquina=maquina, chave=chave, kind=kind, run_rel=run_rel, sha256=sha256,
                            parte=parte, partes=partes, dados_b64=base64.b64encode(dados).decode("ascii"))

    def baixar(self, id_: str) -> bytes:
        return base64.b64decode(self._chamar("baixar", id=id_).get("dados_b64") or "")

    def reservar(self, maquina: str, nome: str, chaves: list[str], ttl_s: int) -> dict:
        return self._chamar("reservar", maquina=maquina, maquina_nome=nome, chaves=list(chaves), ttl_s=int(ttl_s))

    def liberar(self, maquina: str, chaves: list[str]) -> dict:
        return self._chamar("liberar", maquina=maquina, chaves=list(chaves))


# ------------------------------------------------------------------ identidade e configuração

def maquina(settings: Settings) -> tuple[str, str]:
    """(id, nome) deste computador. O id é gerado uma vez e gravado no config.toml; nome vazio = hostname."""
    mid = str(settings.get("planilha", "maquina_id", "") or "").strip()
    if not mid:
        mid = secrets.token_hex(8)
        settings.set("planilha", "maquina_id", mid)
        settings.save()
    nome = str(settings.get("planilha", "maquina_nome", "") or "").strip() or socket.gethostname() or mid
    return mid, nome


def ativa(settings: Settings) -> bool:
    """Há URL e `sincronizar` está ligado."""
    return bool((settings.get("planilha", "url", "") or "").strip()) and bool(settings.get("planilha", "sincronizar", True))


def _num(settings: Settings, chave: str, padrao: float) -> float:
    try:
        v = float(settings.get("planilha", chave, padrao))
        return v if v > 0 else padrao
    except (TypeError, ValueError):
        return padrao


def _hub_de(settings: Settings, post=None) -> Hub | None:
    url = (settings.get("planilha", "url", "") or "").strip()
    if not url or not settings.get("planilha", "sincronizar", True):
        return None
    return Hub(url, planilha.garantir_token(settings), post)


# ------------------------------------------------------------------ banco

def _con(home: Home) -> sqlite3.Connection:
    con = jq._connect(home)
    con.execute("CREATE TABLE IF NOT EXISTS sync_estado(chave TEXT PRIMARY KEY, valor TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS sync_pacotes(run_rel TEXT PRIMARY KEY, bug_key TEXT, kind TEXT, "
                "sha256 TEXT, partes INT, enviadas INT DEFAULT 0, enviado TEXT, erro TEXT)")
    if "remoto_ate" not in {r[1] for r in con.execute("PRAGMA table_info(jobs)")}:
        try:
            con.execute("ALTER TABLE jobs ADD COLUMN remoto_ate REAL")
        except sqlite3.OperationalError:    # outra conexão migrou primeiro
            pass
    return con


def _estado_ler(home: Home, chave: str, padrao: str | None = None) -> str | None:
    with contextlib.closing(_con(home)) as con:
        r = con.execute("SELECT valor FROM sync_estado WHERE chave=?", (chave,)).fetchone()
    return r[0] if r else padrao


def _estado_gravar(home: Home, chave: str, valor: str) -> None:
    with contextlib.closing(_con(home)) as con, con:
        con.execute("INSERT OR REPLACE INTO sync_estado(chave, valor) VALUES(?,?)", (chave, valor))


def cursor(home: Home, url: str) -> int:
    try:
        return int(_estado_ler(home, f"cursor:{url}", "0") or 0)
    except ValueError:
        return 0


def registrar_pacote(home: Home, bug_key: str, kind: str, run_rel: str) -> None:
    """Põe a execução local concluída na fila de upload de pacotes (idempotente). O planilha.py chama isto para
    toda etapa que entra no `sync`; o upload só acontece com a sincronia ligada."""
    with contextlib.closing(_con(home)) as con, con:
        con.execute("INSERT OR IGNORE INTO sync_pacotes(run_rel, bug_key, kind, enviadas) VALUES(?,?,?,0)",
                    (run_rel, bug_key, kind))


def pacotes_pendentes(home: Home) -> int:
    try:
        with contextlib.closing(_con(home)) as con:
            return con.execute("SELECT COUNT(*) FROM sync_pacotes WHERE enviado IS NULL").fetchone()[0]
    except sqlite3.Error:
        return 0


# ------------------------------------------------------------------ pacote (zip)

def montar_pacote(home: Home, run_rel: str) -> bytes:
    """Zip (deflate) da pasta da execução, sem `outputs/jacoco.exec` nem `.writer.json`. Determinístico (ordem e
    datas vêm dos arquivos, imutáveis), o que permite retomar um upload interrompido."""
    raiz = store.run_dir(home, run_rel)
    if not raiz.is_dir():
        raise FileNotFoundError(f"execução não encontrada: {run_rel}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
        for atual, dirs, arqs in os.walk(raiz):
            dirs.sort()
            for nome in sorted(arqs):
                f = Path(atual) / nome
                rel = f.relative_to(raiz).as_posix()
                if rel in EXCLUIDOS or f.is_symlink() or not f.is_file():
                    continue
                zf.write(f, rel)
    return buf.getvalue()


def _caminho_seguro(nome: str) -> bool:
    if not nome or "\\" in nome or nome.startswith("/") or PureWindowsPath(nome).anchor or PureWindowsPath(nome).drive:
        return False
    return ".." not in PurePosixPath(nome).parts and "\x00" not in nome


def _extrair(zf: zipfile.ZipFile, destino: Path) -> None:
    for info in zf.infolist():
        if not _caminho_seguro(info.filename):
            raise PacoteInvalido(f"caminho inválido no pacote: {info.filename!r}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise PacoteInvalido(f"link simbólico no pacote: {info.filename!r}")
        alvo = destino.joinpath(*PurePosixPath(info.filename).parts)
        try:
            alvo.resolve().relative_to(destino.resolve())
        except ValueError:
            raise PacoteInvalido(f"caminho fora da pasta: {info.filename!r}") from None
        if info.is_dir():
            alvo.mkdir(parents=True, exist_ok=True)
            continue
        alvo.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(alvo, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _bug_do_job(job: dict) -> Bug:
    benchmark, project, bug_id = job["bug_key"].split("/", 2)
    try:
        extra = json.loads(job.get("bug_extra") or "{}")
    except ValueError:
        extra = {}
    return Bug(benchmark, project, bug_id, extra)


def aplicar_pacote(home: Home, job: dict, dados_zip: bytes, origem_info: dict) -> str:
    """Extrai o pacote num temporário (recusa caminhos absolutos, `..` e links) e grava uma execução normal do `job`
    com `origem: "remoto"` e `remoto_de: origem_info`. Devolve o `run_rel` novo. `PacoteInvalido` se o zip é ruim."""
    home.root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sync-", dir=home.root) as tmp:
        tmp = Path(tmp)
        try:
            with zipfile.ZipFile(io.BytesIO(dados_zip)) as zf:
                _extrair(zf, tmp)
        except zipfile.BadZipFile as e:
            raise PacoteInvalido(f"zip ilegível: {e}") from None
        try:
            run = json.loads((tmp / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise PacoteInvalido("pacote sem run.json válido") from None
        if not isinstance(run, dict):
            raise PacoteInvalido("run.json inesperado")
        w = store.RunWriter(home, _bug_do_job(job), job["kind"])
        try:
            for f in sorted(tmp.rglob("*")):
                rel = f.relative_to(tmp).as_posix()
                if f.is_file() and rel != "run.json":
                    w.copy_into(f, rel)
            w.finish({**run, "origem": "remoto", "remoto_de": dict(origem_info)})
        except BaseException:
            store.delete_run(home, w.rel)
            raise
        return w.rel


# ------------------------------------------------------------------ upload

def _agora_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fechar(home: Home, run_rel: str, erro: str | None = None) -> None:
    with contextlib.closing(_con(home)) as con, con:
        con.execute("UPDATE sync_pacotes SET enviado=?, erro=? WHERE run_rel=?", (_agora_iso(), erro, run_rel))


def subir_pacotes(home: Home, hub: Hub, mid: str, max_partes: int | None = None) -> int:
    """Sobe pedaços dos pacotes pendentes (um POST por pedaço, `max_partes` no máximo; None = tudo). Retomável:
    `enviadas` é gravado a cada pedaço. Devolve quantos pedaços foram enviados. Erro de rede propaga."""
    with contextlib.closing(_con(home)) as con:
        fila = [dict(r) for r in con.execute("SELECT * FROM sync_pacotes WHERE enviado IS NULL ORDER BY rowid")]
    enviados = 0
    for p in fila:
        if max_partes is not None and enviados >= max_partes:
            break
        try:
            dados = montar_pacote(home, p["run_rel"])
        except FileNotFoundError:
            _fechar(home, p["run_rel"], "execução apagada")
            continue
        sha = hashlib.sha256(dados).hexdigest()
        tam = max(int(TAMANHO_PARTE), 1)
        partes = max(-(-len(dados) // tam), 1)
        enviadas = int(p["enviadas"] or 0)
        if p["sha256"] != sha or p["partes"] != partes:    # primeira vez, ou o zip mudou: recomeça
            enviadas = 0
            with contextlib.closing(_con(home)) as con, con:
                con.execute("UPDATE sync_pacotes SET sha256=?, partes=?, enviadas=0 WHERE run_rel=?",
                            (sha, partes, p["run_rel"]))
        while enviadas < partes:
            if max_partes is not None and enviados >= max_partes:
                break
            resp = hub.pacote(mid, p["bug_key"], p["kind"], p["run_rel"], sha, enviadas, partes,
                              dados[enviadas * tam:(enviadas + 1) * tam])
            enviados += 1
            enviadas += 1
            with contextlib.closing(_con(home)) as con, con:
                con.execute("UPDATE sync_pacotes SET enviadas=? WHERE run_rel=?", (enviadas, p["run_rel"]))
            if enviadas < partes:
                continue
            if resp.get("completo") and not resp.get("obsoleto"):
                _fechar(home, p["run_rel"])
                break
            # último pedaço aceito, mas o hub não ligou o pacote à linha (a linha pode ainda não ter chegado):
            # reenvia só o último pedaço nas próximas vezes; depois de algumas, desiste
            ant = str(p["erro"] or "")
            n = int(ant.split()[1]) + 1 if ant.startswith("obsoleto ") and ant.split()[1].isdigit() else 1
            if n >= TENTATIVAS_OBSOLETO:
                _fechar(home, p["run_rel"], "obsoleto")
            else:
                with contextlib.closing(_con(home)) as con, con:
                    con.execute("UPDATE sync_pacotes SET enviadas=?, erro=? WHERE run_rel=?",
                                (partes - 1, f"obsoleto {n}", p["run_rel"]))
                p["erro"] = f"obsoleto {n}"
            break
    return enviados


# ------------------------------------------------------------------ aplicar mudanças remotas

def _jobs_do_lote(home: Home, chaves: list[str]) -> dict[tuple[str, str], dict]:
    bid = cb.batch_id(home)
    if bid is None or not chaves:
        return {}
    with contextlib.closing(_con(home)) as con:
        marks = ",".join("?" * len(chaves))
        return {(r["bug_key"], r["kind"]): dict(r) for r in con.execute(
            f"SELECT * FROM jobs WHERE batch_id=? AND bug_key IN ({marks})", (bid, *chaves))}


def _baixar_pacote(hub: Hub, pacote: dict) -> bytes:
    dados = b"".join(hub.baixar(i) for i in pacote.get("ids") or [])
    if hashlib.sha256(dados).hexdigest() != pacote.get("sha256"):
        raise PacoteInvalido("sha256 do pacote não confere")
    return dados


def _publicar(home: Home, job: dict, rel: str, segundos, *, livre: bool) -> bool:
    """Marca o job com a execução remota; se o estado mudou depois do instantâneo, descarta a execução gravada."""
    with contextlib.closing(_con(home)) as con, con:
        if livre:
            cur = con.execute("UPDATE jobs SET state='done', run_rel=?, error=NULL, seconds=?, origem='remoto' "
                              "WHERE id=? AND state IN ('pending','cancelled') AND run_rel IS NULL",
                              (rel, segundos, job["id"]))
        else:
            cur = con.execute("UPDATE jobs SET state='done', run_rel=?, error=NULL, seconds=?, origem='remoto' "
                              "WHERE id=? AND state IN ('done','cancelled') AND run_rel=?",
                              (rel, segundos, job["id"], job["run_rel"]))
        ok = cur.rowcount > 0
        if ok:
            jq._close_if_finished(con, job["batch_id"])
    if not ok:
        store.delete_run(home, rel)
    return ok


def _segundos(home: Home, rel: str):
    try:
        return store.load_run(home, rel).get("seconds")
    except (OSError, ValueError):
        return None


def aplicar_mudancas(home: Home, settings: Settings, hub: Hub, mudancas: list[dict]) -> dict:
    """Aplica as etapas com pacote completo produzidas por outras máquinas.

    Job livre (pendente/cancelado, sem execução) recebe a execução; já concluído localmente só é trocado se o local
    `falhou` e o remoto é `ok`; `running` não é tocado. Devolve {"aplicadas", "mantidas", "recusadas": [texto]}.
    Erros de rede propagam (a puxada inteira é repetida); pacote inválido só é recusado e anotado."""
    mid, _nome = maquina(settings)
    res = {"aplicadas": 0, "mantidas": 0, "recusadas": []}
    jobs = _jobs_do_lote(home, list({m["chave"] for m in mudancas if m.get("chave")}))
    tocadas = []
    for m in mudancas:
        chave = m.get("chave")
        for kind, info in (m.get("dados") or {}).items():
            if not isinstance(info, dict) or not info.get("pacote") or info.get("maquina") == mid:
                continue
            job = jobs.get((chave, kind))
            if job is None:
                continue
            # estado atual (o instantâneo pode ter envelhecido entre uma etapa e outra)
            with contextlib.closing(_con(home)) as con:
                r = con.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            job = dict(r) if r else job
            if job["state"] == "running":
                res["mantidas"] += 1
                continue
            livre = job["state"] in ("pending", "cancelled") and not job.get("run_rel")
            if not livre:
                local = cb.etapa(job, cb._run_of(home, job, {}, True)) if job["state"] == "done" else cb.SIT_FALHOU
                if not (local == cb.SIT_FALHOU and info.get("estado") == cb.SIT_OK and job.get("run_rel")):
                    res["mantidas"] += 1
                    continue
            origem = {"maquina": info.get("maquina"), "maquina_nome": info.get("maquina_nome"),
                      "run_rel": info.get("run_rel")}
            try:
                rel = aplicar_pacote(home, job, _baixar_pacote(hub, info["pacote"]), origem)
            except PacoteInvalido as e:
                res["recusadas"].append(f"{chave} ({kind}): {e}")
                _log.warning("pacote recusado %s %s: %s", chave, kind, e)
                continue
            if _publicar(home, job, rel, _segundos(home, rel), livre=livre):
                res["aplicadas"] += 1
                tocadas.append(chave)
            else:
                res["mantidas"] += 1
    if tocadas:
        try:
            planilha.enfileirar(home, list(dict.fromkeys(tocadas)))
        except Exception as e:      # noqa: BLE001 - a linha da planilha é refeita na próxima reconciliação
            _log.warning("não foi possível enfileirar as linhas recebidas: %r", e)
    return res


# ------------------------------------------------------------------ reservas

def atualizar_remoto_ate(home: Home, reservas: dict, agora: float | None = None) -> int:
    """Grava `jobs.remoto_ate` (epoch s) dos bugs reservados por outras máquinas e zera os que deixaram de estar.
    Devolve quantos bugs estão reservados por outros."""
    agora = time.time() if agora is None else agora
    bid = cb.batch_id(home)
    if bid is None:
        return 0
    vivas = {}
    for chave, r in (reservas or {}).items():
        try:
            vivas[chave] = agora + float(r.get("expira_em_s") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    with contextlib.closing(_con(home)) as con, con:
        for chave, ate in vivas.items():
            con.execute("UPDATE jobs SET remoto_ate=? WHERE batch_id=? AND bug_key=?", (ate, bid, chave))
        marks = ",".join("?" * len(vivas))
        con.execute("UPDATE jobs SET remoto_ate=NULL WHERE batch_id=? AND remoto_ate IS NOT NULL"
                    + (f" AND bug_key NOT IN ({marks})" if vivas else ""), (bid, *vivas))
    return len(vivas)


class Reservas:
    """Reservas de bugs deste computador no hub. `hub` pode ser um `Hub`, um chamável que devolve `Hub | None`
    (reavaliado a cada uso: a URL pode mudar) ou None (sem sincronia: tudo é concedido).

    `reservar(chave)` é o gancho do worker: rede fora do ar, hub antigo ou erro ⇒ True (roda como antes)."""

    def __init__(self, hub, settings: Settings):
        self._hub, self.settings = hub, settings
        self._lock = threading.Lock()
        self.minhas: set[str] = set()
        self.negadas: dict[str, dict] = {}       # chave -> {maquina, maquina_nome, expira_em_s} da última negativa
        self.antigo = False

    def _h(self) -> Hub | None:
        h = self._hub() if callable(self._hub) and not isinstance(self._hub, Hub) else self._hub
        return None if self.antigo else h

    def _ttl(self) -> int:
        return int(_num(self.settings, "reserva_ttl_s", 1800))

    def reservar(self, chave: str) -> bool:
        hub = self._h()
        if hub is None or not ativa(self.settings):
            return True
        try:
            mid, nome = maquina(self.settings)
            resp = hub.reservar(mid, nome, [chave], self._ttl())
        except HubAntigo:
            self.antigo = True
            return True
        except Exception as e:      # noqa: BLE001 - sem rede (ou erro do hub): degrada para rodar
            _log.info("reserva de %s indisponível, rodando mesmo assim: %r", chave, e)
            return True
        with self._lock:
            if chave in (resp.get("negadas") or {}):
                self.negadas[chave] = resp["negadas"][chave]
                return False
            self.negadas.pop(chave, None)
            self.minhas.add(chave)
        return True

    __call__ = reservar

    def restante_s(self, chave: str) -> float | None:
        """Segundos que faltam da reserva de outra máquina que negou `chave` (None = não negada)."""
        with self._lock:
            n = self.negadas.get(chave)
        try:
            return float(n["expira_em_s"]) if n else None
        except (KeyError, TypeError, ValueError):
            return None

    def renovar_e_liberar(self, home: Home) -> dict:
        """Renova as reservas dos bugs com job `running` local; libera as que não têm mais job `running` nem
        `pending`. Erros de rede propagam. Devolve {"renovadas": n, "liberadas": n}."""
        hub = self._h()
        res = {"renovadas": 0, "liberadas": 0}
        with self._lock:
            minhas = set(self.minhas)
        bid = cb.batch_id(home)
        if hub is None or not minhas or bid is None:
            return res
        with contextlib.closing(_con(home)) as con:
            estados: dict[str, set] = {}
            for r in con.execute("SELECT bug_key, state FROM jobs WHERE batch_id=?", (bid,)):
                estados.setdefault(r[0], set()).add(r[1])
        rodando = sorted(c for c in minhas if "running" in estados.get(c, ()))
        soltar = sorted(c for c in minhas if not estados.get(c, set()) & {"running", "pending"})
        mid, nome = maquina(self.settings)
        if rodando:
            resp = hub.reservar(mid, nome, rodando, self._ttl())
            res["renovadas"] = len(resp.get("concedidas") or [])
            with self._lock:
                for c in resp.get("negadas") or {}:     # outro computador assumiu (a reserva venceu): não é mais minha
                    self.minhas.discard(c)
        if soltar:
            hub.liberar(mid, soltar)
            res["liberadas"] = len(soltar)
            with self._lock:
                self.minhas.difference_update(soltar)
        return res


# ------------------------------------------------------------------ um ciclo de puxar

def puxar_e_aplicar(home: Home, settings: Settings, hub: Hub, agora=time.time) -> dict:
    """Puxa em laço enquanto houver `mais`, aplica as mudanças, grava o cursor (por URL) e atualiza `remoto_ate`.
    Devolve {"aplicadas", "mantidas", "recusadas", "reservas_remotas", "cursor"}."""
    mid, _nome = maquina(settings)
    cur = cursor(home, hub.url)
    total = {"aplicadas": 0, "mantidas": 0, "recusadas": [], "reservas_remotas": 0, "cursor": cur}
    reservas: dict = {}
    while True:
        resp = hub.puxar(mid, cur)
        reservas = resp.get("reservas") or {}
        r = aplicar_mudancas(home, settings, hub, resp.get("mudancas") or [])
        total["aplicadas"] += r["aplicadas"]
        total["mantidas"] += r["mantidas"]
        total["recusadas"] += r["recusadas"]
        try:
            novo = int(resp.get("cursor", cur))
        except (TypeError, ValueError):
            novo = cur
        if novo > cur:
            cur = novo
            _estado_gravar(home, f"cursor:{hub.url}", str(cur))
        if not resp.get("mais") or not (resp.get("mudancas") or []):
            break
    total["cursor"] = cur
    total["reservas_remotas"] = atualizar_remoto_ate(home, reservas, agora())
    return total


def sincronizar_uma_vez(home: Home, settings: Settings, post=None, agora=time.time) -> dict:
    """Um ciclo completo, sem esperas: sobe todos os pacotes pendentes, puxa até acabar, aplica e renova/libera as
    reservas. Devolve {"pedacos_enviados", "pacotes_pendentes", "aplicadas", "mantidas", "recusadas",
    "reservas_remotas", "cursor", "renovadas", "liberadas"}. `RuntimeError` sem URL/sincronia; erros do hub propagam
    (`HubAntigo` inclusive)."""
    hub = _hub_de(settings, post)
    if hub is None:
        raise RuntimeError("a sincronização está desligada ou a URL da planilha não foi configurada")
    mid, _nome = maquina(settings)
    enviados = subir_pacotes(home, hub, mid)
    res = puxar_e_aplicar(home, settings, hub, agora)
    r = Reservas(hub, settings).renovar_e_liberar(home)
    return {"pedacos_enviados": enviados, "pacotes_pendentes": pacotes_pendentes(home), **res, **r}


# ------------------------------------------------------------------ thread

class Sincronizador(threading.Thread):
    """Sobe pacotes, puxa e aplica execuções de outros computadores e mantém as reservas, em segundo plano.

    Mesma robustez do `planilha.Enviador`: falha nunca derruba a thread, espera exponencial entre falhas. Com o Apps
    Script antigo (`HubAntigo`) a sincronia se desliga nesta sessão (`status()["hub_antigo"]`)."""

    def __init__(self, home: Home, settings: Settings, post=None, *, espera_inicial: float = ESPERA_INICIAL_S,
                 espera_maxima: float = ESPERA_MAXIMA_S, espera_ociosa: float = ESPERA_OCIOSA_S,
                 relogio=time.monotonic, agora=time.time):
        super().__init__(name="jflp-sincronia", daemon=True)
        self.home, self.settings, self.post = home, settings, post
        self._inicial, self._maxima, self._ociosa = espera_inicial, espera_maxima, espera_ociosa
        self._relogio, self._agora = relogio, agora
        self._espera = espera_inicial
        self._proximo = 0.0         # antes disso (relógio monotônico) não tenta de novo, salvo `acordar(forcar=True)`
        self._prox_puxar = 0.0
        self._prox_reservas = 0.0
        self._forcar = False
        self._ev = threading.Event()
        self._parar = threading.Event()
        self._lock = threading.Lock()
        self._ultimo_puxar: str | None = None
        self._aplicadas = 0
        self._recusadas: list[str] = []
        self._reservas_remotas = 0
        self._erro: str | None = None
        self._antigo = False
        self._hub: Hub | None = None
        self.reservas = Reservas(self._hub_atual, settings)

    @property
    def reservar_remoto(self):
        """Gancho para o worker (`bug_key -> bool`)."""
        return self.reservas.reservar

    def _hub_atual(self) -> Hub | None:
        """O hub das configurações de agora (reconstruído se a URL ou o token mudaram)."""
        if self._antigo:
            return None
        url = (self.settings.get("planilha", "url", "") or "").strip()
        if not url or not self.settings.get("planilha", "sincronizar", True):
            return None
        token = self.settings.get("planilha", "token", "") or ""
        h = self._hub
        if h is None or (h.url, h.token) != (url, token):
            h = self._hub = Hub(url, planilha.garantir_token(self.settings), self.post)
        return h

    def acordar(self, forcar: bool = False) -> None:
        """Sincroniza agora. `forcar=True` (ex.: "Sincronizar agora") ignora a espera por falha e o intervalo."""
        if forcar:
            self._forcar = True
        self._ev.set()

    def stop(self) -> None:
        self._parar.set()
        self._ev.set()

    def status(self) -> dict:
        with self._lock:
            return {"ultimo_puxar": self._ultimo_puxar, "aplicadas": self._aplicadas,
                    "pacotes_pendentes": pacotes_pendentes(self.home), "reservas_remotas": self._reservas_remotas,
                    "erro": self._erro, "hub_antigo": self._antigo, "recusadas": list(self._recusadas)}

    def ciclo(self) -> float:
        """Um passo. Devolve quantos segundos esperar antes do próximo (0 = há mais pedaços para subir)."""
        if self._antigo or not ativa(self.settings):
            return self._ociosa
        agora = self._relogio()
        forcar, self._forcar = self._forcar, False
        if not forcar and agora < self._proximo:
            return self._proximo - agora
        try:
            hub = self._hub_atual()
            if hub is None:
                return self._ociosa
            mid, _nome = maquina(self.settings)
            subir_pacotes(self.home, hub, mid, PARTES_POR_CICLO)
            if forcar or agora >= self._prox_puxar:
                res = puxar_e_aplicar(self.home, self.settings, hub, self._agora)
                with self._lock:
                    self._ultimo_puxar = datetime.now().isoformat(timespec="seconds")
                    self._aplicadas += res["aplicadas"]
                    self._reservas_remotas = res["reservas_remotas"]
                    if res["recusadas"]:
                        self._recusadas = (self._recusadas + res["recusadas"])[-20:]
                self._prox_puxar = self._relogio() + _num(self.settings, "puxar_cada_s", 120)
            ttl = _num(self.settings, "reserva_ttl_s", 1800)
            if forcar or agora >= self._prox_reservas:
                self.reservas.renovar_e_liberar(self.home)
                self._prox_reservas = self._relogio() + ttl / 3
        except HubAntigo:
            with self._lock:
                self._antigo, self._erro = True, AVISO_ANTIGO
            return self._ociosa
        except Exception as e:      # noqa: BLE001 - qualquer falha mantém a fila e tenta de novo mais tarde
            with self._lock:
                self._erro = (str(e) or repr(e))[-300:]
                espera, self._espera = self._espera, min(self._espera * 2, self._maxima)
                self._proximo = self._relogio() + espera
            return espera
        with self._lock:
            self._erro, self._espera, self._proximo = None, self._inicial, 0.0
        if pacotes_pendentes(self.home):
            return 0
        agora = self._relogio()
        return max(min(self._prox_puxar, self._prox_reservas) - agora, 1.0)

    def run(self) -> None:
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
