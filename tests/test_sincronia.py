from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from sbfl import cobertura as cb
from sbfl import compare, store
from sbfl import queue as jq
from sbfl import sincronia as sc
from sbfl.adapters.base import Bug
from sbfl.home import Home, load_settings
from tests.fake_hub import FakeHub

FIX = Path(__file__).parent / "fixtures" / "importar" / "raiz" / "commons-math" / "bugs-dot-jar_MATH-1005_91d280b7"
URL = "https://hub.example/exec"
BUGS = [("commons-math", "bugs-dot-jar_MATH-1005_91d280b7"), ("maven", "bugs-dot-jar_MNG-1797_5d99b35c"),
        ("maven", "bugs-dot-jar_MNG-1798_aaaaaaaa")]


def chave(i=0) -> str:
    return f"bugsjar/{BUGS[i][0]}/{BUGS[i][1]}"


def wait_for(cond, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class Maq:
    """Um computador: Home + Settings + lote de cobertura."""

    def __init__(self, base: Path, nome: str):
        self.home = Home(base / nome).ensure()
        self.settings = load_settings(self.home)
        self.settings.set("planilha", "url", URL)
        self.settings.set("planilha", "token", "tok")
        self.settings.set("planilha", "maquina_nome", nome)
        self.id, self.nome = sc.maquina(self.settings)
        with contextlib.closing(jq._connect(self.home)) as con, con:
            self.bid = con.execute(
                "INSERT INTO batches(name, created, kinds, options, state, meta) VALUES(?,?,?,?,?,?)",
                (cb.NOME_LOTE, "2026-10-30T10:00:00", json.dumps(list(cb.KINDS)), "{}", "active",
                 json.dumps({"cobertura": True}))).lastrowid
            idx = 0
            for proj, nome_bug in BUGS:
                for k in cb.KINDS:
                    con.execute("INSERT INTO jobs(batch_id, idx, bug_key, bug_extra, kind, state) "
                                "VALUES(?,?,?,?,?,'pending')",
                                (self.bid, idx, f"bugsjar/{proj}/{nome_bug}", "{}", k))
                    idx += 1

    def job(self, i=0, kind="jaguar1") -> dict:
        return next(j for j in jq.batch_jobs(self.home, self.bid) if j["bug_key"] == chave(i) and j["kind"] == kind)

    def concluir(self, i=0, kind="jaguar1", status="ok_testes_falhando", extra_arquivos=None) -> str:
        """Grava uma execução local e marca o job como concluído (como o worker faria)."""
        proj, nome = BUGS[i]
        w = store.RunWriter(self.home, Bug("bugsjar", proj, nome, {}), kind)
        w.copy_into(FIX / "control-flow.xml", "outputs/jaguar1.xml")
        w.write_text("outputs/jacoco.exec", "binario que não vai no pacote")
        w.write_text("outputs/sub/log.txt", "log")
        for rel, dados in (extra_arquivos or {}).items():
            w.write_text(rel, dados) if isinstance(dados, str) else w._dest(rel).write_bytes(dados)
        w.finish({"status": status, "tests": {"total": 3, "failed": 1, "error": 0}, "attempt_configs": []})
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='done', run_rel=?, seconds=1.5 WHERE id=?",
                        (w.rel, self.job(i, kind)["id"]))
        return w.rel

    def publicar(self, hub: FakeHub, i=0, kind="jaguar1", rel=None, upload=True) -> str:
        """Simula o Enviador (linhas + sync) e o upload do pacote."""
        rel = rel or self.job(i, kind)["run_rel"]
        run = store.load_run(self.home, rel)
        estado = cb.SIT_OK if run["status"].startswith("ok") else cb.SIT_FALHOU
        hub(URL, {"token": "tok", "colunas": [], "linhas": [[chave(i), "x"]], "maquina": self.id,
                  "maquina_nome": self.nome,
                  "sync": {chave(i): {kind: {"estado": estado, "revisao": run["finished"], "run_rel": rel}}}})
        sc.registrar_pacote(self.home, chave(i), kind, rel)
        if upload:
            sc.subir_pacotes(self.home, sc.Hub(URL, "tok", hub), self.id)
        return rel

    def sincronizar(self, hub: FakeHub) -> dict:
        return sc.sincronizar_uma_vez(self.home, self.settings, hub)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hub = FakeHub()
        self.a, self.b = Maq(self.tmp, "A"), Maq(self.tmp, "B")
        patcher = mock.patch("sbfl.planilha.enfileirar")
        self.enfileirar = patcher.start()
        self.addCleanup(patcher.stop)


