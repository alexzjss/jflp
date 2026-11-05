import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sbfl import cli
from sbfl.adapters.base import Bug
from sbfl.home import Home, load_settings
from sbfl.toolchain.profiles import MissingTool

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bugsjar_fixture import make_bugsjar_git


def _run_main(argv, home_dir):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, {"JFLP_HOME": str(home_dir)}), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jflp_cli_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_bug_spec_parsing(self):
        s = load_settings(Home(self.tmp / "h"))
        self.assertEqual(cli.parse_bug(s, "d4j/Jsoup/93").key, "d4j/Jsoup/93")
        folder = self.tmp / "proj"
        folder.mkdir()
        b = cli.parse_bug(s, f"local:{folder}")
        self.assertEqual((b.benchmark, b.project), ("local", "proj"))
        b = cli.parse_bug(s, "local:https://x.org/a/repo.git@abcdef1234567890")
        self.assertEqual((b.benchmark, b.project, b.bug_id), ("local", "repo", "abcdef123456"))
        bj = Bug("bugsjar", "maven", "bugs-dot-jar_MNG-1_abc", {"repo": "r"})
        with mock.patch("sbfl.cli.bug_from_key", return_value=bj) as m:
            self.assertIs(cli.parse_bug(s, bj.key), bj)
            m.assert_called_once_with(s, bj.key)
        with self.assertRaises(ValueError):
            cli.parse_bug(s, "lixo")

    def test_run_invokes_execute_with_options(self):
        bug = Bug("d4j", "Jsoup", "93")
        with mock.patch("sbfl.cli.execute", return_value="d4j/Jsoup/93/jaguar2/x") as ex, \
                mock.patch("sbfl.cli.load_run", return_value={"status": "ok_testes_falhando"}):
            rc, out, _ = _run_main(["run", "jaguar2", "d4j/Jsoup/93", "--all-tests", "--fix", "A",
                                    "--no-fix", "B", "--jdk", "11", "--maven", "3.9.9",
                                    "--no-search", "--no-archive"], self.tmp / "h")
        self.assertEqual(rc, 0)
        self.assertIn("ok_testes_falhando", out)
        home, settings, b, kind, opts = ex.call_args.args
        self.assertEqual((b.key, kind), (bug.key, "jaguar2"))
        self.assertEqual(opts.test_scope, "all")
        self.assertEqual(opts.fixes_add, frozenset({"A"}))      # soma às detectadas, não substitui
        self.assertIsNone(opts.fixes_on)
        self.assertEqual(opts.fixes_off, frozenset({"B"}))
        self.assertEqual((opts.jdk, opts.maven, opts.auto_search, opts.archive_checkout),
                         (11, "3.9.9", False, False))

    def test_run_refuses_when_another_instance_holds_the_lock(self):
        from sbfl.instance import WorkerLock
        h = self.tmp / "h"
        other = WorkerLock(Home(h).ensure())
        self.assertTrue(other.acquire())
        self.addCleanup(other.release)
        with mock.patch("sbfl.cli.execute") as ex:
            rc, out, err = _run_main(["run", "maven", "d4j/Jsoup/93"], h)
        self.assertEqual(rc, 1)
        ex.assert_not_called()
        self.assertIn("outra instância", err)

    def test_batch_run_refuses_when_another_instance_holds_the_lock(self):
        from sbfl.instance import WorkerLock
        h = self.tmp / "h"
        other = WorkerLock(Home(h).ensure())
        self.assertTrue(other.acquire())
        self.addCleanup(other.release)
        rc, out, err = _run_main(["batch", "run"], h)
        self.assertEqual(rc, 1)
        self.assertIn("outra instância", err)

    def test_batch_cancel_and_delete(self):
        from sbfl.queue import batch_status, create_batch
        h = Home(self.tmp / "h").ensure()
        bid = create_batch(h, "t", [Bug("local", "p", "1", {"dir": "/x"})], ["maven"], {})
        rc, out, err = _run_main(["batch", "delete", str(bid)], h.root)
        self.assertEqual(rc, 1)                       # ainda pendente: recusa
        self.assertIn("cancele antes", err)
        rc, out, _ = _run_main(["batch", "cancel", str(bid)], h.root)
        self.assertEqual(rc, 0)
        self.assertEqual(batch_status(h, bid)["counts"]["cancelled"], 1)
        rc, out, _ = _run_main(["batch", "delete", str(bid)], h.root)
        self.assertEqual(rc, 0)
        self.assertIn("excluído", out)

    def test_run_releases_the_lock_when_done(self):
        from sbfl.instance import WorkerLock
        h = self.tmp / "h"
        with mock.patch("sbfl.cli.execute", return_value="r"),                 mock.patch("sbfl.cli.load_run", return_value={"status": "ok"}):
            _run_main(["run", "maven", "d4j/Jsoup/93"], h)
        probe = WorkerLock(Home(h))
        self.assertTrue(probe.acquire())
        probe.release()

    def test_prepare_and_run_accept_refresh(self):
        with mock.patch("sbfl.cli.prepare") as pr:
            pr.return_value.pristine, pr.return_value.module, pr.return_value.truth = Path("p"), ".", []
            rc, out, _ = _run_main(["prepare", "d4j/Jsoup/93", "--refresh"], self.tmp / "h")
        self.assertEqual(rc, 0)
        self.assertTrue(pr.call_args.kwargs["refresh"])
        with mock.patch("sbfl.cli.execute", return_value="r") as ex,                 mock.patch("sbfl.cli.load_run", return_value={"status": "ok"}):
            _run_main(["run", "maven", "d4j/Jsoup/93", "--refresh"], self.tmp / "h")
        self.assertTrue(ex.call_args.args[4].refresh)

    def test_malformed_config_prints_message_and_returns_1(self):
        h = self.tmp / "h"
        h.mkdir()
        (h / "config.toml").write_text('[tools.jdks]\n"11" = "C:\\x"\n', encoding="utf-8")
        rc, out, err = _run_main(["doctor"], h)
        self.assertEqual(rc, 1)
        self.assertIn("config.toml", err)
        self.assertIn("aspas simples", err)
        self.assertNotIn("Traceback", err)

    def test_run_failure_status_returns_1(self):
        with mock.patch("sbfl.cli.execute", return_value="r"), \
                mock.patch("sbfl.cli.load_run", return_value={"status": "compilacao_falhou"}):
            rc, out, _ = _run_main(["run", "maven", "d4j/Jsoup/93"], self.tmp / "h")
        self.assertEqual(rc, 1)

    def test_run_prints_warnings(self):
        with mock.patch("sbfl.cli.execute", return_value="r"),                 mock.patch("sbfl.cli.load_run", return_value={
                    "status": "ok", "warnings": ["tentativa 2 ignorada: Maven 3.6.3 não encontrado"]}):
            rc, out, _ = _run_main(["run", "maven", "d4j/Jsoup/93"], self.tmp / "h")
        self.assertEqual(rc, 0)
        self.assertIn("aviso: tentativa 2 ignorada: Maven 3.6.3 não encontrado", out)

    def test_run_unexpected_error_returns_1_with_log(self):
        h = self.tmp / "h"
        with mock.patch("sbfl.cli.execute", side_effect=RuntimeError("quebrou")):
            rc, out, err = _run_main(["run", "maven", "d4j/Jsoup/93"], h)
        self.assertEqual(rc, 1)
        self.assertIn("erro inesperado (RuntimeError): quebrou", out)
        self.assertNotIn("Traceback", out + err)
        logs = list((h / "work" / "erros").glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn(str(logs[0]), out)
        self.assertIn("RuntimeError: quebrou", logs[0].read_text(encoding="utf-8"))

    def test_run_unexpected_error_mentions_created_run(self):
        h = self.tmp / "h"

        def boom(home, settings, bug, kind, opts):
            from sbfl.store import RunWriter
            w = RunWriter(home, bug, kind)
            w.finish({"status": "desconhecido"})
            raise ValueError("depois de gravar")
        with mock.patch("sbfl.cli.execute", side_effect=boom):
            rc, out, _ = _run_main(["run", "maven", "d4j/Jsoup/93"], h)
        self.assertEqual(rc, 1)
        self.assertRegex(out, r"execução: d4j/Jsoup/93/\d{8}-\d{6}-maven")

    def test_doctor_runs_without_tools(self):
        miss = MissingTool("jdk", "11", "JDK 11 não encontrado — baixe na tela Ambiente")
        with mock.patch("sbfl.cli.find_jdks", return_value=[]), \
                mock.patch("sbfl.cli.find_mavens", return_value=[]), \
                mock.patch("sbfl.cli.resolve", side_effect=miss):
            rc, out, _ = _run_main(["doctor"], self.tmp / "h")
        self.assertEqual(rc, 1)
        self.assertIn("JDK 11 não encontrado", out)

    def test_ui_without_web_returns_1(self):
        with mock.patch.dict(sys.modules, {"sbfl.web.app": None}):
            rc, out, err = _run_main(["ui", "--no-browser"], self.tmp / "h")
        self.assertEqual(rc, 1)
        self.assertIn("ainda não disponível", out + err)

    def test_fetch_requires_config(self):
        rc, out, err = _run_main(["fetch"], self.tmp / "h")
        self.assertEqual(rc, 1)
        self.assertIn("bugsjar", out + err)

    def test_help_lists_commands(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        for name in ("ui", "doctor", "tools", "bugs", "prepare", "run", "runs", "compare",
                     "batch", "export", "recipes", "reindex", "selftest", "fetch"):
            self.assertIn(name, out.getvalue())

    def test_recipes_and_reindex(self):
        h = Home(self.tmp / "h").ensure()
        (h.recipes / "d4j").mkdir()
        (h.recipes / "d4j" / "Jsoup.toml").write_text('[[recipe]]\nmatch = "*"\n', encoding="utf-8")
        rc, out, _ = _run_main(["recipes"], h.root)
        self.assertEqual(rc, 0)
        self.assertIn("d4j/Jsoup", out)
        rc, out, _ = _run_main(["reindex"], h.root)
        self.assertEqual(rc, 0)
        self.assertIn("0", out)


class CoberturaCliTests(unittest.TestCase):
    """Comandos `jflp cobertura ...`: uma colecao falsa do Bugs.jar com um projeto e dois bugs (6 jobs)."""

    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="jflp_cobmolde_"))
        (cls.base / "colecao").mkdir()
        repo = make_bugsjar_git(cls.base / "src", ids=("A-1", "A-2"))
        shutil.move(str(repo), str(cls.base / "colecao" / "maven"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jflp_cobcli_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.h = self.tmp / "h"
        self.root = self.tmp / "colecao"
        shutil.copytree(self.base / "colecao", self.root)

    def _sincronizar(self):
        return _run_main(["cobertura", "sincronizar", str(self.root)], self.h)

    def test_cli_cobertura_sincronizar(self):
        rc, out, err = self._sincronizar()
        self.assertEqual(rc, 0, err)
        self.assertIn("2 bugs", out)
        self.assertIn("6 jobs novos", out)
        rc, out, _ = self._sincronizar()
        self.assertEqual(rc, 0)
        self.assertIn("0 jobs novos", out)

    def test_cli_cobertura_sincronizar_usa_bugsjar_root(self):
        self.h.mkdir(parents=True)
        (self.h / "config.toml").write_text(f"[bugsjar]\nroot = '{self.root}'\n", encoding="utf-8")
        rc, out, err = _run_main(["cobertura", "sincronizar"], self.h)
        self.assertEqual(rc, 0, err)
        self.assertIn("2 bugs", out)

    def test_cli_cobertura_sincronizar_sem_pasta_recusa(self):
        rc, out, err = _run_main(["cobertura", "sincronizar"], self.h)
        self.assertEqual(rc, 1)
        self.assertIn("bugsjar.root", err)

    def test_cli_cobertura_status(self):
        self._sincronizar()
        rc, out, err = _run_main(["cobertura", "status"], self.h)
        self.assertEqual(rc, 0, err)
        self.assertIn("0 de 2 bugs concluídos", out)
        self.assertIn("pendente", out)

    def test_cli_cobertura_status_sem_lote(self):
        rc, out, err = _run_main(["cobertura", "status"], self.h)
        self.assertEqual(rc, 1)
        self.assertIn("sincronizar", err)

    def test_cli_cobertura_refazer(self):
        from sbfl import cobertura as cb
        self._sincronizar()
        bid = cb.batch_id(Home(self.h))
        with mock.patch("sbfl.cobertura.refazer", return_value=2) as rf:
            rc, out, err = _run_main(["cobertura", "refazer"], self.h)
            self.assertEqual(rc, 0, err)
            self.assertEqual(rf.call_args.args[1:3], (bid, None))
            self.assertTrue(rf.call_args.kwargs["so_falhas"])
            self.assertIn("2", out)
            rc, out, err = _run_main(["cobertura", "refazer", "--todos"], self.h)
            self.assertEqual(rf.call_args.args[1:3], (bid, None))
            self.assertFalse(rf.call_args.kwargs["so_falhas"])

    def test_cli_cobertura_importar_sem_lote_informa_erro(self):
        rc, out, err = _run_main(["cobertura", "importar", str(self.root)], self.h)
        self.assertEqual(rc, 1)
        self.assertIn("sincronize", out + err)

    def test_cli_cobertura_importar_ok(self):
        res = {"maven": 2, "jaguar1": 1, "ignorados": 3, "erros": []}
        with mock.patch("sbfl.cli.importar_pipeline", return_value=res) as imp:
            rc, out, err = _run_main(["cobertura", "importar", str(self.root)], self.h)
        self.assertEqual(rc, 0, err)
        self.assertIn("2 Maven", out)
        self.assertIn("1 Jaguar 1", out)
        self.assertEqual(imp.call_args.args[2], self.root)

    def test_cli_cobertura_importar_com_erros_sai_com_1(self):
        res = {"maven": 0, "jaguar1": 0, "ignorados": 0, "erros": ["bugsjar/maven/A-1: quebrou"]}
        with mock.patch("sbfl.cli.importar_pipeline", return_value=res):
            rc, out, err = _run_main(["cobertura", "importar", str(self.root)], self.h)
        self.assertEqual(rc, 1)
        self.assertIn("quebrou", err)

    def test_cli_cobertura_sincronizar_pasta_inexistente(self):
        rc, out, err = _run_main(["cobertura", "sincronizar", str(self.tmp / "nao_existe")], self.h)
        self.assertEqual(rc, 1)
        self.assertIn("não existe", err)
        from sbfl import cobertura as cb
        self.assertIsNone(cb.batch_id(Home(self.h)))

    def test_cli_cobertura_planilha_sem_flag_mostra_token(self):
        with mock.patch("sbfl.cli.garantir_token", return_value="TOKEN-XYZ"):
            rc, out, err = _run_main(["cobertura", "planilha"], self.h)
        self.assertEqual(rc, 0, err)
        self.assertIn("TOKEN-XYZ", out)
        self.assertIn("URL não configurada", out)

    def test_cli_cobertura_planilha_reenviar_sem_progresso(self):
        from sbfl.planilha import Enviador
        self._com_url()
        with mock.patch.object(Enviador, "ciclo", return_value=0) as ciclo:
            rc, out, err = _run_main(["cobertura", "planilha", "--reenviar"], self.h)
        self.assertEqual(rc, 1)
        self.assertEqual(ciclo.call_count, 3)
        self.assertIn("não avançou", err)

    def test_batch_run_aplica_pasta_de_trabalho(self):
        from sbfl.home import Home as H
        H(self.h).ensure()
        outra = H(self.h)
        with mock.patch("sbfl.cli.apply_work_dir", return_value=(outra, "aviso de unidade")) as apl, \
                mock.patch("sbfl.cli.WorkerPool") as pool:
            pool.return_value.status.return_value = {"last_error": None}
            rc, out, err = _run_main(["batch", "run"], self.h)
        self.assertEqual(rc, 0, err)
        self.assertIn("aviso de unidade", out)
        self.assertIs(pool.call_args.args[0], outra)
        apl.assert_called_once()

    def _com_url(self, url="https://script.example/exec"):
        self._sincronizar()
        cfg = load_settings(Home(self.h).ensure())
        cfg.set("planilha", "url", url)
        cfg.save()

    def test_cli_cobertura_planilha_reenviar(self):
        from sbfl import planilha as pl
        self._com_url()
        enviados = []

        def post(url, body):
            enviados.append(body)
            return {"ok": True, "n": len(body["linhas"])}
        with mock.patch("sbfl.planilha.post_http", side_effect=post):
            rc, out, err = _run_main(["cobertura", "planilha", "--reenviar"], self.h)
        self.assertEqual(rc, 0, err)
        self.assertEqual(sum(len(b["linhas"]) for b in enviados), 2)
        self.assertEqual(pl.pendentes(Home(self.h)), 0)
        self.assertIn("2 linha(s)", out)

    def test_cli_cobertura_planilha_reenviar_sem_url(self):
        self._sincronizar()
        rc, out, err = _run_main(["cobertura", "planilha", "--reenviar"], self.h)
        self.assertEqual(rc, 1)
        self.assertIn("URL", err)

    def test_cli_cobertura_planilha_reenviar_erro_do_app(self):
        self._com_url()
        with mock.patch("sbfl.planilha.post_http", return_value={"ok": False, "erro": "token"}):
            rc, out, err = _run_main(["cobertura", "planilha", "--reenviar"], self.h)
        self.assertEqual(rc, 1)
        self.assertIn("recusou", err)

    def test_cli_cobertura_sincronia_resumo(self):
        self._com_url()
        res = {"pedacos_enviados": 1, "pacotes_pendentes": 0, "aplicadas": 2, "mantidas": 1, "recusadas": [],
               "reservas_remotas": 3, "cursor": 5, "renovadas": 0, "liberadas": 0}
        with mock.patch("sbfl.sincronia.sincronizar_uma_vez", return_value=res):
            rc, out, err = _run_main(["cobertura", "sincronia"], self.h)
        self.assertEqual(rc, 0, err)
        self.assertIn("2 execução(ões) recebida(s)", out)
        self.assertIn("reservados por outros computadores: 3", out)

    def test_cli_cobertura_sincronia_hub_antigo_e_sem_url(self):
        from sbfl.sincronia import HubAntigo
        self._com_url()
        with mock.patch("sbfl.sincronia.sincronizar_uma_vez", side_effect=HubAntigo("x")):
            rc, out, err = _run_main(["cobertura", "sincronia"], self.h)
        self.assertEqual(rc, 1)
        self.assertIn("Nova versão", err)
        cfg = load_settings(Home(self.h).ensure())
        cfg.set("planilha", "url", "")
        cfg.save()
        rc, out, err = _run_main(["cobertura", "sincronia"], self.h)
        self.assertEqual(rc, 1)
        self.assertIn("URL", err)


if __name__ == "__main__":
    unittest.main()
