from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sbfl import cobertura as cb
from sbfl import queue as jq
from sbfl.home import Home, load_settings

from bugsjar_fixture import make_bugsjar_git


class Base(unittest.TestCase):
    _molde: Path | None = None

    @classmethod
    def _criar_molde(cls) -> Path:
        """Coleção com 2 projetos x 2 bugs, criada uma vez (git é lento) e copiada a cada teste."""
        if Base._molde is None:
            base = Path(tempfile.mkdtemp())
            unittest.addModuleCleanup(shutil.rmtree, base, True)
            (base / "colecao").mkdir()
            for nome, ids in (("maven", ("A-1", "A-2")), ("outro", ("B-1", "B-2"))):
                repo = make_bugsjar_git(base / ("src_" + nome), ids=ids)
                shutil.move(str(repo), str(base / "colecao" / nome))
            Base._molde = base / "colecao"
        return Base._molde

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Home(self.tmp / "home").ensure()
        self.settings = load_settings(self.home)
        self.root = self.tmp / "colecao"
        shutil.copytree(self._criar_molde(), self.root)

    def sync(self):
        return cb.sincronizar(self.home, self.settings, self.root)

    def jobs(self, bid):
        return jq.batch_jobs(self.home, bid)

    def sql(self, q, *a):
        with contextlib.closing(jq._connect(self.home)) as con, con:
            return con.execute(q, a)


class TestSincronizar(Base):
    def test_sincronizar_cria_lote_e_jobs(self):
        r = self.sync()
        self.assertEqual((r["adicionados"], r["total_bugs"], r["avisos"]), (12, 4, []))
        self.assertEqual(cb.batch_id(self.home), r["batch_id"])
        b = jq.get_batch(self.home, r["batch_id"])
        self.assertEqual(b["name"], "Cobertura bugs-dot-jar")
        self.assertEqual(json.loads(b["kinds"]), list(cb.KINDS))
        self.assertEqual(len(self.jobs(r["batch_id"])), 12)
        with contextlib.closing(jq._connect(self.home)) as con:
            row = con.execute("SELECT options, meta FROM batches WHERE id=?", (r["batch_id"],)).fetchone()
        self.assertEqual(json.loads(row["options"]), {
            "test_scope": "all", "auto_search": True, "archive_checkout": False,
            "params": {"cobertura": True, "max_attempts": 8}})
        self.assertEqual(json.loads(row["meta"]), {"cobertura": True, "root": str(self.root)})

    def test_sincronizar_idempotente(self):
        self.sync()
        r = self.sync()
        self.assertEqual(r["adicionados"], 0)
        self.assertEqual(len(self.jobs(r["batch_id"])), 12)

    def test_ordem_alterna_projetos(self):
        r = self.sync()
        chaves = [j["bug_key"] for j in self.jobs(r["batch_id"])]
        blocos = [chaves[i] for i in range(0, 12, 3)]
        self.assertEqual([k.split("/")[1] for k in blocos], ["maven", "outro", "maven", "outro"])
        for i in range(0, 12, 3):
            self.assertEqual(len(set(chaves[i:i + 3])), 1)
        self.assertEqual([j["kind"] for j in self.jobs(r["batch_id"])][:3], list(cb.KINDS))

    def test_sincronizar_repo_quebrado(self):
        quebrado = self.root / "quebrado"
        (quebrado / ".bugs-dot-jar").mkdir(parents=True)
        r = self.sync()
        self.assertTrue(r["avisos"])
        self.assertEqual(r["adicionados"], 12)

    def test_novos_vao_para_o_fim(self):
        r = self.sync()
        mx = max(j["idx"] for j in self.jobs(r["batch_id"]))
        extra = make_bugsjar_git(self.tmp / "src_z", ids=("Z-1",))
        shutil.move(str(extra), str(self.root / "zeta"))
        r2 = self.sync()
        self.assertEqual(r2["adicionados"], 3)
        novos = [j for j in self.jobs(r["batch_id"]) if j["idx"] > mx]
        self.assertEqual(len(novos), 3)