def arquivos(pasta: Path) -> dict:
    return {p.relative_to(pasta).as_posix(): p.read_bytes() for p in sorted(pasta.rglob("*")) if p.is_file()}


class TestAplicar(Base):
    def test_execucao_completa(self):
        rel_a = self.a.concluir()
        self.a.publicar(self.hub)
        r = self.b.sincronizar(self.hub)
        self.assertEqual(r["aplicadas"], 1)
        j = self.b.job()
        self.assertEqual((j["state"], j["origem"]), ("done", "remoto"))
        self.assertEqual(j["seconds"], store.load_run(self.a.home, rel_a)["seconds"])
        run = store.load_run(self.b.home, j["run_rel"])
        self.assertEqual(run["origem"], "remoto")
        self.assertEqual(run["remoto_de"], {"maquina": self.a.id, "maquina_nome": "A", "run_rel": rel_a})
        saida_a = arquivos(store.run_dir(self.a.home, rel_a) / "outputs")
        saida_b = arquivos(store.run_dir(self.b.home, j["run_rel"]) / "outputs")
        saida_a.pop("jacoco.exec")
        self.assertEqual(saida_a, saida_b)
        sa, sb = compare.run_scores(self.a.home, rel_a), compare.run_scores(self.b.home, j["run_rel"])
        self.assertTrue(sa)
        self.assertEqual(sa, sb)
        self.assertEqual(store.load_run(self.b.home, j["run_rel"])["status"], "ok_testes_falhando")
        self.assertEqual(self.enfileirar.call_args.args[1], [chave()])
        # a execução entrou no índice e as outras etapas seguem livres
        self.assertEqual(self.b.job(0, "maven")["state"], "pending")
        # o pacote não leva jacoco.exec nem .writer.json
        nomes = zipfile.ZipFile(io.BytesIO(sc.montar_pacote(self.a.home, rel_a))).namelist()
        self.assertNotIn("outputs/jacoco.exec", nomes)
        self.assertIn("run.json", nomes)

    def test_segunda_puxada_nao_repete(self):
        self.a.concluir()
        self.a.publicar(self.hub)
        self.b.sincronizar(self.hub)
        n = self.hub.n_chamadas("baixar")
        r = self.b.sincronizar(self.hub)
        self.assertEqual((r["aplicadas"], self.hub.n_chamadas("baixar")), (0, n))
        self.assertEqual(len(store.list_runs(self.b.home)), 1)

    def test_sem_eco(self):
        rel = self.a.concluir()
        self.a.publicar(self.hub)
        seq = self.hub.seq
        self.assertEqual(seq, 2)       # linha (1) + pacote (2)
        self.a.publicar(self.hub, rel=rel)      # mesmo run_rel e revisão: nada muda
        self.assertEqual(self.hub.seq, seq)
        # A puxa a própria mudança: ignora (é dela), sem baixar nada
        r = self.a.sincronizar(self.hub)
        self.assertEqual((r["aplicadas"], r["mantidas"], self.hub.n_chamadas("baixar")), (0, 0, 0))
        self.assertEqual(len(store.list_runs(self.a.home)), 1)

    def test_sem_pacote_ainda_nao_aplica(self):
        self.a.concluir()
        self.a.publicar(self.hub, upload=False)
        r = self.b.sincronizar(self.hub)
        self.assertEqual(r["aplicadas"], 0)
        self.assertEqual(self.b.job()["state"], "pending")

    def test_bug_inexistente_localmente_e_ignorado(self):
        self.a.concluir()
        self.a.publicar(self.hub)
        with contextlib.closing(jq._connect(self.b.home)) as con, con:
            con.execute("DELETE FROM jobs WHERE bug_key=?", (chave(),))
        self.assertEqual(self.b.sincronizar(self.hub)["aplicadas"], 0)

    def test_fecha_lote_quando_acaba(self):
        with contextlib.closing(jq._connect(self.b.home)) as con, con:
            con.execute("UPDATE jobs SET state='cancelled' WHERE NOT (bug_key=? AND kind='jaguar1')", (chave(),))
        self.a.concluir()
        self.a.publicar(self.hub)
        self.b.sincronizar(self.hub)
        with contextlib.closing(jq._connect(self.b.home)) as con:
            self.assertEqual(con.execute("SELECT state FROM batches").fetchone()[0], "done")


