from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET
from unittest import mock
from pathlib import Path

from sbfl.adapters.base import Bug, Ctx
from sbfl.home import Home, load_settings
from sbfl.jaguar import JaguarOptions, run_jaguar
from sbfl.outputs import parse_xml_jaguar1
from sbfl.prepare import Prepared
from sbfl.run_jaguar1 import run_jaguar1
from sbfl.run_jaguar2 import prefetch_jaguar2, run_jaguar2
from sbfl.run_maven import AttemptConfig
from sbfl.toolchain.detect import Jdk, Maven, find_jdks
from sbfl.toolchain.profiles import ToolEnv

DATA = Path(__file__).resolve().parent.parent / "sbfl" / "selftest_data"
POM = ("<project><modelVersion>4.0.0</modelVersion><groupId>g</groupId>"
       "<artifactId>{}</artifactId><version>1</version></project>")

FAKE_MVN = textwrap.dedent("""\
    import json, os, sys
    with open(r"{calls}", "a") as f:
        f.write(json.dumps({{"cwd": os.getcwd(), "argv": sys.argv[1:],
                            "jto": os.environ.get("JAVA_TOOL_OPTIONS")}}) + "\\n")
    if os.environ.get("FAKE_FAIL") and any(os.environ["FAKE_FAIL"] in a for a in sys.argv):
        sys.exit(1)
    if "-f" in sys.argv and "test" in sys.argv:
        os.makedirs("target", exist_ok=True)
        open("target/jaguar2.csv", "w").write("classe,linha,cef,cnf,cep,cnp,susp\\ndemo/Calc,6,1,0,0,1,1.0\\n")
    if "dependency:copy-dependencies" in sys.argv:
        os.makedirs("target/dependency", exist_ok=True)
    if "test-compile" in sys.argv:
        os.makedirs("target/classes", exist_ok=True)
        os.makedirs("target/test-classes", exist_ok=True)
    """)

FAKE_JAVA = textwrap.dedent('''\
    import sys, os
    a = sys.argv
    tf = a[a.index("-tf") + 1]; out = a[a.index("-o") + 1]
    for cls in open(tf).read().split():
        if cls == "x.CrashTest": sys.exit(137)
        st = "Failed" if cls == "x.FailTest" else "Passed"
        print(f"10:00:00.000 [main] DEBUG JaguarLogger - Test t({cls}) : {st}", flush=True)
    os.makedirs(".jaguar", exist_ok=True)
    p = os.path.abspath(f".jaguar/{out}.xml")
    open(p, "w").write('<F><requirements location="5" cef="1" cep="0" cnf="0" cnp="2" name="a.B" suspicious-value="1.0"/></F>')
    print(f"10:00:01.000 [main] INFO JaguarLogger - Output xml created at: {p}")
    ''')


def _wrapper(path: Path, script: Path) -> Path:
    """Executável falso que chama o script com o Python atual."""
    if os.name == "nt":
        path = path.with_suffix(".cmd")
        path.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class FakeEnv(ToolEnv):
    fake_java: str = ""

    def java(self) -> str:
        return self.fake_java


