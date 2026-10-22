"""Visão por bug do lote: veredito, concordância Jaguar 1 × 2, Maven × catálogo, fonte no git e CSV."""
from __future__ import annotations

import contextlib
import csv
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from sbfl import bugview as bv
from sbfl import queue as jq
from sbfl import store
from sbfl.adapters.base import Bug
from sbfl.diffgt import FaultLine
from sbfl.home import Home
from sbfl.outputs import LineScore
from tests.bugsjar_fixture import make_bugsjar_git, ramo_de

FOO = """package org.x;
class Foo {
    int bar(int a) {
        // TODO velho
        return a;
    }

    void baz() {
        int x = 1;
        x++;
    }
}
"""

TRUTH = [FaultLine("org.x.Foo", 4, "removed").to_dict()]


def _rm(func, path, exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def sc(cls, line, susp):
    return LineScore(cls, line, 1, 0, 0, 1, susp)


def ranking(n, faixa=None):
    """n linhas de org.x.Other com suspeição decrescente; `faixa` = {posição: (cls, linha)} para encaixar o defeito."""
    out = []
    for i in range(1, n + 1):
        cls, line = (faixa or {}).get(i, ("org.x.Other", i))
        out.append(sc(cls, line, 1.0 - i / 1000))
    return out


FALTA = [FaultLine("org.x.Foo", 4, "removed")]


class Veredito(unittest.TestCase):
    def test_limites_do_top10(self):
        self.assertEqual(bv.veredito_jaguar(ranking(60, {10: ("org.x.Foo", 4)}), FALTA)[0], "achou")
        self.assertEqual(bv.veredito_jaguar(ranking(60, {11: ("org.x.Foo", 4)}), FALTA)[0], "parcial")
        self.assertEqual(bv.veredito_jaguar(ranking(60, {50: ("org.x.Foo", 4)}), FALTA)[0], "parcial")

    def test_fora_do_top50_so_parcial_pela_classe(self):
        self.assertEqual(bv.veredito_jaguar(ranking(80, {51: ("org.x.Foo", 4)}), FALTA)[0], "nao_achou")
        r = ranking(80, {51: ("org.x.Foo", 4), 3: ("org.x.Foo", 99)})      # outra linha da mesma classe no top 10
        self.assertEqual(bv.veredito_jaguar(r, FALTA)[0], "parcial")
        r = ranking(80, {3: ("org.x.Foo$Inner", 99)})                       # defeito fora do ranking, classe no top 10
        code, pos = bv.veredito_jaguar(r, FALTA)
        self.assertEqual((code, pos["melhor"]), ("parcial", None))

    def test_empate_usa_a_posicao_esperada(self):
        iguais = [sc("org.x.Foo", 4, 0.5)] + [sc("org.x.O", i, 0.5) for i in range(18)]   # 19 empatadas: esperada 10
        self.assertEqual(bv.veredito_jaguar(iguais, FALTA)[0], "achou")
        mais = iguais + [sc("org.x.O", 99, 0.5)]                                           # 20 empatadas: esperada 10,5
        code, pos = bv.veredito_jaguar(mais, FALTA)
        self.assertEqual((code, pos["melhor"]["expected"], pos["melhor"]["tied"]), ("parcial", 10.5, 20))
        self.assertIn("20 empatadas", pos["melhor"]["faixa"])

    def test_sem_dado(self):
        self.assertEqual(bv.veredito_jaguar(None, FALTA)[0], "sem_dado")
        self.assertEqual(bv.veredito_jaguar(ranking(5), [])[0], "sem_dado")

    def test_anchor_so_vale_sem_linhas_removidas(self):
        r = ranking(20, {1: ("org.x.Foo", 10)})
        self.assertEqual(bv.veredito_jaguar(r, [FaultLine("org.x.Foo", 10, "anchor")])[0], "achou")
        mix = [FaultLine("org.x.Foo", 10, "anchor"), FaultLine("org.x.Foo", 4, "removed")]
        self.assertNotEqual(bv.veredito_jaguar(r, mix)[0], "achou")

    def test_classe_interna_vale_pela_externa(self):
        r = ranking(20, {2: ("org.x.Foo$1", 4)})
        code, pos = bv.veredito_jaguar(r, FALTA)
        self.assertEqual((code, pos["melhor"]["best"]), ("achou", 2))

    def test_melhor_veredito_e_simbolos(self):
        self.assertEqual(bv.melhor_veredito(["nao_achou", "parcial", "sem_dado"]), "parcial")
        self.assertEqual(bv.melhor_veredito([]), "sem_dado")
        self.assertEqual(bv.veredito("achou"), {"code": "achou", "simbolo": "✓", "label": "achou"})
        self.assertEqual(bv.veredito("nao_achou")["simbolo"], "✗")
        self.assertEqual(bv.veredito("parcial")["simbolo"], "~")
        self.assertEqual(bv.veredito("sem_dado")["simbolo"], "—")

    def test_maven_classes(self):
        esp = ["org.x.FooTest", "org.x.BoomTest"]
        self.assertEqual(bv.veredito_maven(["org.x.FooTest#a", "org.x.BoomTest#b"], esp), "achou")
        self.assertEqual(bv.veredito_maven(["org.x.FooTest#a"], esp), "parcial")
        self.assertEqual(bv.veredito_maven(["org.x.Zed#a"], esp), "nao_achou")
        self.assertEqual(bv.veredito_maven([], esp), "nao_achou")
        self.assertEqual(bv.veredito_maven(["org.x.FooTest#a"], []), "sem_dado")
        e = bv.esperado_maven(["org.x.FooTest#a", "org.x.Zed#a"], esp)
        self.assertEqual((e["estado"], e["a_mais"], e["faltando"]), ("diferentes", ["org.x.Zed"], ["org.x.BoomTest"]))
        self.assertEqual(e["texto"], "a mais: Zed; faltando: BoomTest")
        self.assertEqual(bv.esperado_maven(["org.x.FooTest#a"], ["org.x.FooTest"])["texto"], "iguais")
        self.assertEqual(bv.esperado_maven(["a.B#c"], None)["estado"], "sem_catalogo")


class Metodo(unittest.TestCase):
    def met(self, src, n):
        return bv.metodo_da_linha(src.strip("\n").splitlines(), n)

    def test_simples_e_fora_de_metodo(self):
        lin = FOO.splitlines()
        self.assertEqual(bv.metodo_da_linha(lin, 4), "bar")
        self.assertEqual(bv.metodo_da_linha(lin, 10), "baz")
        self.assertIsNone(bv.metodo_da_linha(lin, 2))
        self.assertIsNone(bv.metodo_da_linha(lin, 7))

    def test_construtor_assinatura_longa_e_throws(self):
        src = """
class A {
    @Override @SuppressWarnings("unchecked")
    public <T extends Comparable<T>> List<T> foo(final int a,
                                                 String b) throws IOException, Foo {
        return null;
    }
    A(int x) {
        this.x = x;
    }
    static {
        init();
    }
}
"""
        self.assertEqual(self.met(src, 5), "foo")
        self.assertEqual(self.met(src, 8), "A")
        self.assertEqual(self.met(src, 11), "<static>")

    def test_chaves_em_strings_comentarios_e_chars(self):
        src = """
class A {
    void a() {
        String s = "}}{";   // } comentário {
        char c = '{';
        char d = '\\'';
        /* } */
        String t = "\\"}";
    }
    void b() {
        x();
    }
}
"""
        self.assertEqual(self.met(src, 7), "a")
        self.assertEqual(self.met(src, 10), "b")

    def test_classes_internas_e_anonimas(self):
        src = """
class A {
    void a() {
        Runnable r = new Runnable() {
            public void run() {
                foo();
            }
        };
        bar();
    }
    static class In {
        int g() {
            return 1;
        }
    }
}
"""
        self.assertEqual(self.met(src, 6), "run")          # o método da classe anônima, não o `a`
        self.assertEqual(self.met(src, 8), "a")
        self.assertEqual(self.met(src, 9), "a")
        self.assertEqual(self.met(src, 13), "g")

    def test_lambda_if_try_pertencem_ao_metodo(self):
        src = """
class A {
    void a() {
        list.forEach(x -> {
            if (x > 0) {
                try {
                    f(x);
                } catch (Exception e) {
                    g();
                }
            }
        });
        int[] v = new int[] { 1, 2 };
        for (int i = 0; i < 2; i++) { h(); }
    }
}
"""
        for n in (5, 7, 9, 13):
            self.assertEqual(self.met(src, n), "a")

    def test_interface_default_e_enum(self):
        src = """
interface I {
    default int m() {
        return 1;
    }
}
enum E {
    X, Y;
    int n() {
        return 2;
    }
}
"""
        self.assertEqual(self.met(src, 4), "m")
        self.assertEqual(self.met(src, 10), "n")


def _git(repo, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *a],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)