class TestRegras(unittest.TestCase):
    def test_etapa_regras(self):
        def job(state="done", kind="jaguar1", forcado=None):
            return {"state": state, "kind": kind, "forcado": forcado}
        legado = {"status": "compilacao_falhou", "attempt_configs": [{"jdk": 8}, {"jdk": 7}]}
        casos = [
            (job("running"), None, "rodando"),
            (job("pending"), None, "pendente"),
            (job("pending", forcado="nao_executavel"), None, "nao_executavel"),
            (job(forcado="nao_executavel"), {"status": "ok"}, "nao_executavel"),
            (job(), {"status": "ok"}, "ok"),
            (job(), {"status": "ok_testes_falhando"}, "ok"),
            (job(kind="maven"), {"status": "sem_teste_falhando"}, "ok"),
            (job(kind="jaguar1"), {"status": "sem_teste_falhando"}, "falhou"),
            (job(), {"status": "projeto_invalido"}, "nao_executavel"),
            (job(), legado, "nao_executavel"),
            (job(), {"status": "compilacao_falhou", "attempt_configs": [{"jdk": 8}]}, "falhou"),
            (job(), None, "falhou"),
        ]
        for j, run, esperado in casos:
            self.assertEqual(cb.etapa(j, run), esperado, (j, run))

    def test_situacao_regras(self):
        s = cb.situacao
        self.assertEqual(s({"a": "rodando", "b": "ok"}), "rodando")
        self.assertEqual(s({"a": "ok", "b": "ok"}), "ok")
        self.assertEqual(s({"a": "pendente", "b": "pendente"}), "pendente")
        self.assertEqual(s({"a": "ok", "b": "falhou"}), "parcial")
        self.assertEqual(s({"a": "ok", "b": "pendente"}), "parcial")
        self.assertEqual(s({"a": "nao_executavel", "b": "nao_executavel"}), "nao_executavel")
        self.assertEqual(s({"a": "pendente", "b": "falhou"}), "pendente")
        self.assertEqual(s({"a": "falhou", "b": "nao_executavel"}), "falhou")
        self.assertEqual(s({"a": "falhou", "b": "falhou"}), "falhou")


