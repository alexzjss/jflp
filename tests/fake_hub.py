"""Hub falso do protocolo v2 (docs/superpowers/specs/2026-11-05-jflp-sincronia-design.md), em memória.

Usado como `post(url, body) -> dict`. A aba "Bugs" só guarda as linhas recebidas; `_sync`, pacotes, `puxar`,
reservas e `baixar` seguem o protocolo. `antigo=True` imita um Apps Script sem as ações novas; `rede_caida=True`
levanta `OSError` em toda chamada; `falhar_apos=n` derruba a rede depois de `n` chamadas bem-sucedidas."""
from __future__ import annotations

import base64
import time


class FakeHub:
    def __init__(self, relogio=None, token: str | None = None):
        self.relogio = relogio or time.time      # segundos; injetável para vencer reservas
        self.token = token
        self.seq = 0
        self.sync: dict[str, dict] = {}          # chave -> {"seq", "dados": {kind: {...}}}
        self.linhas: dict[str, list] = {}        # chave -> linha da aba "Bugs"
        self.pedacos: dict[tuple[str, int], str] = {}   # (sha256, parte) -> id do arquivo
        self.arquivos: dict[str, bytes] = {}
        self.reservas: dict[str, dict] = {}      # chave -> {maquina, maquina_nome, expira}
        self.limite_max: int | None = None       # teto de linhas por `puxar` (testa a paginação)
        self.antigo = False
        self.rede_caida = False
        self.falhar_apos: int | None = None
        self.chamadas: list[dict] = []           # corpos recebidos (com `acao` preenchida)

    # ------------------------------------------------------------ entrada
    def __call__(self, url: str, body: dict) -> dict:
        if self.rede_caida:
            raise OSError("rede caída (FakeHub)")
        if self.falhar_apos is not None:
            if self.falhar_apos <= 0:
                raise OSError("rede caiu (FakeHub)")
            self.falhar_apos -= 1
        self.chamadas.append(dict(body))
        if self.token is not None and body.get("token") != self.token:
            return {"ok": False, "erro": "token", "versao": 2}
        acao = body.get("acao")
        if self.antigo:
            if acao is None:        # o app antigo só entendia o envio de linhas
                self._guardar_linhas(body)
                return {"ok": True, "n": len(body.get("linhas") or [])}
            return {"ok": False, "erro": "formato"}
        fn = getattr(self, "_" + (acao or "linhas"), None)
        if fn is None:
            return {"ok": False, "erro": "acao", "versao": 2}
        return {"versao": 2, **fn(body)}

    def n_chamadas(self, acao: str) -> int:
        return sum(1 for c in self.chamadas if (c.get("acao") or "linhas") == acao)

    # ------------------------------------------------------------ linhas
    def _guardar_linhas(self, b: dict) -> None:
        for lin in b.get("linhas") or []:
            self.linhas[lin[0]] = lin

    def _linhas(self, b: dict) -> dict:
        self._guardar_linhas(b)
        for chave, kinds in (b.get("sync") or {}).items():
            reg = self.sync.setdefault(chave, {"seq": 0, "dados": {}})
            mudou = False
            for kind, e in kinds.items():
                atual = reg["dados"].get(kind)
                if atual and atual["run_rel"] == e["run_rel"] and atual["revisao"] == e["revisao"]:
                    continue        # sem eco: nada muda, nada de seq novo
                reg["dados"][kind] = {"estado": e["estado"], "maquina": b.get("maquina"),
                                      "maquina_nome": b.get("maquina_nome"), "revisao": e["revisao"],
                                      "run_rel": e["run_rel"], "pacote": None}
                mudou = True
            if mudou:
                self._bump(reg)
        return {"ok": True, "n": len(b.get("linhas") or [])}

    def _bump(self, reg: dict) -> None:
        self.seq += 1
        reg["seq"] = self.seq

    # ------------------------------------------------------------ pacote / baixar
    def _pacote(self, b: dict) -> dict:
        sha, parte, partes = b["sha256"], int(b["parte"]), int(b["partes"])
        if (sha, parte) not in self.pedacos:
            fid = f"{sha[:8]}.parte{parte}"
            self.arquivos[fid] = base64.b64decode(b["dados_b64"])
            self.pedacos[(sha, parte)] = fid
        if not all((sha, i) in self.pedacos for i in range(partes)):
            return {"ok": True, "completo": False}
        ids = [self.pedacos[(sha, i)] for i in range(partes)]
        reg = self.sync.get(b["chave"])
        e = (reg or {"dados": {}})["dados"].get(b["kind"])
        if not e or e["run_rel"] != b["run_rel"] or e["maquina"] != b["maquina"]:
            return {"ok": True, "completo": True, "obsoleto": True}
        e["pacote"] = {"sha256": sha, "partes": partes, "ids": ids, "bytes": sum(len(self.arquivos[i]) for i in ids)}
        self._bump(reg)
        return {"ok": True, "completo": True}

    def _baixar(self, b: dict) -> dict:
        if b["id"] not in self.arquivos:
            return {"ok": False, "erro": "id"}
        return {"ok": True, "dados_b64": base64.b64encode(self.arquivos[b["id"]]).decode("ascii")}

    # ------------------------------------------------------------ puxar
    def _vivas(self) -> dict:
        agora = self.relogio()
        self.reservas = {k: v for k, v in self.reservas.items() if v["expira"] > agora}
        return self.reservas

    def _puxar(self, b: dict) -> dict:
        cur = int(b.get("cursor") or 0)
        limite = min(int(b.get("limite") or 200), 500)
        if self.limite_max:
            limite = min(limite, self.limite_max)
        todos = sorted((r["seq"], c, r) for c, r in self.sync.items() if r["seq"] > cur)
        pagina = todos[:limite]
        agora = self.relogio()
        reservas = {c: {"maquina": r["maquina"], "maquina_nome": r["maquina_nome"],
                        "expira_em_s": round(r["expira"] - agora, 3)}
                    for c, r in self._vivas().items() if r["maquina"] != b.get("maquina")}
        return {"ok": True, "cursor": pagina[-1][0] if pagina else cur, "mais": len(todos) > len(pagina),
                "mudancas": [{"chave": c, "seq": s, "dados": r["dados"]} for s, c, r in pagina],
                "reservas": reservas}

    # ------------------------------------------------------------ reservas
    def _reservar(self, b: dict) -> dict:
        agora = self.relogio()
        vivas = self._vivas()
        concedidas, negadas = [], {}
        for c in b.get("chaves") or []:
            r = vivas.get(c)
            if r and r["maquina"] != b["maquina"]:
                negadas[c] = {"maquina": r["maquina"], "maquina_nome": r["maquina_nome"],
                              "expira_em_s": round(r["expira"] - agora, 3)}
            else:
                vivas[c] = {"maquina": b["maquina"], "maquina_nome": b.get("maquina_nome"),
                            "expira": agora + float(b.get("ttl_s") or 1800)}
                concedidas.append(c)
        return {"ok": True, "concedidas": concedidas, "negadas": negadas}

    def _liberar(self, b: dict) -> dict:
        n = 0
        for c in b.get("chaves") or []:
            if self.reservas.get(c, {}).get("maquina") == b["maquina"]:
                del self.reservas[c]
                n += 1
        return {"ok": True, "n": n}