def _has_jdk11() -> bool:
    h = Home(Path(tempfile.mkdtemp()) / "none")
    return any(j.major == 11 for j in find_jdks(h, load_settings(h)))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Home(self.tmp / "Pasta Ção")
        self.settings = load_settings(self.home)

    def toolenv(self, major=11, cls=ToolEnv):
        mhome = self.tmp / "maven"
        (mhome / "bin").mkdir(parents=True, exist_ok=True)
        self.calls = self.tmp / "calls.jsonl"
        script = mhome / "bin" / "fake.py"
        script.write_text(FAKE_MVN.format(calls=self.calls), encoding="utf-8")
        if os.name == "nt":
            (mhome / "bin" / "mvn.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        else:
            _wrapper(mhome / "bin" / "mvn", script)
        return cls(Jdk(self.tmp / "jdk", major, f"{major}.0.1", "managed"),
                   Maven(mhome, (3, 9, 9), "managed"), dict(os.environ), self.home.m2)

    def read_calls(self):
        return [json.loads(l) for l in self.calls.read_text().splitlines()]


class Jaguar2Test(Base):
    def _prep(self, work, module, tests=None):
        return Prepared(Bug("local", "p", "1"), work, module, tests, [], {})

    def test_jaguar2_multimodule_installs_reactor_first(self):
        work = self.tmp / "w"
        (work / "core").mkdir(parents=True)
        (work / "pom.xml").write_text("<project/>")
        (work / "pom.xml").write_text(POM.format("root"))
        (work / "core" / "pom.xml").write_text(POM.format("core"))
        r = run_jaguar2(self.home, self.settings, self._prep(work, "core", ["a.BTest"]), work,
                        AttemptConfig(), self.tmp / "l" / "run.log", toolenv=self.toolenv())
        calls = self.read_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(Path(calls[0]["cwd"]), work)
        self.assertIn("install", calls[0]["argv"])
        self.assertIn("-DskipTests", calls[0]["argv"])
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("-pl") + 1], "core")
        self.assertIn("-am", argv)
        self.assertEqual(Path(calls[1]["cwd"]), work / "core")
        self.assertEqual(calls[1]["argv"][calls[1]["argv"].index("-f") + 1], "jaguar2-pom.xml")
        self.assertIn("test", calls[1]["argv"])
        self.assertIn("-Dtest=a.BTest", calls[1]["argv"])
        self.assertIn(f"-Djaguar2.classesDirs={work / 'core' / 'target' / 'classes'}", calls[1]["argv"])
        self.assertTrue((work / "core" / "jaguar2-pom.xml").exists())
        self.assertFalse((work / "jaguar2-pom.xml").exists())
        text = r.log.read_text(encoding="utf-8")
        self.assertIn("# jflp fase: reactor", text)
        self.assertIn("# jflp fase: jaguar2", text)

    def test_jaguar2_unblock_http_passes_settings_to_every_mvn_call(self):
        work = self.tmp / "w"
        (work / "core").mkdir(parents=True)
        (work / "pom.xml").write_text("<project/>")
        (work / "pom.xml").write_text(POM.format("root"))
        (work / "core" / "pom.xml").write_text(POM.format("core"))
        r = run_jaguar2(self.home, self.settings, self._prep(work, "core", ["a.BTest"]), work,
                        AttemptConfig(fixes_on=frozenset({"unblock-http"})), self.tmp / "l" / "run.log",
                        toolenv=self.toolenv())
        calls = self.read_calls()
        self.assertEqual(len(calls), 2)
        for c in calls:
            i = c["argv"].index("-s")
            self.assertTrue(c["argv"][i + 1].endswith("jflp-settings-unblock.xml"))
        # a correção de execução não vira "correção desconhecida" no gerador do POM
        self.assertEqual(r.extra["pom_plan"]["warnings"], [])

    def test_jaguar2_reactor_failure_stops(self):
        work = self.tmp / "w"
        (work / "core").mkdir(parents=True)
        (work / "pom.xml").write_text("<project/>")
        te = self.toolenv()
        (Path(te.maven.home) / "bin" / "fake.py").write_text("import sys; sys.exit(1)\n")
        r = run_jaguar2(self.home, self.settings, self._prep(work, "core"), work, AttemptConfig(),
                        self.tmp / "run.log", toolenv=te)
        self.assertEqual(r.extra["fase"], "reactor")
        self.assertEqual(r.rc, 1)
        self.assertFalse((work / "core" / "jaguar2-pom.xml").exists())

    def test_jaguar2_collects_outputs(self):
        work = self.tmp / "w"
        work.mkdir()
        (work / "pom.xml").write_text(POM.format("p"))
        r = run_jaguar2(self.home, self.settings, self._prep(work, "."), work, AttemptConfig(),
                        self.tmp / "run.log", toolenv=self.toolenv())
        self.assertEqual(r.rc, 0)
        self.assertTrue(r.outputs["jaguar2_csv"].is_file())
        self.assertTrue(r.outputs["pom"].is_file())
        self.assertIn("pom_plan", r.extra)
        self.assertIn("-Dmaven.test.failure.ignore=true", r.command)
        self.assertEqual(len(self.read_calls()), 1)

    def test_prefetch_failure_returns_false_and_logs_coordinate(self):
        te = self.toolenv()
        te.env["FAKE_FAIL"] = "surefire-junit4"
        self.assertFalse(prefetch_jaguar2(self.home, self.settings, toolenv=te))
        log = (self.home.work / "prefetch_jaguar2.log").read_text(encoding="utf-8")
        self.assertIn("# jflp falhou: org.apache.maven.surefire:surefire-junit4:3.5.3", log)
        self.assertEqual(len(self.read_calls()), 10)   # continua nas demais

    def test_jaguar2_forces_locale_and_records_phases_with_env(self):
        work = self.tmp / "w"
        (work / "core").mkdir(parents=True)
        (work / "pom.xml").write_text("<project/>")
        (work / "pom.xml").write_text(POM.format("root"))
        (work / "core" / "pom.xml").write_text(POM.format("core"))
        te = self.toolenv()
        te.env["JAVA_HOME"] = "C:/jdk 11"
        r = run_jaguar2(self.home, self.settings, self._prep(work, "core"), work, AttemptConfig(),
                        self.tmp / "run.log", toolenv=te)
        self.assertEqual([p["name"] for p in r.phases], ["reactor", "jaguar2"])
        self.assertEqual(r.phases[1]["env"]["JAVA_TOOL_OPTIONS"], "-Duser.language=en -Duser.country=US")
        self.assertEqual(r.phases[1]["env"]["JAVA_HOME"], "C:/jdk 11")
        self.assertEqual(r.phases[1]["command"], r.command)

    def test_jaguar2_sets_locale_without_existing_java_tool_options(self):
        work = self.tmp / "w"
        work.mkdir()
        (work / "pom.xml").write_text(POM.format("p"))
        te = self.toolenv()
        te.env.pop("JAVA_TOOL_OPTIONS", None)
        run_jaguar2(self.home, self.settings, self._prep(work, "."), work, AttemptConfig(),
                    self.tmp / "run.log", toolenv=te)
        self.assertEqual(self.read_calls()[0]["jto"], "-Duser.language=en -Duser.country=US")

    def test_jaguar2_without_pom_reports_phase(self):
        work = self.tmp / "w"
        work.mkdir()
        r = run_jaguar2(self.home, self.settings, self._prep(work, "."), work, AttemptConfig(),
                        self.tmp / "run.log", toolenv=self.toolenv())
        self.assertIsNone(r.rc)
        self.assertEqual(r.extra["fase"], "pom")
        self.assertTrue(r.extra["erro"])

    def test_prefetch_runs_dependency_get_for_all(self):
        self.assertTrue(prefetch_jaguar2(self.home, self.settings, toolenv=self.toolenv()))
        arts = [a for c in self.read_calls() for a in c["argv"] if a.startswith("-Dartifact=")]
        self.assertEqual(len(arts), 10)
        self.assertIn("-Dartifact=org.apache.maven.surefire:surefire-junit4:3.5.3", arts)
        self.assertIn("-Dartifact=org.apache.maven.plugins:maven-compiler-plugin:3.8.0", arts)
        self.assertIn("-Dartifact=org.jacoco:jacoco-maven-plugin:0.8.7", arts)
        self.assertIn("-Dartifact=org.apache.maven.plugins:maven-surefire-plugin:3.5.3", arts)
        self.assertIn("-Dartifact=junit:junit:4.12", arts)
        self.assertIn("-Dartifact=br.usp.each.saeg:jaguar2-core:0.0.2", arts)

    @unittest.skipUnless(shutil.which("mvn") and shutil.which("java") and _has_jdk11(),
                         "requer mvn, java e JDK 11")
    def test_jaguar2_real_selftest(self):
        work = self.tmp / "pasta com espaço"
        shutil.copytree(DATA / "fixed", work)
        shutil.copy(DATA / "buggy" / "src" / "main" / "java" / "demo" / "Calc.java",
                    work / "src" / "main" / "java" / "demo" / "Calc.java")
        self.settings.set("run", "timeout_s", 1500)
        r = run_jaguar2(self.home, self.settings, self._prep(work, "."), work, AttemptConfig(),
                        self.tmp / "real.log")
        logtxt = r.log.read_text(encoding="utf-8", errors="replace")
        if r.rc != 0 and any(s in logtxt for s in ("UnknownHostException", "Could not transfer",
                                                    "Connection refused", "Temporary failure")):
            self.skipTest("sem rede para o Maven Central")
        self.assertIn("jaguar2_csv", r.outputs, logtxt[-3000:])
        rows = [l.split(",") for l in r.outputs["jaguar2_csv"].read_text(encoding="utf-8").splitlines()]
        self.assertTrue(rows and all(len(x) == 7 for x in rows), rows)   # decimal com ponto, 7 colunas
        self.assertTrue(any(x[0] == "demo/Calc" and x[1] == "6" for x in rows), rows)


