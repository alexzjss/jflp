import contextlib
import io
import os
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
        self.assertEqual(opts.fixes_on, frozenset({"A"}))
        self.assertEqual(opts.fixes_off, frozenset({"B"}))
        self.assertEqual((opts.jdk, opts.maven, opts.auto_search, opts.archive_checkout),
                         (11, "3.9.9", False, False))

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


if __name__ == "__main__":
    unittest.main()
