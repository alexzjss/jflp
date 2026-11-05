"""Famílias de falha do Jaguar 2 com correção própria: diagnóstico -> sugestão -> flags/POM."""
import tempfile
import unittest
from pathlib import Path

from sbfl import status as st
from sbfl.diagnose import classify
from sbfl.home import Home, load_settings
from sbfl.run_jaguar2 import reactor_toolenv, with_javac7
from sbfl.run_maven import AttemptConfig, AttemptResult, reactor_flags
from sbfl.toolchain.detect import Jdk
from sbfl.toolchain.profiles import MissingTool

LOG_SOURCE5 = """\
[INFO] --- compiler:3.15.0:compile (default-compile) @ wicket ---
[ERROR] COMPILATION ERROR :
[INFO] -------------------------------------------------------------
[ERROR] Source option 5 is no longer supported. Use 6 or later.
[ERROR] Target option 1.5 is no longer supported. Use 1.6 or later.
[INFO] 2 errors
[INFO] BUILD FAILURE
[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.15.0:compile (default-compile)
"""
LOG_INFERENCIA = """\
[ERROR] COMPILATION ERROR :
[ERROR] /E:/w/wicket/src/main/java/org/apache/wicket/util/value/ValueMap.java:[772,35] incompatible types: inference variable T has incompatible upper bounds java.lang.Enum<T>,T
[INFO] 1 error
[INFO] BUILD FAILURE
"""
LOG_VOID = """\
[ERROR] COMPILATION ERROR :
[ERROR] /E:/w/flink-java/src/main/java/A.java:[31,12] incompatible types: void cannot be converted to java.lang.Object
[INFO] BUILD FAILURE
"""
LOG_ACCESS = """\
[ERROR] COMPILATION ERROR :
[ERROR] \\w\\camel-core\\src\\main\\java\\JmxMBeanAssembler.java:[45,22] error: cannot access InitializingBean
[INFO] BUILD FAILURE
"""
LOG_ASM = """\
[INFO] Running org.apache.logging.log4j.core.appender.AsyncAppenderTest
[INFO] Tests run: 2, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.342 s
[ERROR] class org.jacoco.core.internal.flow.ClassProbesVisitor has interface org.objectweb.asm.ClassVisitor as super class
[INFO] Running org.apache.logging.log4j.core.appender.ConsoleAppenderTest
[ERROR] class org.jacoco.core.internal.flow.ClassProbesVisitor has interface org.objectweb.asm.ClassVisitor as super class
[ERROR] The forked VM terminated without saying properly goodbye.
"""
LOG_RAT = r"""[INFO] --- apache-rat:0.10:check (default) @ flink-parent ---
[INFO] Rat check: Summary of files. Unapproved: 1 unknown: 1 generated: 0 approved: 2472 licence.
[ERROR] Failed to execute goal org.apache.rat:apache-rat-plugin:0.10:check (default) on project flink-parent: Too many files with unapproved license: 1 See RAT report in: E:\w\target\rat.txt -> [Help 1]
"""
LOG_TOOLS = r"""[ERROR] Failed to execute goal on project flink-java: Could not resolve dependencies for project org.apache.flink:flink-java:jar:0.9-SNAPSHOT
[ERROR] dependency: jdk.tools:jdk.tools:jar:1.6 (system)
[ERROR] 	Could not find artifact jdk.tools:jdk.tools:jar:1.6 at specified path C:\Program Files\Java\jdk-11.0.32.1/../lib/tools.jar
"""
LOG_SCR = r"""[INFO] --- scr:1.9.0:scr (generate-scr-scrdescriptor) @ oak-mk-api ---
[INFO] BUILD FAILURE
[ERROR] Failed to execute goal org.apache.felix:maven-scr-plugin:1.9.0:scr (generate-scr-scrdescriptor) on project oak-mk-api: Execution generate-scr-scrdescriptor of goal org.apache.felix:maven-scr-plugin:1.9.0:scr failed: org.apache.felix.scrplugin.annotations.AnnotationProcessor is not an ImageIO SPI class -> [Help 1]
"""
LOG_EXEC = r"""[INFO] --- exec:1.2.1:exec (generate-thrift) @ accumulo-trace ---
[ERROR] Failed to execute goal org.codehaus.mojo:exec-maven-plugin:1.2.1:exec (generate-thrift) on project accumulo-trace: Command execution failed. Cannot run program "E:\w\trace\src\main\scripts\generate-thrift.sh" (in directory "E:\w\trace"): CreateProcess error=193 -> [Help 1]
"""
LOG_SORTPOM = r"""[INFO] --- sortpom:2.1.0:verify (verify-sorted-pom) @ accumulo-core ---
[ERROR] The file E:\w\core\jaguar2-pom.xml is not sorted
[ERROR] Failed to execute goal com.google.code.sortpom:maven-sortpom-plugin:2.1.0:verify (verify-sorted-pom) on project accumulo-core: The file E:\w\core\jaguar2-pom.xml is not sorted -> [Help 1]
"""
LOG_JAVAX = r"""[ERROR] COMPILATION ERROR :
[ERROR] \w\camel-core\src\main\java\org\apache\camel\ExchangePattern.java:[22,32] error: package javax.xml.bind.annotation does not exist
[INFO] BUILD FAILURE
"""