class Jaguar1Test(Base):
    def _fake_java(self):
        script = self.tmp / "fakejava.py"
        script.write_text(FAKE_JAVA)
        return str(_wrapper(self.tmp / "fakejava", script))

    def test_jaguar1_uses_existing_loop(self):
        fake = self._fake_java()
        proj = self.tmp / "proj"
        proj.mkdir()
        ctx = Ctx(proj, "target/classes", "target/test-classes", [],
                  ["x.OkTest", "x.CrashTest", "x.FailTest", "x.LastTest"], [])
        out = self.tmp / "out"
        out.mkdir()
        opts = JaguarOptions(lib=self.tmp, java=fake, env=dict(os.environ), max_retries=6)
        meta = run_jaguar(opts, ctx, out)
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(meta["excluded_classes"], ["x.CrashTest"])
        self.assertEqual(len(parse_xml_jaguar1(out / "jaguar_Ochiai.xml")), 1)
        self.assertEqual(meta["failed_tests"], ["x.FailTest#t"])

    def test_run_jaguar1_end_to_end_with_fakes(self):
        lib = self.tmp / "lib"
        lib.mkdir()
        (lib / "jacocoagent.jar").write_bytes(b"jar")
        self.settings.set("jaguar1", "lib", str(lib))
        te = self.toolenv(8, FakeEnv)
        te.fake_java = self._fake_java()
        work = self.tmp / "w"
        work.mkdir()
        prep = Prepared(Bug("local", "p", "1"), work, ".",
                        ["x.OkTest", "x.CrashTest$Inner", "x.FailTest"], [], {})
        r = run_jaguar1(self.home, self.settings, prep, work, AttemptConfig(), self.tmp / "j1.log",
                        toolenv=te)
        self.assertEqual(r.rc, 0, r.extra)
        self.assertTrue(r.outputs["jaguar1_xml"].is_file())
        self.assertEqual(r.extra["excluded_classes"], ["x.CrashTest"])
        self.assertEqual(r.extra["meta_status"], "ok")
        self.assertGreaterEqual(r.extra["attempts"], 2)
        self.assertEqual(r.tests, {"x.OkTest#t": "passed", "x.FailTest#t": "failed"})   # só a última rodada
        argvs = [c["argv"] for c in self.read_calls()]
        self.assertIn("test-compile", argvs[0])
        self.assertIn("dependency:copy-dependencies", argvs[1])
        self.assertIn("# jflp fase: compilar", r.log.read_text(encoding="utf-8"))

    def _j1(self, relevant, scope="relevant", status="ok"):
        """Roda run_jaguar1 com run_jaguar simulado; devolve (resultado, classes passadas ao loop)."""
        lib = self.tmp / "lib"
        lib.mkdir(exist_ok=True)
        (lib / "jacocoagent.jar").write_bytes(b"jar")
        self.settings.set("jaguar1", "lib", str(lib))
        work = self.tmp / "w"
        work.mkdir(exist_ok=True)
        seen = {}

        def fake_loop(opts, ctx, out_dir, heuristic=None):
            seen["classes"] = ctx.test_classes
            seen["out_existed"] = sorted(p.name for p in out_dir.iterdir())
            if status == "ok":
                (out_dir / "jaguar_Ochiai.xml").write_text("<F/>")
            return {"status": status, "excluded_classes": [], "attempts": 1}

        prep = Prepared(Bug("local", "p", "1"), work, ".", relevant, [], {})
        with mock.patch("sbfl.run_jaguar1.run_jaguar", fake_loop),                 mock.patch("sbfl.run_jaguar1.scan_tests", lambda root: (["scan.ATest"], {})):
            r = run_jaguar1(self.home, self.settings, prep, work, AttemptConfig(test_scope=scope),
                            self.tmp / "j1.log", toolenv=self.toolenv(8))
        return r, seen, work

    def test_run_jaguar1_relevant_strips_inner_and_method(self):
        r, seen, _ = self._j1(["a.BTest$Inner", "a.BTest#m", "a.CTest"])
        self.assertEqual(seen["classes"], ["a.BTest", "a.CTest"])

    def test_run_jaguar1_scope_all_scans_even_with_relevant(self):
        r, seen, _ = self._j1(["a.BTest"], scope="all")
        self.assertEqual(seen["classes"], ["scan.ATest"])

    def test_run_jaguar1_empty_relevant_scans(self):
        r, seen, _ = self._j1([])
        self.assertEqual(seen["classes"], ["scan.ATest"])

    def test_run_jaguar1_stale_output_not_reported(self):
        lib = self.tmp / "w" / "target" / "jflp-jaguar1"
        lib.mkdir(parents=True)
        (lib / "jaguar_Ochiai.xml").write_text("<velho/>")
        r, seen, _ = self._j1(["a.BTest"], status="jaguar_crash")
        self.assertEqual(seen["out_existed"], [])        # pasta limpa antes da tentativa
        self.assertNotIn("jaguar1_xml", r.outputs)
        self.assertIsNone(r.rc)
        self.assertEqual(r.extra["meta_status"], "jaguar_crash")

    def test_run_jaguar1_reactor_failure_stops(self):
        lib = self.tmp / "lib"
        lib.mkdir()
        (lib / "jacocoagent.jar").write_bytes(b"jar")
        self.settings.set("jaguar1", "lib", str(lib))
        te = self.toolenv(8)
        te.env["FAKE_FAIL"] = "install"
        work = self.tmp / "w"
        (work / "core").mkdir(parents=True)
        (work / "pom.xml").write_text("<project/>")
        prep = Prepared(Bug("local", "p", "1"), work, "core", None, [], {})
        r = run_jaguar1(self.home, self.settings, prep, work, AttemptConfig(), self.tmp / "j1.log",
                        toolenv=te)
        self.assertEqual(r.extra["fase"], "reactor")
        self.assertEqual(len(self.read_calls()), 1)

    def test_run_jaguar1_missing_lib_does_not_raise(self):
        work = self.tmp / "w"
        work.mkdir()
        prep = Prepared(Bug("local", "p", "1"), work, ".", None, [], {})
        r = run_jaguar1(self.home, self.settings, prep, work, AttemptConfig(), self.tmp / "j1.log",
                        toolenv=self.toolenv(8))
        self.assertIsNone(r.rc)
        self.assertEqual(r.extra["fase"], "jaguar1_lib")
        self.assertIn("Jaguar 1", r.extra["erro"])

    def test_logback_proprio_grava_o_jaguarlogger_em_arquivo(self):
        from sbfl.jaguar import build_cmd, write_logback
        ctx = Ctx(self.tmp, "c", "t", [], [], [])
        cfg = self.tmp / "lb.xml"
        write_logback(cfg, self.tmp / "tests & log.log")
        txt = cfg.read_text(encoding="utf-8")
        self.assertIn('<logger name="JaguarLogger" level="DEBUG">', txt)
        self.assertIn("tests &amp; log.log", txt)
        self.assertEqual(ET.fromstring(txt).tag, "configuration")
        opts = JaguarOptions(lib=self.tmp, java="java")
        flag = f"-Dlogback.configurationFile={cfg.resolve().as_posix()}"
        cmd = build_cmd(opts, ctx, self.tmp / "tf", "o", "Ochiai", cfg)
        self.assertIn(flag, cmd)
        self.assertLess(cmd.index(flag), cmd.index("-cp"))
        self.assertFalse(any("logback" in c for c in build_cmd(opts, ctx, self.tmp / "tf", "o", "Ochiai")))

    def test_run_jaguar1_stdout_cortado_usa_arquivo_e_xml(self):
        """Um teste troca System.out: o stdout para no 1º teste, mas o arquivo do JaguarLogger e o XML têm tudo."""
        script = self.tmp / "cortado.py"
        script.write_text(textwrap.dedent('''\
            import sys, os
            a = sys.argv
            out = a[a.index("-o") + 1]
            lb = [x for x in a if x.startswith("-Dlogback.configurationFile=")]
            assert lb, a
            cfg = open(lb[0].split("=", 1)[1], encoding="utf-8").read()
            f = cfg.split("<file>")[1].split("</file>")[0]
            print("10:00:00.000 [main] DEBUG JaguarLogger - Test t1(x.AllTest) : Passed", flush=True)
            with open(f, "a", encoding="utf-8") as lf:
                for n, st in (("t1", "Passed"), ("t2", "Failed"), ("t3", "Passed")):
                    lf.write("Test %s(x.AllTest) : %s\\n" % (n, st))
            os.makedirs(".jaguar", exist_ok=True)
            p = os.path.abspath(f".jaguar/{out}.xml")
            open(p, "w").write('<F><requirements location="5" cef="1" cep="0" cnf="0" cnp="2" name="a.B" suspicious-value="1.0"/></F>')
            '''))
        lib = self.tmp / "lib"
        lib.mkdir()
        (lib / "jacocoagent.jar").write_bytes(b"jar")
        self.settings.set("jaguar1", "lib", str(lib))
        te = self.toolenv(8, FakeEnv)
        te.fake_java = str(_wrapper(self.tmp / "fakejava2", script))
        work = self.tmp / "w2"
        work.mkdir()
        prep = Prepared(Bug("local", "p", "1"), work, ".", ["x.AllTest"], [], {})
        r = run_jaguar1(self.home, self.settings, prep, work, AttemptConfig(), self.tmp / "j2.log", toolenv=te)
        self.assertEqual(r.rc, 0, r.extra)
        self.assertEqual(r.tests, {"x.AllTest#t1": "passed", "x.AllTest#t2": "failed", "x.AllTest#t3": "passed"})
        self.assertEqual(r.extra["test_counts"], {"total": 3, "passed": 2, "failed": 1, "error": 0, "skipped": 0})
        self.assertFalse(r.extra["log_truncado"])     # o arquivo do JaguarLogger completa o que o stdout perdeu
        self.assertIn("Test t3(x.AllTest) : Passed", r.log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