class TestConflitos(Base):
    def test_local_ok_fica(self):
        self.b.concluir(status="ok_testes_falhando")
        rel_b = self.b.job()["run_rel"]
        self.a.concluir(status="ok")
        self.a.publicar(self.hub)
        r = self.b.sincronizar(self.hub)
        self.assertEqual((r["aplicadas"], r["mantidas"]), (0, 1))
        self.assertEqual(self.b.job()["run_rel"], rel_b)
        self.assertIsNone(self.b.job()["origem"])

    def test_local_falhou_remoto_ok_troca(self):
        rel_b = self.b.concluir(status="compilacao_falhou")
        self.a.concluir(status="ok")
        self.a.publicar(self.hub)
        r = self.b.sincronizar(self.hub)
        self.assertEqual(r["aplicadas"], 1)
        j = self.b.job()
        self.assertNotEqual(j["run_rel"], rel_b)
        self.assertEqual((j["state"], j["origem"]), ("done", "remoto"))
        self.assertEqual(store.load_run(self.b.home, j["run_rel"])["status"], "ok")

    def test_local_falhou_remoto_falhou_fica(self):
        rel_b = self.b.concluir(status="compilacao_falhou")
        self.a.concluir(status="compilacao_falhou")
        self.a.publicar(self.hub)
        self.assertEqual(self.b.sincronizar(self.hub)["aplicadas"], 0)
        self.assertEqual(self.b.job()["run_rel"], rel_b)

    def test_running_nao_mexe(self):
        with contextlib.closing(jq._connect(self.b.home)) as con, con:
            con.execute("UPDATE jobs SET state='running' WHERE bug_key=? AND kind='jaguar1'", (chave(),))
        self.a.concluir()
        self.a.publicar(self.hub)
        r = self.b.sincronizar(self.hub)
        self.assertEqual((r["aplicadas"], r["mantidas"]), (0, 1))
        self.assertEqual(self.b.job()["state"], "running")
        self.assertEqual(store.list_runs(self.b.home), [])

    def test_perdeu_a_corrida_descarta_execucao(self):
        self.a.concluir()
        self.a.publicar(self.hub)
        original = sc.aplicar_pacote

        def aplicar_e_roubar(home, job, dados, origem):
            rel = original(home, job, dados, origem)
            with contextlib.closing(jq._connect(home)) as con, con:     # um worker pegou o job no meio
                con.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
            return rel

        with mock.patch.object(sc, "aplicar_pacote", aplicar_e_roubar):
            r = self.b.sincronizar(self.hub)
        self.assertEqual((r["aplicadas"], r["mantidas"]), (0, 1))
        self.assertEqual(store.list_runs(self.b.home), [])


def zip_de(membros: dict, links: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for nome, dados in membros.items():
            zf.writestr(nome, dados)
        for nome, alvo in (links or {}).items():
            info = zipfile.ZipInfo(nome)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, alvo)
    return buf.getvalue()