class TestAcoes(Base):
    def setUp(self):
        super().setUp()
        self.bid = self.sync()["batch_id"]
        self.js = self.jobs(self.bid)
        self.k0 = self.js[0]["bug_key"]
        self.k1 = self.js[3]["bug_key"]

    def finish(self, key, kinds_status, secs=10.0, ago_h=0.0):
        """Conclui os jobs do bug `key`: {kind: status} grava runs e marca done."""
        from sbfl.store import _connect as sc
        for j in self.jobs(self.bid):
            if j["bug_key"] != key or j["kind"] not in kinds_status:
                continue
            rel = f"r/{j['id']}"
            ini = (datetime.now() - timedelta(hours=ago_h, seconds=secs)).isoformat(timespec="seconds")
            with contextlib.closing(sc(self.home)) as con, con:
                con.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (rel, "bugsjar", "p", "b", j["kind"], kinds_status[j["kind"]], ini, secs, 1, 1, None))
            self.sql("UPDATE jobs SET state='done', run_rel=?, seconds=? WHERE id=?", rel, secs, j["id"])

    def test_refazer_so_falhas(self):
        self.finish(self.k0, {"maven": "ok", "jaguar1": "compilacao_falhou", "jaguar2": "ok"})
        self.sql("UPDATE jobs SET error='x' WHERE bug_key=?", self.k0)
        n = cb.refazer(self.home, self.bid)
        self.assertEqual(n, 1)
        por = {j["kind"]: j for j in self.jobs(self.bid) if j["bug_key"] == self.k0}
        self.assertEqual(por["maven"]["state"], "done")
        self.assertEqual(por["jaguar2"]["state"], "done")
        self.assertEqual(por["jaguar1"]["state"], "pending")
        self.assertIsNone(por["jaguar1"]["run_rel"])
        self.assertIsNone(por["jaguar1"]["error"])
        self.assertIsNone(por["jaguar1"]["seconds"])

    def test_refazer_nao_mexe_em_rodando_e_tudo(self):
        self.finish(self.k0, {"maven": "ok", "jaguar1": "compilacao_falhou", "jaguar2": "ok"})
        self.sql("UPDATE jobs SET state='running' WHERE bug_key=? AND kind='jaguar1'", self.k0)
        self.assertEqual(cb.refazer(self.home, self.bid, [self.k0], so_falhas=False), 2)
        por = {j["kind"]: j["state"] for j in self.jobs(self.bid) if j["bug_key"] == self.k0}
        self.assertEqual(por, {"maven": "pending", "jaguar1": "running", "jaguar2": "pending"})

    def test_marcar_e_priorizar(self):
        self.assertEqual(cb.marcar_nao_executavel(self.home, self.bid, [self.k0]), 3)
        for j in self.jobs(self.bid):
            if j["bug_key"] == self.k0:
                self.assertEqual((j["state"], j["forcado"]), ("done", "nao_executavel"))
        mn = min(j["idx"] for j in self.jobs(self.bid) if j["state"] == "pending")
        self.assertEqual(cb.priorizar(self.home, self.bid, [self.k1]), 3)
        pend = [j for j in self.jobs(self.bid) if j["state"] == "pending"]
        primeiros = sorted(pend, key=lambda j: j["idx"])[:3]
        self.assertEqual({j["bug_key"] for j in primeiros}, {self.k1})
        self.assertEqual([j["kind"] for j in primeiros], list(cb.KINDS))
        self.assertLess(primeiros[0]["idx"], mn)

    def test_bugs_e_filtros(self):
        self.finish(self.k0, {"maven": "ok", "jaguar1": "ok", "jaguar2": "ok"})
        self.finish(self.k1, {"maven": "ok", "jaguar1": "compilacao_falhou"})
        todos = cb.bugs(self.home, self.bid)
        self.assertEqual(len(todos), 4)
        por = {b["key"]: b for b in todos}
        self.assertEqual(por[self.k0]["situacao"], "ok")
        self.assertEqual(por[self.k1]["situacao"], "parcial")
        self.assertEqual(por[self.k1]["etapas"]["jaguar2"], "pendente")
        self.assertEqual(por[self.k0]["projeto"], self.k0.split("/")[1])
        self.assertEqual([b["key"] for b in cb.bugs(self.home, self.bid, situacoes=["parcial"])], [self.k1])
        self.assertEqual(len(cb.bugs(self.home, self.bid, projeto="maven")), 2)
        self.assertEqual([b["key"] for b in cb.bugs(self.home, self.bid, busca=self.k0.split("/")[2].upper())],
                         [self.k0])

    def test_resumo_contagens(self):
        self.finish(self.k0, {"maven": "ok", "jaguar1": "ok", "jaguar2": "ok"}, ago_h=1)
        self.finish(self.k1, {"maven": "ok", "jaguar1": "compilacao_falhou", "jaguar2": "ok"}, ago_h=2)
        r = cb.resumo(self.home, self.bid)
        self.assertEqual(r["total"], 4)
        self.assertEqual(r["concluidos"], 2)
        self.assertEqual(r["por_situacao"]["ok"], 1)
        self.assertEqual(r["por_situacao"]["parcial"], 1)
        self.assertEqual(r["por_situacao"]["pendente"], 2)
        proj = {p["projeto"]: p for p in r["por_projeto"]}
        self.assertEqual(sum(p["total"] for p in proj.values()), 4)
        self.assertEqual(sum(p["ok"] for p in proj.values()), 1)
        self.assertIsNotNone(r["ritmo_h"])
        self.assertGreater(r["restante_s"], 0)

    def test_resumo_conta_legado_jdk7_como_nao_executavel(self):
        from sbfl.store import run_dir
        self.finish(self.k0, {k: "compilacao_falhou" for k in cb.KINDS})
        for j in self.jobs(self.bid):
            if j["bug_key"] == self.k0:
                d = run_dir(self.home, j["run_rel"])
                d.mkdir(parents=True, exist_ok=True)
                (d / "run.json").write_text(json.dumps({"status": "compilacao_falhou",
                                                        "attempt_configs": [{"jdk": 8}, {"jdk": 7}]}),
                                            encoding="utf-8")
        b = {x["key"]: x for x in cb.bugs(self.home, self.bid)}[self.k0]
        self.assertEqual(b["situacao"], "nao_executavel")
        r = cb.resumo(self.home, self.bid)
        self.assertEqual(r["por_situacao"]["nao_executavel"], 1)
        self.assertEqual(r["por_situacao"]["falhou"], 0)

    def test_refazer_requeue_cancelados_e_reativa_lote(self):
        self.sql("UPDATE jobs SET state='cancelled' WHERE bug_key=?", self.k0)
        self.sql("UPDATE batches SET state='cancelled' WHERE id=?", self.bid)
        self.assertEqual(cb.refazer(self.home, self.bid, [self.k0]), 3)
        self.assertEqual({j["state"] for j in self.jobs(self.bid) if j["bug_key"] == self.k0}, {"pending"})
        self.assertEqual(jq.get_batch(self.home, self.bid)["state"], "active")

    def test_refazer_reativa_lote_concluido(self):
        self.finish(self.k0, {"maven": "compilacao_falhou"})
        self.sql("UPDATE batches SET state='done' WHERE id=?", self.bid)
        self.assertEqual(cb.refazer(self.home, self.bid, [self.k0]), 1)
        self.assertEqual(jq.get_batch(self.home, self.bid)["state"], "active")

    def test_batch_id_com_varios_lotes(self):
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("INSERT INTO batches(name, kinds, options, state, meta) VALUES('x','[]','{}','active',NULL)")
            novo = con.execute("INSERT INTO batches(name, kinds, options, state, meta) "
                               "VALUES('y','[]','{}','active','{\"cobertura\": true}')").lastrowid
            con.execute("INSERT INTO batches(name, kinds, options, state, meta) VALUES('z','[]','{}','active','{}')")
        self.assertEqual(cb.batch_id(self.home), novo)

    def test_resumo_sem_historico(self):
        r = cb.resumo(self.home, self.bid)
        self.assertIsNone(r["ritmo_h"])
        self.assertIsNone(r["restante_s"])


class TestPainel(Base):
    def test_painel_uma_leitura_rodando_e_ultimos(self):
        bid = self.sync()["batch_id"]
        jobs = self.jobs(bid)
        k0, k1 = jobs[0]["bug_key"], next(j["bug_key"] for j in jobs if j["bug_key"] != jobs[0]["bug_key"])
        self.sql("UPDATE jobs SET state='done' WHERE batch_id=? AND bug_key=?", bid, k0)
        self.sql("UPDATE jobs SET state='running' WHERE batch_id=? AND bug_key=? AND kind='maven'", bid, k1)
        p = cb.painel(self.home, bid, n_ultimos=3)
        self.assertEqual(p["total"], 4)
        self.assertEqual(list(p["rodando"]), [k1])
        self.assertEqual(p["rodando"][k1]["etapas"]["maven"], "rodando")
        self.assertEqual([u["key"] for u in p["ultimos"]], [k0])
        self.assertIsNone(p["ultimos"][0]["fim"])
        self.assertEqual(p["concluidos"], cb.resumo(self.home, bid)["concluidos"])


if __name__ == "__main__":
    unittest.main()