def res(tmp, log, **kw):
    lp = Path(tmp) / "log.txt"
    lp.write_text(log, encoding="utf-8")
    return AttemptResult(kw.pop("rc", 1), False, 1.0, lp, kw.pop("outputs", {}), kw.pop("tests", {}),
                         extra=kw.pop("extra", {}))


class DiagnosticoFamilias(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.tmp = self._t.name

    def diag(self, log, kind="jaguar2", **kw):
        return classify(kind, res(self.tmp, log, **kw))

    def fixes(self, d):
        return [(s.fix, s.jdk) for s in d.suggestions]

    def test_source_option_5_sugere_javac7_no_jdk8(self):
        d = self.diag(LOG_SOURCE5, extra={"fase": "reactor"})
        self.assertEqual(d.status, st.JDK_INCOMPATIVEL)      # e não "compilacao_falhou" genérico
        self.assertEqual(self.fixes(d)[0], ("javac7", 8))
        self.assertIn((None, 8), self.fixes(d))

    def test_inferencia_e_void_sao_compilacao_falhou_com_javac7(self):
        for log in (LOG_INFERENCIA, LOG_VOID, LOG_ACCESS):
            d = self.diag(log, extra={"fase": "reactor"})
            self.assertEqual(d.status, st.COMPILACAO_FALHOU, log)
            self.assertEqual(self.fixes(d), [("javac7", 8)], log)
            self.assertIn("javac 8/11", d.cause)

    def test_erro_de_compilacao_comum_continua_generico(self):
        d = self.diag("[ERROR] COMPILATION ERROR :\n[ERROR] A.java:[3,1] cannot find symbol\n[INFO] BUILD FAILURE\n")
        self.assertEqual(d.status, st.COMPILACAO_FALHOU)
        self.assertNotIn("javac7", [s.fix for s in d.suggestions])

    def test_javac7_so_para_jaguar2(self):
        d = self.diag(LOG_INFERENCIA, kind="maven")
        self.assertNotIn("javac7", [s.fix for s in d.suggestions])

    def test_asm_antigo_diagnostica_e_sugere_asm_shadow(self):
        d = self.diag(LOG_ASM, rc=1)
        self.assertEqual(d.status, st.JAGUAR_SEM_SAIDA)
        self.assertIn("ASM antigo", d.cause)
        self.assertEqual([s.fix for s in d.suggestions], ["asm-shadow"])
        self.assertTrue(any("ClassProbesVisitor" in e for e in d.evidence))

    def test_asm_antigo_com_csv_vazio(self):
        csv = Path(self.tmp) / "jaguar2.csv"
        csv.write_text("", encoding="utf-8")       # relatório vazio: o Jaguar 2 não analisou nenhuma classe
        d = self.diag(LOG_ASM, rc=0, outputs={"jaguar2_csv": csv}, tests={"T": "failed"})
        self.assertEqual(d.status, st.JAGUAR_SEM_SAIDA)
        self.assertEqual([s.fix for s in d.suggestions], ["asm-shadow"])

    def test_asm_antigo_so_vale_para_jaguar2(self):
        d = self.diag(LOG_ASM, kind="maven")
        self.assertNotIn("asm-shadow", [s.fix for s in d.suggestions])

    def test_rat_010_sugere_flags_da_linha_de_comando(self):
        d = self.diag(LOG_RAT, extra={"fase": "reactor"})
        self.assertEqual(d.status, st.PLUGIN_INCOMPATIVEL)
        self.assertEqual([s.fix for s in d.suggestions], ["rat-flags", "skip-rat"])

    def test_jdk_tools_pede_jdk8(self):
        d = self.diag(LOG_TOOLS, extra={"fase": "reactor"})
        self.assertEqual(d.status, st.JDK_INCOMPATIVEL)
        self.assertIn("JDK 8", d.cause)
        self.assertEqual(self.fixes(d), [("javac7", 8), (None, 8)])      # não cai em "dependência não resolvida"

    def test_scr_sugere_scr_jdk7(self):
        d = self.diag(LOG_SCR, extra={"fase": "reactor"})
        self.assertEqual(d.status, st.PLUGIN_INCOMPATIVEL)
        self.assertEqual(self.fixes(d), [("scr-jdk7", 8)])

    def test_exec_sh_no_windows(self):
        d = self.diag(LOG_EXEC, extra={"fase": "reactor"})
        self.assertEqual(d.status, st.PLUGIN_INCOMPATIVEL)
        self.assertEqual([s.fix for s in d.suggestions], ["exec-skip"])

    def test_sortpom_reprova_o_pom_gerado(self):
        d = self.diag(LOG_SORTPOM)
        self.assertEqual(d.status, st.PLUGIN_INCOMPATIVEL)
        self.assertEqual([s.fix for s in d.suggestions], ["sortpom-warn"])

    def test_pacotes_java_ee_no_jdk11(self):
        d = self.diag(LOG_JAVAX)
        self.assertEqual(d.status, st.JDK_INCOMPATIVEL)
        self.assertEqual(self.fixes(d), [("javac7", 8), (None, 8)])


class FlagsDoReactor(unittest.TestCase):
    def test_params_e_correcoes_ligadas(self):
        cfg = AttemptConfig(fixes_on=frozenset({"javac7", "outra"}),
                            params={"reactor_flags": ["-Da=1"],
                                    "javac7": {"reactor_flags": ["-Db=2", "-Da=1"]},
                                    "nao-ligada": {"reactor_flags": ["-Dx=9"]}})
        self.assertEqual(reactor_flags(cfg), ["-Da=1", "-Db=2"])

    def test_vazio(self):
        self.assertEqual(reactor_flags(AttemptConfig()), [])

    def test_flags_padrao_das_correcoes_de_linha_de_comando(self):
        cfg = AttemptConfig(fixes_on=frozenset({"rat-flags", "exec-skip", "sortpom-warn", "skip-rat"}))
        self.assertIn("-Drat.ignoreErrors=true", reactor_flags(cfg))
        self.assertIn("-Dskip=true", reactor_flags(cfg))
        self.assertIn("-Dsort.verifyFail=warn", reactor_flags(cfg))

    def test_sugestao_pode_trazer_flags_proprios(self):
        cfg = AttemptConfig(fixes_on=frozenset({"rat-flags"}), params={"rat-flags": {"reactor_flags": ["-Dx=1"]}})
        self.assertEqual(reactor_flags(cfg), ["-Dx=1"])


class ComJavac7(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)
        self.home = Home(self.root / "home")
        self.settings = load_settings(self.home)

    def test_sem_a_correcao_nada_muda(self):
        cfg = AttemptConfig()
        self.assertIs(with_javac7(self.home, self.settings, cfg), cfg)

    def test_sem_jdk7_levanta_missing_tool(self):
        cfg = AttemptConfig(fixes_on=frozenset({"javac7"}))
        from unittest import mock
        with mock.patch("sbfl.toolchain.profiles.find_jdks", return_value=[]):
            with self.assertRaises(MissingTool):
                with_javac7(self.home, self.settings, cfg)

    def test_com_jdk7_preenche_executavel_e_flags(self):
        jdk = self.root / "jdk7"
        (jdk / "bin").mkdir(parents=True)
        for name in ("javac.exe", "javac"):
            (jdk / "bin" / name).write_text("")
        cfg = AttemptConfig(fixes_on=frozenset({"javac7"}), params={"cobertura": True})
        from unittest import mock
        with mock.patch("sbfl.toolchain.profiles.find_jdks",
                        return_value=[Jdk(jdk, 7, "1.7.0_352", "config"), Jdk(self.root, 8, "8", "system")]):
            out = with_javac7(self.home, self.settings, cfg)
        exe = out.params["javac7"]["executable"]
        self.assertTrue(exe.endswith(("javac.exe", "javac")))
        flags = reactor_flags(out)
        self.assertEqual(flags[0], "-Dmaven.compiler.fork=true")
        self.assertIn("-Dcompiler.fork=true", flags)
        self.assertEqual(flags[-1], f"-Dmaven.compiler.executable={exe}")
        self.assertTrue(out.params["cobertura"])
        self.assertNotIn("executable", cfg.params.get("javac7", {}))     # a configuração original não é alterada

    def test_scr_jdk7_liga_javac7_no_pom_gerado_sem_flags_no_reactor(self):
        jdk = self.root / "jdk7"
        (jdk / "bin").mkdir(parents=True)
        for name in ("javac.exe", "javac"):
            (jdk / "bin" / name).write_text("")
        cfg = AttemptConfig(fixes_on=frozenset({"scr-jdk7"}))
        from unittest import mock
        with mock.patch("sbfl.toolchain.profiles.find_jdks", return_value=[Jdk(jdk, 7, "1.7.0_352", "config")]):
            out = with_javac7(self.home, self.settings, cfg)
        self.assertEqual(out.fixes_on, frozenset({"scr-jdk7", "javac7"}))
        self.assertIn("executable", out.params["javac7"])
        self.assertEqual(reactor_flags(out), [])
        self.assertEqual(cfg.fixes_on, frozenset({"scr-jdk7"}))


class ReactorNoJdk7(unittest.TestCase):
    def test_sem_a_correcao_usa_o_mesmo_ambiente(self):
        te = object()
        self.assertIs(reactor_toolenv(None, None, te, AttemptConfig(), Path(".")), te)

    def test_scr_jdk7_resolve_jdk7_maven_363_e_tls12(self):
        from types import SimpleNamespace
        from unittest import mock
        r7 = SimpleNamespace(env={"MAVEN_OPTS": "-Xmx1g"})
        cfg = AttemptConfig(fixes_on=frozenset({"scr-jdk7"}))
        with mock.patch("sbfl.run_jaguar2.resolve", return_value=r7) as rs:
            out = reactor_toolenv("h", "s", object(), cfg, Path("m"))
        rs.assert_called_once_with("maven", "h", "s", pom_dir=Path("m"), jdk=7, maven="3.6.3")
        self.assertEqual(out.env["MAVEN_OPTS"], "-Xmx1g -Dhttps.protocols=TLSv1.2")


if __name__ == "__main__":
    unittest.main()