class TestSeguranca(Base):
    RUN = json.dumps({"kind": "jaguar1", "status": "ok"})

    def aplicar(self, dados: bytes):
        return sc.aplicar_pacote(self.b.home, self.b.job(), dados, {"maquina": "x"})

    def test_caminhos_perigosos_recusados(self):
        for nome in ("../fora.txt", "outputs/../../fora.txt", "/etc/passwd", "C:/x.txt", "a\\..\\b.txt"):
            with self.subTest(nome=nome), self.assertRaises(sc.PacoteInvalido):
                self.aplicar(zip_de({"run.json": self.RUN, nome: "x"}))
        with self.assertRaises(sc.PacoteInvalido):
            self.aplicar(zip_de({"run.json": self.RUN}, {"outputs/link": "/etc/passwd"}))
        self.assertEqual(store.list_runs(self.b.home), [])
        self.assertFalse((self.b.home.root / "fora.txt").exists())

    def test_sem_run_json_e_zip_ruim(self):
        with self.assertRaises(sc.PacoteInvalido):
            self.aplicar(zip_de({"outputs/x": "1"}))
        with self.assertRaises(sc.PacoteInvalido):
            self.aplicar(b"isto nao e um zip")

    def test_pacote_malicioso_via_hub_e_recusado(self):
        mal = zip_de({"run.json": self.RUN, "../fora.txt": "x"})
        self.publicar_bruto(mal)
        r = self.b.sincronizar(self.hub)
        self.assertEqual(r["aplicadas"], 0)
        self.assertEqual(len(r["recusadas"]), 1)
        self.assertEqual(self.b.job()["state"], "pending")
        self.assertEqual(store.list_runs(self.b.home), [])
        self.assertEqual(r["cursor"], self.hub.seq)       # não trava a fila: o cursor avança

    def test_sha_errado_recusado(self):
        self.publicar_bruto(zip_de({"run.json": self.RUN}))
        for e in self.hub.sync[chave()]["dados"].values():
            e["pacote"]["sha256"] = "0" * 64
        r = self.b.sincronizar(self.hub)
        self.assertEqual((r["aplicadas"], len(r["recusadas"])), (0, 1))
        self.assertIn("sha256", r["recusadas"][0])
        self.assertEqual(store.list_runs(self.b.home), [])

    def publicar_bruto(self, dados: bytes):
        """Põe no hub um pacote arbitrário como se A o tivesse subido."""
        import base64
        import hashlib
        sha = hashlib.sha256(dados).hexdigest()
        self.hub(URL, {"token": "tok", "linhas": [], "maquina": self.a.id, "maquina_nome": "A",
                       "sync": {chave(): {"jaguar1": {"estado": "ok", "revisao": "r1", "run_rel": "x/y/z/1"}}}})
        self.hub(URL, {"token": "tok", "acao": "pacote", "maquina": self.a.id, "chave": chave(), "kind": "jaguar1",
                       "run_rel": "x/y/z/1", "sha256": sha, "parte": 0, "partes": 1,
                       "dados_b64": base64.b64encode(dados).decode()})


class TestHubAntigo(Base):
    def test_hub_levanta(self):
        self.hub.antigo = True
        with self.assertRaises(sc.HubAntigo):
            sc.Hub(URL, "tok", self.hub).puxar("m", 0)
        with self.assertRaises(sc.HubAntigo):
            self.b.sincronizar(self.hub)

    def test_erro_normal_nao_e_antigo(self):
        self.hub.token = "outro"
        with self.assertRaises(RuntimeError) as cm:
            sc.Hub(URL, "tok", self.hub).puxar("m", 0)
        self.assertNotIsInstance(cm.exception, sc.HubAntigo)

    def test_sincronizador_desliga_na_sessao(self):
        self.hub.antigo = True
        s = sc.Sincronizador(self.b.home, self.b.settings, self.hub)
        self.assertEqual(s.ciclo(), s._ociosa)
        self.assertTrue(s.status()["hub_antigo"])
        n = len(self.hub.chamadas)
        s.ciclo()
        self.assertEqual(len(self.hub.chamadas), n)        # não insiste
        self.assertTrue(s.reservar_remoto("qualquer"))      # reservas degradam para "pode rodar"