_REPO: dict = {}


def repo_compartilhado() -> Path:
    """Repositório git falso criado uma vez para todos os testes (eles só leem do git)."""
    if "p" not in _REPO:
        base = Path(tempfile.mkdtemp(prefix="jflp_bvrepo_"))
        _REPO["base"] = base
        repo = make_bugsjar_git(base, ids=("MNG-1", "MNG-2", "MNG-3"))
        for i in ("MNG-1", "MNG-2", "MNG-3"):
            _git(repo, "checkout", "-q", ramo_de(i))
            (repo / "maven-core/src/main/java/org/x/Foo.java").write_text(FOO, encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "fonte")
        _git(repo, "checkout", "-q", "master")
        _REPO["p"] = repo
    return _REPO["p"]


def tearDownModule():
    if "base" in _REPO:
        shutil.rmtree(_REPO["base"], onerror=_rm)


class Base(unittest.TestCase):
    """Repositório Bugs.jar falso com git e execuções gravadas à mão."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jflp_bv_"))
        self.addCleanup(shutil.rmtree, self.tmp, onerror=_rm)
        self.home = Home(self.tmp / "home").ensure()
        self.repo = repo_compartilhado()
        bv.limpar_cache()

    def bug(self, i):
        ramo = ramo_de(i)
        return Bug("bugsjar", "maven", ramo, {"repo": str(self.repo), "buggy": ramo, "fixed": ramo[-8:]})

    def lote(self, ids, kinds):
        return jq.create_batch(self.home, "t", [self.bug(i) for i in ids], kinds, {})

    def finish(self, bid, bug_id, kind, rel=None, state="done", error=None):
        key = self.bug(bug_id).key
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state=?, run_rel=?, error=? WHERE batch_id=? AND bug_key=? AND kind=?",
                        (state, rel, error, bid, key, kind))

    def grava(self, bug_id, kind, status="ok_testes_falhando", outputs=None, truth=TRUTH, **extra):
        w = store.RunWriter(self.home, self.bug(bug_id), kind)
        for rel, text in (outputs or {}).items():
            w.write_text(rel, text)
        doc = {"status": status, "started": "2026-01-01T10:00:00", **extra}
        if truth is not None:
            doc["truth"] = truth
        w.finish(doc)
        return w.rel

    def j2(self, bug_id, rows, **kw):
        """Execução Jaguar 2 com o CSV `classe,linha,susp` (classe com `.`)."""
        txt = "".join(f"{c.replace('.', '/')},{ln},1,0,0,2,{s}\n" for c, ln, s in rows)
        return self.grava(bug_id, "jaguar2", outputs={"outputs/jaguar2.csv": txt}, **kw)

    def j1(self, bug_id, rows, **kw):
        xml = "<root>" + "".join(
            f'<requirements name="{c}" location="{ln}" suspicious-value="{s}" cef="1" cep="0" cnf="0" cnp="2"/>'
            for c, ln, s in rows) + "</root>"
        return self.grava(bug_id, "jaguar1", outputs={"outputs/jaguar1.xml": xml}, **kw)

    def maven(self, bug_id, falhas, status="ok_testes_falhando"):
        fl = [{"test": t, "message": "esperava 1", "type": "junit.framework.AssertionFailedError",
               "frame": {"cls": "org.x.Foo", "method": "bar", "file": "Foo.java", "line": 4}} for t in falhas]
        return self.grava(bug_id, "maven", status=status, failures=fl,
                        tests={"total": 10, "passed": 10 - len(falhas), "failed": len(falhas), "error": 0, "skipped": 0})


def linhas(n, defeito_pos=None):
    rows = [("org.x.Other", i, round(1.0 - i / 1000, 6)) for i in range(1, n + 1)]
    if defeito_pos:
        rows[defeito_pos - 1] = ("org.x.Foo", 4, rows[defeito_pos - 1][2])
    return rows


class Lote(Base):
    def test_tabela_por_bug_com_vereditos_e_contagem(self):
        bid = self.lote(["MNG-1", "MNG-2", "MNG-3"], ["maven", "jaguar1", "jaguar2"])
        # MNG-1: J2 acha na posição 1, J1 na 3
        self.finish(bid, "MNG-1", "maven", self.maven("MNG-1", ["org.x.FooTest#testA"]))
        self.finish(bid, "MNG-1", "jaguar2", self.j2("MNG-1", linhas(60, 1)))
        self.finish(bid, "MNG-1", "jaguar1", self.j1("MNG-1", linhas(60, 3)))
        # MNG-2: J2 só na 20 (parcial), J1 com erro
        self.finish(bid, "MNG-2", "jaguar2", self.j2("MNG-2", linhas(60, 20)))
        self.finish(bid, "MNG-2", "jaguar1", self.grava("MNG-2", "jaguar1", status="jaguar_crash"))
        self.finish(bid, "MNG-2", "maven", self.maven("MNG-2", []))
        # MNG-3: nada terminou
        v = bv.lote_view(self.home, bid)
        self.assertEqual(v["kinds"], ["maven", "jaguar1", "jaguar2"])
        self.assertEqual([b["id"] for b in v["bugs"]], ["MNG-1", "MNG-2", "MNG-3"])
        b1, b2, b3 = v["bugs"]
        self.assertEqual((b1["project"], b1["branch"]), ("maven", ramo_de("MNG-1")))
        self.assertEqual(b1["celulas"]["maven"]["texto"], "1 falha")
        self.assertEqual(b1["celulas"]["maven"]["falhas"], 1)
        self.assertEqual(b1["celulas"]["jaguar2"]["texto"], "pos. 1")
        self.assertEqual(b1["celulas"]["jaguar1"]["pos"]["expected"], 3)
        self.assertEqual(b1["veredito"]["code"], "achou")
        self.assertEqual(b1["posicoes"], "J2 pos. 1 · J1 pos. 3")
        self.assertEqual(b2["celulas"]["maven"]["texto"], "nenhuma falha")
        self.assertEqual(b2["celulas"]["jaguar1"]["tom"], "erro")
        self.assertEqual(b2["celulas"]["jaguar1"]["texto"], "o Jaguar caiu")
        self.assertEqual(b2["celulas"]["jaguar1"]["veredito"]["code"], "sem_dado")
        self.assertEqual(b2["veredito"]["code"], "parcial")      # o melhor entre os Jaguar
        self.assertEqual(b3["celulas"]["maven"]["texto"], "na fila")
        self.assertEqual(b3["veredito"]["code"], "sem_dado")
        self.assertEqual(v["contagem"], {"achou": 1, "parcial": 1, "nao_achou": 0, "sem_dado": 1})
        self.assertTrue(v["pendente"])
        self.assertEqual((v["total_jobs"], v["feitos"]), (9, 6))

    def test_colunas_so_das_ferramentas_usadas(self):
        bid = self.lote(["MNG-1"], ["jaguar2"])
        self.assertEqual(bv.lote_view(self.home, bid)["kinds"], ["jaguar2"])

    def test_sig_muda_so_com_o_estado_e_pendente_acaba(self):
        bid = self.lote(["MNG-1"], ["maven"])
        a = bv.lote_view(self.home, bid)
        self.assertEqual(bv.lote_view(self.home, bid)["sig"], a["sig"])
        self.assertEqual(bv.lote_sig(self.home, bid), a["sig"])
        self.finish(bid, "MNG-1", "maven", state="running")
        b = bv.lote_view(self.home, bid)
        self.assertNotEqual(a["sig"], b["sig"])
        self.assertEqual(b["bugs"][0]["celulas"]["maven"]["texto"], "rodando")
        self.assertTrue(b["pendente"])
        self.finish(bid, "MNG-1", "maven", self.maven("MNG-1", []))
        c = bv.lote_view(self.home, bid)
        self.assertFalse(c["pendente"])
        self.assertNotEqual(b["sig"], c["sig"])

    def test_cancelado_e_job_sem_execucao(self):
        bid = self.lote(["MNG-1", "MNG-2"], ["jaguar2"])
        self.finish(bid, "MNG-1", "jaguar2", state="cancelled")
        self.finish(bid, "MNG-2", "jaguar2", None, error="falhou antes de gravar")
        v = bv.lote_view(self.home, bid)
        self.assertEqual(v["bugs"][0]["celulas"]["jaguar2"]["texto"], "cancelado")
        self.assertEqual(v["bugs"][1]["celulas"]["jaguar2"]["tom"], "erro")
        self.assertFalse(v["pendente"])
        self.assertEqual(v["contagem"]["sem_dado"], 2)

    def test_gabarito_vem_do_catalogo_quando_a_execucao_nao_tem_truth(self):
        bid = self.lote(["MNG-1"], ["jaguar2"])
        self.finish(bid, "MNG-1", "jaguar2", self.j2("MNG-1", linhas(30, 2), truth=None))
        v = bv.lote_view(self.home, bid)
        self.assertEqual(v["bugs"][0]["celulas"]["jaguar2"]["pos"]["expected"], 2)

    def test_sem_gabarito_nem_catalogo(self):
        bug = Bug("local", "proj", "dir-1", {"path": str(self.tmp)})
        bid = jq.create_batch(self.home, "t", [bug], ["jaguar2"], {})
        w = store.RunWriter(self.home, bug, "jaguar2")
        w.write_text("outputs/jaguar2.csv", "org/x/Foo,4,1,0,0,2,0.9\n")
        w.finish({"status": "ok_testes_falhando"})
        with contextlib.closing(jq._connect(self.home)) as con, con:
            con.execute("UPDATE jobs SET state='done', run_rel=? WHERE batch_id=?", (w.rel, bid))
        v = bv.lote_view(self.home, bid)
        c = v["bugs"][0]
        self.assertEqual(c["celulas"]["jaguar2"]["texto"], "sem gabarito")
        self.assertEqual((c["veredito"]["code"], c["id"], c["catalogo"]), ("sem_dado", "dir-1", False))
        self.assertIsNone(bv.bug_detail(self.home, bid, "x/y/z"))
        self.assertIsNone(bv.bug_detail(self.home, bid, bug.key)["catalogo"])

    def test_so_maven_compara_classes_com_o_catalogo(self):
        bid = self.lote(["MNG-1", "MNG-2", "MNG-3"], ["maven"])
        self.finish(bid, "MNG-1", "maven", self.maven("MNG-1", ["org.x.FooTest#a", "org.x.BoomTest#b"]))
        self.finish(bid, "MNG-2", "maven", self.maven("MNG-2", ["org.x.FooTest#a"]))
        self.finish(bid, "MNG-3", "maven", self.maven("MNG-3", ["org.x.Outro#a"]))
        v = bv.lote_view(self.home, bid)
        self.assertEqual([b["veredito"]["code"] for b in v["bugs"]], ["achou", "parcial", "nao_achou"])

    def test_resumos_sob_demanda(self):
        bid = self.lote(["MNG-1"], ["maven"])
        r = bv.lote_resumos(self.home, bid)[self.bug("MNG-1").key]
        self.assertEqual(r["resumo"], "resumo do MNG-1")
        self.assertEqual(r["jira"], "https://issues.apache.org/jira/browse/MNG-1")


class Detalhe(Base):
    def setUp(self):
        super().setUp()
        self.bid = self.lote(["MNG-1"], ["maven", "jaguar1", "jaguar2"])
        # J2: defeito na posição 1; J1: posição 3. Linhas Other 1..4 e 20 em comum, o resto diverge.
        j2 = [("org.x.Foo", 4, 0.9), ("org.x.Other", 1, 0.8), ("org.x.Other", 2, 0.7), ("org.x.Zed", 9, 0.6)] + \
             [("org.x.P", i, 0.5 - i / 1000) for i in range(1, 70)]
        j1 = [("org.x.Other", 1, 0.95), ("org.x.Other", 2, 0.9), ("org.x.Foo", 4, 0.85), ("org.x.Q", 5, 0.5)] + \
             [("org.x.R", i, 0.4 - i / 1000) for i in range(1, 70)]
        self.finish(self.bid, "MNG-1", "maven", self.maven("MNG-1", ["org.x.FooTest#testA", "org.x.ExtraTest#t"]))
        self.finish(self.bid, "MNG-1", "jaguar2", self.j2("MNG-1", j2))
        self.finish(self.bid, "MNG-1", "jaguar1", self.j1("MNG-1", j1))
        self.d = bv.bug_detail(self.home, self.bid, self.bug("MNG-1").key)

    def test_cabecalho(self):
        self.assertEqual(self.d["bug"]["id"], "MNG-1")
        self.assertEqual(self.d["kinds"], ["maven", "jaguar1", "jaguar2"])
        self.assertEqual(self.d["veredito"]["code"], "achou")

    def test_maven_falhas_e_esperado(self):
        m = self.d["maven"]
        self.assertEqual(m["n_falhas"], 2)
        f = m["falhas_lista"][0]
        self.assertEqual((f["cls"], f["metodo"], f["tipo"], f["message"]),
                         ("org.x.FooTest", "testA", "junit.framework.AssertionFailedError", "esperava 1"))
        self.assertEqual(f["frame_text"], "org.x.Foo.bar (Foo.java:4)")
        e = m["esperado"]
        self.assertEqual((e["estado"], e["a_mais"], e["faltando"]), ("diferentes", ["org.x.ExtraTest"], ["org.x.BoomTest"]))

    def test_jaguar_top_estrela_comum_e_mais(self):
        j2 = self.d["jaguar"]["jaguar2"]
        self.assertEqual(len(j2["top"]), 10)
        self.assertEqual(len(j2["top"]) + len(j2["mais"]), 50)
        self.assertTrue(j2["tem_mais"])
        primeira = j2["top"][0]
        self.assertEqual((primeira["cls"], primeira["line"], primeira["rank"], primeira["defeito"], primeira["comum"]),
                         ("org.x.Foo", 4, 1, True, True))        # a linha do defeito também está no top 10 do J1
        self.assertEqual((j2["top"][1]["short"], j2["top"][1]["defeito"]), ("Other", False))
        self.assertFalse(j2["top"][3]["comum"])                  # Zed:9 só existe no J2
        self.assertEqual(j2["veredito"]["code"], "achou")
        self.assertEqual(j2["posicao"]["melhor"]["expected"], 1)
        self.assertEqual(j2["rel"], bv.lote_view(self.home, self.bid)["bugs"][0]["celulas"]["jaguar2"]["rel"])
        self.assertEqual(j2["total"], 73)

    def test_jaguar_com_erro_mostra_causa(self):
        bid = self.lote(["MNG-2"], ["jaguar2"])
        rel = self.grava("MNG-2", "jaguar2", status="jaguar_sem_saida", diagnosis={"cause": "o plugin não gravou nada"})
        self.finish(bid, "MNG-2", "jaguar2", rel)
        j = bv.bug_detail(self.home, bid, self.bug("MNG-2").key)["jaguar"]["jaguar2"]
        self.assertEqual((j["top"], j["causa"], j["veredito"]["code"]), ([], "o plugin não gravou nada", "sem_dado"))
        self.assertEqual(j["label"], "o Jaguar não gerou resultado")

    def test_concordancia_valores(self):
        c = self.d["concordancia"]
        chaves = {(x["cls"], x["line"]) for x in c["comuns"]}
        self.assertEqual(chaves, {("org.x.Foo", 4), ("org.x.Other", 1), ("org.x.Other", 2)})
        self.assertEqual((c["n_comuns"], c["texto_comuns"]), (3, "3 de 10"))
        self.assertFalse(c["mesma_classe_topo"])
        self.assertEqual(c["classe_topo"], {"jaguar1": "org.x.Other", "jaguar2": "org.x.Foo"})
        self.assertEqual(c["achou_antes"], "jaguar2")
        self.assertEqual(c["expected"], {"jaguar1": 3, "jaguar2": 1})
        foo = next(x for x in c["comuns"] if x["cls"] == "org.x.Foo")
        self.assertAlmostEqual(foo["dif"], 0.05)
        self.assertEqual((foo["susp1"], foo["susp2"]), (0.85, 0.9))

    def test_concordancia_so_com_os_dois(self):
        bid = self.lote(["MNG-2"], ["jaguar2"])
        self.finish(bid, "MNG-2", "jaguar2", self.j2("MNG-2", linhas(20, 1)))
        self.assertIsNone(bv.bug_detail(self.home, bid, self.bug("MNG-2").key)["concordancia"])

    def test_catalogo(self):
        c = self.d["catalogo"]
        self.assertEqual((c["id"], c["resumo"]), ("MNG-1", "resumo do MNG-1"))
        self.assertEqual(c["jira"], "https://issues.apache.org/jira/browse/MNG-1")
        self.assertEqual(c["testes"], ["org.x.BoomTest", "org.x.FooTest"])
        self.assertEqual(c["modulo"], "maven-core")
        self.assertEqual([(m["short"], m["metodo"], m["linhas"]) for m in c["metodos"]], [("Foo", "bar", [4])])
        ln = c["linhas"][0]
        self.assertEqual((ln["cls"], ln["line"], ln["metodo"]), ("org.x.Foo", 4, "bar"))
        self.assertEqual(ln["pos"]["jaguar2"]["best"], 1)
        self.assertEqual(ln["pos"]["jaguar1"]["best"], 3)
        tipos = [(x["tipo"], x["texto"]) for x in c["patch"]]
        self.assertIn(("rem", "-        // TODO velho"), tipos)
        self.assertIn(("arquivo", "maven-core/src/main/java/org/x/Foo.java"), tipos)
        self.assertFalse(any("FooTest" in t for _k, t in tipos))      # patch de teste fica de fora

    def test_top_n_e_mais_n_configuraveis(self):
        d = bv.bug_detail(self.home, self.bid, self.bug("MNG-1").key, top_n=5, more_n=8)
        j = d["jaguar"]["jaguar1"]
        self.assertEqual((len(j["top"]), len(j["mais"])), (5, 3))


class Catalogo(Base):
    def test_fonte_do_ramo_sem_checkout(self):
        got = bv.fonte_bugsjar(self.repo, ramo_de("MNG-1"), "org.x.Foo")
        self.assertEqual(got[0], "maven-core/src/main/java/org/x/Foo.java")
        self.assertEqual(got[1][3], "        // TODO velho")
        self.assertEqual(bv.fonte_bugsjar(self.repo, ramo_de("MNG-1"), "org.x.Foo$Inner")[0], got[0])
        self.assertIsNone(bv.fonte_bugsjar(self.repo, ramo_de("MNG-1"), "org.x.FooTest"))    # teste não é fonte
        self.assertIsNone(bv.fonte_bugsjar(self.repo, ramo_de("MNG-1"), "org.x.Nada"))
        self.assertIsNone(bv.fonte_bugsjar(self.repo, "nao-existe", "org.x.Foo"))

    def test_catalogo_do_git_usa_o_nucleo_de_texto(self):
        cat = bv.catalogo_bugsjar(self.repo, ramo_de("MNG-1"))
        self.assertEqual([(f.cls, f.line, f.kind) for f in cat["faults"]],
                         [("org.x.Foo", 4, "removed"), ("org.x.Foo", 10, "anchor"), ("org.x.Foo", 11, "anchor")])
        self.assertEqual((cat["module"], cat["tests"]), ("maven-core", ["org.x.BoomTest", "org.x.FooTest"]))
        self.assertIsNone(bv.catalogo_bugsjar(self.repo, "nao-existe"))

    def test_nucleo_de_texto_resolve_nomes_simples(self):
        from sbfl.prepare import bugsjar_patch_text
        res = "Failed tests:\n  FooTest.testA:340 x\n"
        r = bugsjar_patch_text("", res, find_tests=lambda s: {"org.x." + n for n in s})
        self.assertEqual(r["tests"], ["org.x.FooTest"])

    def test_resumo_sem_url_no_yml_monta_link_do_jira(self):
        self.assertEqual(bv.jira_url("LANG-7"), "https://issues.apache.org/jira/browse/LANG-7")
        self.assertEqual(bv.resumo_bugsjar(self.repo, ramo_de("MNG-2"))["resumo"], "resumo do MNG-2")
        self.assertEqual(bv.resumo_bugsjar(self.repo, "nao-existe")["resumo"], "")


class Csv(Base):
    def test_colunas_e_linhas(self):
        bid = self.lote(["MNG-1", "MNG-2"], ["maven", "jaguar1", "jaguar2"])
        self.finish(bid, "MNG-1", "maven", self.maven("MNG-1", ["org.x.FooTest#a"]))
        self.finish(bid, "MNG-1", "jaguar2", self.j2("MNG-1", linhas(30, 1)))
        self.finish(bid, "MNG-1", "jaguar1", self.j1("MNG-1", linhas(30, 12)))
        text = bv.lote_csv(self.home, bid)
        self.assertNotIn("\r", text)
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(list(rows[0]), bv.CSV_COLUNAS)
        self.assertEqual(len(rows), 2)
        r = rows[0]
        self.assertEqual((r["projeto"], r["bug"], r["resumo"]), ("maven", "MNG-1", "resumo do MNG-1"))
        self.assertEqual((r["status_maven"], r["falhas_maven"], r["falhou_o_esperado"]),
                         ("concluída, com testes falhando", "1", "faltando: BoomTest"))
        self.assertEqual((r["pos_esperada_j2"], r["pos_melhor_j2"], r["pos_pior_j2"]), ("1", "1", "1"))
        self.assertEqual(r["pos_esperada_j1"], "12")
        self.assertEqual((r["veredito_j2"], r["veredito_j1"], r["veredito_geral"]), ("achou", "parcial", "achou"))
        self.assertEqual(r["mesma_classe_topo"], "não")
        self.assertEqual(r["metodo_defeito"], "Foo.bar")
        self.assertEqual(r["linhas_comuns_top10"], "9")      # o defeito está no top 10 do J2 (pos. 1) mas não do J1 (pos. 12)
        p = rows[1]       # MNG-2 ainda na fila
        self.assertEqual((p["status_maven"], p["status_j2"], p["veredito_geral"], p["pos_esperada_j2"]),
                         ("na fila", "na fila", "sem dado", ""))

    def test_ferramenta_ausente_fica_vazia(self):
        bid = self.lote(["MNG-1"], ["jaguar2"])
        self.finish(bid, "MNG-1", "jaguar2", self.j2("MNG-1", linhas(30, 1)))
        r = next(csv.DictReader(io.StringIO(bv.lote_csv(self.home, bid))))
        self.assertEqual((r["status_maven"], r["status_j1"], r["veredito_j1"], r["linhas_comuns_top10"]), ("", "", "", ""))


if __name__ == "__main__":
    unittest.main()