class TestReservas(Base):
    def test_concede_e_nega(self):
        ra, rb = (sc.Reservas(sc.Hub(URL, "tok", self.hub), m.settings) for m in (self.a, self.b))
        self.assertTrue(ra.reservar(chave()))
        self.assertFalse(rb.reservar(chave()))
        self.assertGreater(rb.restante_s(chave()), 0)
        self.assertTrue(rb.reservar(chave(1)))
        self.assertTrue(ra.reservar(chave()))       # renovar a própria é permitido

    def test_rede_caida_roda(self):
        self.hub.rede_caida = True
        r = sc.Reservas(sc.Hub(URL, "tok", self.hub), self.a.settings)
        self.assertTrue(r.reservar(chave()))
        self.assertEqual(r.minhas, set())

    def test_sem_hub_ou_desligada(self):
        self.assertTrue(sc.Reservas(None, self.a.settings).reservar(chave()))
        self.a.settings.set("planilha", "sincronizar", False)
        self.assertTrue(sc.Reservas(sc.Hub(URL, "tok", self.hub), self.a.settings).reservar(chave()))
        self.assertEqual(self.hub.chamadas, [])

    def test_reserva_vence(self):
        t = [1000.0]
        self.hub.relogio = lambda: t[0]
        ra, rb = (sc.Reservas(sc.Hub(URL, "tok", self.hub), m.settings) for m in (self.a, self.b))
        self.assertTrue(ra.reservar(chave()))
        t[0] += 1801
        self.assertTrue(rb.reservar(chave()))

    def test_renovar_e_liberar(self):
        r = sc.Reservas(sc.Hub(URL, "tok", self.hub), self.a.settings)
        r.reservar(chave(0))
        r.reservar(chave(1))
        with contextlib.closing(jq._connect(self.a.home)) as con, con:
            con.execute("UPDATE jobs SET state='running' WHERE bug_key=? AND kind='maven'", (chave(0),))
            con.execute("UPDATE jobs SET state='done' WHERE bug_key=?", (chave(1),))
        res = r.renovar_e_liberar(self.a.home)
        self.assertEqual(res, {"renovadas": 1, "liberadas": 1})
        self.assertEqual(r.minhas, {chave(0)})
        self.assertEqual(set(self.hub.reservas), {chave(0)})

    def test_remoto_ate_vem_do_puxar(self):
        t = [5000.0]
        self.hub.relogio = lambda: t[0]
        sc.Reservas(sc.Hub(URL, "tok", self.hub), self.a.settings).reservar(chave(1))
        r = sc.puxar_e_aplicar(self.b.home, self.b.settings, sc.Hub(URL, "tok", self.hub), agora=lambda: 100.0)
        self.assertEqual(r["reservas_remotas"], 1)
        with contextlib.closing(sc._con(self.b.home)) as con:
            vals = {(x["bug_key"], x["remoto_ate"]) for x in con.execute("SELECT bug_key, remoto_ate FROM jobs")}
        self.assertIn((chave(1), 1900.0), vals)
        self.assertEqual({v for k, v in vals if k != chave(1)}, {None})
        # A libera: no próximo puxar o bug volta a ficar livre
        self.hub.reservas.clear()
        sc.puxar_e_aplicar(self.b.home, self.b.settings, sc.Hub(URL, "tok", self.hub), agora=lambda: 100.0)
        with contextlib.closing(sc._con(self.b.home)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs WHERE remoto_ate IS NOT NULL").fetchone()[0], 0)

    def test_a_propria_reserva_nao_bloqueia(self):
        sc.Reservas(sc.Hub(URL, "tok", self.hub), self.a.settings).reservar(chave(1))
        self.assertEqual(sc.puxar_e_aplicar(self.a.home, self.a.settings, sc.Hub(URL, "tok", self.hub))
                         ["reservas_remotas"], 0)


class TestCursor(Base):
    def test_persiste_e_pagina(self):
        for i in range(3):
            self.a.concluir(i)
            self.a.publicar(self.hub, i)
        self.hub.limite_max = 2        # 6 mudanças de seq (linha + pacote por bug, mas 1 registro/bug)
        r = self.b.sincronizar(self.hub)
        self.assertEqual(r["aplicadas"], 3)
        self.assertGreaterEqual(self.hub.n_chamadas("puxar"), 2)
        self.assertEqual(sc.cursor(self.b.home, URL), self.hub.seq)
        # persistido por URL: um Hub novo retoma do cursor
        antes = len(self.hub.chamadas)
        self.b.sincronizar(self.hub)
        puxadas = [c for c in self.hub.chamadas[antes:] if c["acao"] == "puxar"]
        self.assertEqual([c["cursor"] for c in puxadas], [self.hub.seq])
        self.assertEqual(sc.cursor(self.b.home, "https://outro"), 0)

    def test_falha_no_meio_nao_avanca_o_cursor(self):
        self.a.concluir()
        self.a.publicar(self.hub)
        self.hub.falhar_apos = 1        # o puxar passa, o baixar cai
        with self.assertRaises(OSError):
            self.b.sincronizar(self.hub)
        self.assertEqual(sc.cursor(self.b.home, URL), 0)
        self.hub.falhar_apos = None
        self.assertEqual(self.b.sincronizar(self.hub)["aplicadas"], 1)


class TestUpload(Base):
    def test_pedacos_e_retomada(self):
        blob = os.urandom(5000)      # incompressível: o zip passa de 5 pedaços de 1000 bytes
        self.a.concluir(extra_arquivos={"outputs/grande.bin": blob})
        with mock.patch.object(sc, "TAMANHO_PARTE", 1000):
            self.a.publicar(self.hub, upload=False)
            hub_a = sc.Hub(URL, "tok", self.hub)
            self.hub.falhar_apos = 2
            with self.assertRaises(OSError):
                sc.subir_pacotes(self.a.home, hub_a, self.a.id)
            with contextlib.closing(sc._con(self.a.home)) as con:
                row = dict(con.execute("SELECT * FROM sync_pacotes").fetchone())
            self.assertEqual((row["enviadas"], row["enviado"]), (2, None))
            self.assertGreater(row["partes"], 5)
            self.hub.falhar_apos = None
            n = sc.subir_pacotes(self.a.home, hub_a, self.a.id)
            self.assertEqual(n, row["partes"] - 2)
            self.assertEqual(sc.pacotes_pendentes(self.a.home), 0)
            self.assertEqual(self.hub.n_chamadas("pacote"), row["partes"])       # nada reenviado
            self.assertTrue(all(len(self.hub.arquivos[i]) <= 1000 for i in self.hub.arquivos))
            self.assertEqual(sc.subir_pacotes(self.a.home, hub_a, self.a.id), 0)
            r = self.b.sincronizar(self.hub)
        self.assertEqual(r["aplicadas"], 1)
        rel = self.b.job()["run_rel"]
        self.assertEqual((store.run_dir(self.b.home, rel) / "outputs" / "grande.bin").read_bytes(), blob)

    def test_limite_de_pedacos_por_chamada(self):
        self.a.concluir(extra_arquivos={"outputs/grande.bin": os.urandom(5000)})
        with mock.patch.object(sc, "TAMANHO_PARTE", 1000):
            self.a.publicar(self.hub, upload=False)
            hub_a = sc.Hub(URL, "tok", self.hub)
            self.assertEqual(sc.subir_pacotes(self.a.home, hub_a, self.a.id, 2), 2)
            self.assertEqual(sc.pacotes_pendentes(self.a.home), 1)
            while sc.pacotes_pendentes(self.a.home):
                sc.subir_pacotes(self.a.home, hub_a, self.a.id, 2)

    def test_registrar_pacote_e_idempotente_e_execucao_apagada(self):
        rel = self.a.concluir()
        sc.registrar_pacote(self.a.home, chave(), "jaguar1", rel)
        sc.registrar_pacote(self.a.home, chave(), "jaguar1", rel)
        self.assertEqual(sc.pacotes_pendentes(self.a.home), 1)
        store.delete_run(self.a.home, rel)
        self.assertEqual(sc.subir_pacotes(self.a.home, sc.Hub(URL, "tok", self.hub), self.a.id), 0)
        self.assertEqual(sc.pacotes_pendentes(self.a.home), 0)

    def test_obsoleto_tenta_de_novo_e_desiste(self):
        rel = self.a.concluir()
        sc.registrar_pacote(self.a.home, chave(), "jaguar1", rel)      # sem a linha de sync no hub: obsoleto
        hub_a = sc.Hub(URL, "tok", self.hub)
        for _ in range(sc.TENTATIVAS_OBSOLETO - 1):
            sc.subir_pacotes(self.a.home, hub_a, self.a.id)
            self.assertEqual(sc.pacotes_pendentes(self.a.home), 1)
        sc.subir_pacotes(self.a.home, hub_a, self.a.id)
        self.assertEqual(sc.pacotes_pendentes(self.a.home), 0)
        # com a linha já no hub, o reenvio do último pedaço religa o pacote
        rel2 = self.a.concluir(i=1)
        sc.registrar_pacote(self.a.home, chave(1), "jaguar1", rel2)
        sc.subir_pacotes(self.a.home, hub_a, self.a.id)
        run = store.load_run(self.a.home, rel2)
        self.hub(URL, {"token": "tok", "linhas": [], "maquina": self.a.id, "maquina_nome": "A",
                       "sync": {chave(1): {"jaguar1": {"estado": "ok", "revisao": run["finished"], "run_rel": rel2}}}})
        sc.subir_pacotes(self.a.home, hub_a, self.a.id)
        self.assertEqual(sc.pacotes_pendentes(self.a.home), 0)
        self.assertIsNotNone(self.hub.sync[chave(1)]["dados"]["jaguar1"]["pacote"])


class TestMaquinaEConfig(Base):
    def test_maquina_gera_e_persiste(self):
        home = Home(self.tmp / "C").ensure()
        s = load_settings(home)
        mid, nome = sc.maquina(s)
        self.assertEqual(len(mid), 16)
        self.assertTrue(nome)
        s2 = load_settings(home)
        self.assertEqual(sc.maquina(s2), (mid, nome))
        s2.set("planilha", "maquina_nome", "lab-1")
        self.assertEqual(sc.maquina(s2), (mid, "lab-1"))

    def test_sem_url_levanta(self):
        s = load_settings(Home(self.tmp / "D").ensure())
        with self.assertRaises(RuntimeError):
            sc.sincronizar_uma_vez(s.home, s, self.hub)


class TestSincronizador(Base):
    def sinc(self, m, **kw):
        t = [0.0]
        s = sc.Sincronizador(m.home, m.settings, self.hub, relogio=lambda: t[0], agora=lambda: 1000.0, **kw)
        return s, t

    def test_ciclo_aplica_e_agenda(self):
        self.a.concluir()
        self.a.publicar(self.hub)
        s, t = self.sinc(self.b)
        espera = s.ciclo()
        st = s.status()
        self.assertEqual((st["aplicadas"], st["erro"], st["hub_antigo"]), (1, None, False))
        self.assertIsNotNone(st["ultimo_puxar"])
        self.assertEqual(self.b.job()["origem"], "remoto")
        self.assertEqual(espera, 120.0)       # puxar_cada_s (120) vence antes de reserva_ttl_s/3 (600)
        n = self.hub.n_chamadas("puxar")
        t[0] = 50.0
        s.ciclo()
        self.assertEqual(self.hub.n_chamadas("puxar"), n)       # ainda não é hora
        t[0] = 121.0
        s.ciclo()
        self.assertEqual(self.hub.n_chamadas("puxar"), n + 1)

    def test_backoff_exponencial_e_recupera(self):
        s, t = self.sinc(self.b, espera_inicial=10, espera_maxima=40)
        self.hub.rede_caida = True
        esperas = []
        for _ in range(4):
            t[0] += 1000
            esperas.append(s.ciclo())
        self.assertEqual(esperas, [10, 20, 40, 40])
        self.assertIn("rede", s.status()["erro"])
        self.assertGreater(s.ciclo(), 0)        # dentro da espera: não bate no hub
        self.hub.rede_caida = False
        s.acordar(forcar=True)
        s.ciclo()
        self.assertIsNone(s.status()["erro"])

    def test_sem_url_fica_ocioso(self):
        self.b.settings.set("planilha", "url", "")
        s, _ = self.sinc(self.b)
        self.assertEqual(s.ciclo(), s._ociosa)
        self.assertEqual(self.hub.chamadas, [])

    def test_thread_roda_e_para(self):
        self.a.concluir()
        self.a.publicar(self.hub)
        s = sc.Sincronizador(self.b.home, self.b.settings, self.hub)
        s.start()
        try:
            self.assertTrue(wait_for(lambda: s.status()["aplicadas"] == 1))
        finally:
            s.stop()
            s.join(5)
        self.assertFalse(s.is_alive())

    def test_sobe_pacotes_em_pedacos_por_ciclo(self):
        self.a.concluir(extra_arquivos={"outputs/grande.bin": os.urandom(9000)})
        with mock.patch.object(sc, "TAMANHO_PARTE", 1000), mock.patch.object(sc, "PARTES_POR_CICLO", 3):
            self.a.publicar(self.hub, upload=False)
            s, _ = self.sinc(self.a)
            self.assertEqual(s.ciclo(), 0)
            self.assertEqual(s.status()["pacotes_pendentes"], 1)
            self.assertEqual(self.hub.n_chamadas("pacote"), 3)
            for _ in range(10):
                if not s.status()["pacotes_pendentes"]:
                    break
                s.ciclo()
        self.assertEqual(s.status()["pacotes_pendentes"], 0)


class TestSettingsPadrao(unittest.TestCase):
    def test_defaults_nao_vao_para_o_arquivo(self):
        with tempfile.TemporaryDirectory() as d:
            home = Home(Path(d)).ensure()
            s = load_settings(home)
            self.assertEqual((s.get("planilha", "sincronizar"), s.get("planilha", "puxar_cada_s"),
                              s.get("planilha", "reserva_ttl_s")), (True, 120, 1800))
            s.save()
            self.assertNotIn("puxar_cada_s", home.config_path.read_text(encoding="utf-8"))
            mid, _ = sc.maquina(s)
            self.assertIn(mid, home.config_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
