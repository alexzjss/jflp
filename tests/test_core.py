import json, os, stat, struct, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sbfl.adapters.base import Ctx, scan_test_classes
from sbfl.truth import git_truth, patch_truth, tree_truth, crosscheck_classes
from sbfl.jdk import declared_level, pick_home
from sbfl.adapters.base import junit_flavor
from sbfl.classfile import is_abstract_or_interface
from sbfl.diffgt import FaultLine, faults_from_hunks, fqcn_from_path, parse_left_hunks, select_lines
from sbfl.jaguar import JaguarOptions, run_jaguar
from sbfl.outputs import Req, outer, parse_xml_jaguar1
from sbfl import status as S

DIFF = """diff --git a/m/src/main/java/a/B.java b/m/src/main/java/a/B.java
index 1..2 100644
--- a/m/src/main/java/a/B.java
+++ b/m/src/main/java/a/B.java
@@ -10,0 +11,3 @@ foo
+x
+y
+z
@@ -20,2 +24,1 @@ bar
-old1
-old2
+new
"""


def classfile(flags):
    # classe mínima válida só até access_flags (suficiente para o parser)
    cp = b"\x01\x00\x01A"  # 1 entrada Utf8 -> cp_count=2
    return b"\xca\xfe\xba\xbe" + struct.pack(">HHH", 0, 52, 2) + cp + struct.pack(">H", flags)


class T(unittest.TestCase):
    def test_hunks_and_truth(self):
        h = parse_left_hunks(DIFF, lambda p: p)
        self.assertEqual(h["m/src/main/java/a/B.java"], [(10, 0), (20, 2)])
        f = faults_from_hunks(h, fqcn_from_path)
        self.assertIn(FaultLine("a.B", 10, "anchor"), f)
        self.assertIn(FaultLine("a.B", 20, "removed"), f)
        self.assertTrue(all(x.kind == "removed" for x in select_lines(f)))

    def test_pure_addition_uses_anchors(self):
        h = parse_left_hunks(DIFF.split("@@ -20")[0], lambda p: p)
        f = select_lines(faults_from_hunks(h, fqcn_from_path))
        self.assertEqual({(x.line, x.kind) for x in f}, {(10, "anchor"), (11, "anchor")})

    def test_classfile(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "p").mkdir()
            (t / "p" / "FooTest.class").write_bytes(classfile(0x21))
            (t / "p" / "AbsTest.class").write_bytes(classfile(0x421))
            (t / "p" / "Helper.class").write_bytes(classfile(0x21))
            self.assertTrue(is_abstract_or_interface(t / "p" / "AbsTest.class"))
            self.assertFalse(is_abstract_or_interface(t / "p" / "FooTest.class"))
            self.assertEqual(scan_test_classes(t), ["p.FooTest"])

    def test_bugsjar_truth_with_git(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            g = lambda *a: subprocess.run(["git", "-C", str(t), *a], check=True, capture_output=True, text=True).stdout.strip()
            g("init", "-q"); g("config", "user.email", "a@b"); g("config", "user.name", "x")
            f = t / "mod/src/main/java/a/B.java"; f.parent.mkdir(parents=True)
            f.write_text("l1\nl2\nl3\nl4\n"); g("add", "."); g("commit", "-qm", "fix")
            fix = g("rev-parse", "HEAD")
            f.write_text("l1\nl2\nl4\n"); g("commit", "-qam", "reverse patch (buggy)")  # buggy removeu l3
            faults, module = git_truth(t, "HEAD", fix)
            self.assertEqual(module, "mod")
            # fix ADICIONA l3 depois da linha 2 do buggy -> âncoras 2 e 3
            self.assertEqual({(x.cls, x.line, x.kind) for x in faults},
                             {("a.B", 2, "anchor"), ("a.B", 3, "anchor")})


    def test_patch_and_tree_truth(self):
        faults, module = patch_truth_text = None, None
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "p.diff").write_text(DIFF)
            faults, module = patch_truth(t / "p.diff")
            self.assertEqual(module, "m")
            self.assertIn(FaultLine("a.B", 20, "removed"), faults)
            a, b = t / "a", t / "b"
            for d, body in ((a, "x\ny\nz\n"), (b, "x\nY\nz\n")):
                (d / "core/src/main/java/p").mkdir(parents=True); (d / "core/src/main/java/p/K.java").write_text(body)
                (d / "core/src/test/java/p").mkdir(parents=True); (d / "core/src/test/java/p/KTest.java").write_text(body + "!" if d == b else body)
            faults, module = tree_truth(a, b)
            self.assertEqual(module, "core")
            self.assertEqual([(x.cls, x.line, x.kind) for x in faults], [("p.K", 2, "removed")])

    def test_junit_flavor_and_scan(self):
        from sbfl.adapters.base import scan_tests
        self.assertEqual(junit_flavor(b"..org/junit/jupiter/api/Test.."), "junit5")
        self.assertEqual(junit_flavor(b"org/junit/jupiter/ org/junit/Test"), "junit4")
        self.assertEqual(junit_flavor(b"junit/framework/TestCase"), "junit3")
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "AJupiterTest.class").write_bytes(classfile(0x21) + b"org/junit/jupiter/api/Test")
            (t / "BOldTest.class").write_bytes(classfile(0x21) + b"org/junit/Test")
            classes, st = scan_tests(t)
            self.assertEqual(classes, ["BOldTest"])
            self.assertEqual(st["junit5_skipped"], 1)


    # ------------------------------------------------ campanha Bugs.jar
    def test_crosscheck(self):
        f = [FaultLine("a.B", 20, "removed")]
        self.assertEqual(crosscheck_classes(None, f), "absent")
        self.assertEqual(crosscheck_classes("lixo", f), "unparsable")
        self.assertEqual(crosscheck_classes(DIFF.replace("a/m/", "a/m/").replace("B.java", "B.java"), f), "agree")
        other = DIFF.replace("a/B.java", "a/Z.java")
        self.assertEqual(crosscheck_classes(other, f), "disagree")

    # ------------------------------------------------ autoteste, triagem, qualidade, guardas
    def test_jdk_selection(self):
        self.assertEqual(declared_level("<maven.compiler.source>1.7</maven.compiler.source>"), 7)
        self.assertEqual(declared_level("<java.version>11</java.version>"), 11)
        self.assertEqual(declared_level("<maven.compiler.source>${x}</maven.compiler.source><source>1.6</source>"), 6)
        self.assertIsNone(declared_level("<project/>"))
        homes = {"8": "j8", "11": "j11"}
        self.assertEqual((pick_home(6, homes), pick_home(8, homes), pick_home(11, homes), pick_home(17, homes)),
                         ("j8", "j8", "j11", "j11"))

    def test_keep_awake_and_envcheck(self):
        import subprocess as sp
        from sbfl.power import keep_awake
        from sbfl.envcheck import java_major, maven_version, missing_jaguar_options, advice
        if os.name != "nt" and sys.platform == "linux":   # `systemd-inhibit` falso: confere que o processo é encerrado
            with tempfile.TemporaryDirectory() as t:
                t = Path(t); fake = t / "systemd-inhibit"
                fake.write_text(f"#!/bin/sh\necho $$ > {t}/pid\nexec sleep 1000\n")
                fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
                old_path = os.environ["PATH"]; os.environ["PATH"] = f"{t}:{old_path}"
                try:
                    with keep_awake():
                        for _ in range(50):
                            if (t / "pid").exists() and (t / "pid").read_text().strip():
                                break
                            time.sleep(0.05)
                        pid = int((t / "pid").read_text())
                        os.kill(pid, 0)                  # está vivo durante o bloco
                finally:
                    os.environ["PATH"] = old_path
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)                      # e morto depois
        else:
            with keep_awake():
                pass
        self.assertEqual((java_major('java version "1.8.0_202"'), java_major('openjdk version "21.0.10"')), (8, 21))
        self.assertIsNone(java_major("???"))
        self.assertEqual(maven_version("Apache Maven 3.6.3 (cecedd3)"), (3, 6, 3))
        self.assertEqual(missing_jaguar_options("classesDir heuristic logLevel output outputType projectDir testsDir"),
                         ["testsListFile"])
        lv = [l for l, _ in advice('openjdk version "21.0.10"', "Apache Maven 3.9.6", "", "x" * 60, 5)]
        self.assertEqual(lv, ["warn", "warn", "warn", "warn", "warn"])  # java>8, maven>=3.8.1, sem help, caminho longo, pouco disco
        levels = [l for l, _ in advice('java version "1.8.0"', "Apache Maven 3.6.3",
                                       "classesDir heuristic logLevel output outputType projectDir testsDir testsListFile",
                                       "C:/w", 100)]
        self.assertEqual(levels, ["ok", "ok", "ok"])

    def test_crash_recovery_loop(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            fake = t / "fakejava"
            fake.write_text(textwrap.dedent('''\
                #!/usr/bin/env python3
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
            '''))
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            if os.name == "nt":   # sem shebang no Windows: um .cmd chama o script com o Python atual
                wrapper = t / "fakejava.cmd"
                wrapper.write_text(f'@"{sys.executable}" "{fake}" %*\r\n')
                fake = wrapper
            opts = JaguarOptions(lib=t, java=str(fake), env=dict(os.environ), max_retries=6)
            proj = t / "proj"; proj.mkdir()
            ctx = Ctx(proj, "target/classes", "target/test-classes", [],
                      ["x.OkTest", "x.CrashTest", "x.FailTest", "x.LastTest"], [])
            out = t / "out"; out.mkdir()
            meta = run_jaguar(opts, ctx, out)
            self.assertEqual(meta["status"], "ok")
            self.assertEqual(meta["excluded_classes"], ["x.CrashTest"])
            self.assertTrue((out / "jaguar_Ochiai.xml").exists())
            self.assertEqual(len(parse_xml_jaguar1(out / "jaguar_Ochiai.xml")), 1)
            self.assertEqual(meta["failed_tests"], ["x.FailTest#t"])
    def test_status_constants(self):
        self.assertEqual((S.OK, S.OK_TEST_FAIL), ("ok", "ok_testes_falhando"))
        self.assertEqual(set(S.INFRA), {
            "checkout_falhou", "jdk_incompativel", "dependencia_nao_resolvida", "plugin_incompativel",
            "compilacao_falhou", "classes_duplicadas", "testes_nao_encontrados", "jaguar_sem_saida",
            "jaguar_crash", "timeout", "interrompido", "desconhecido"})
        self.assertEqual(len(S.INFRA), 12)
        self.assertTrue(S.is_success("ok") and S.is_success("ok_testes_falhando"))
        self.assertFalse(any(S.is_success(x) for x in S.INFRA))
        self.assertFalse(S.is_success(""))

    def test_parse_xml_jaguar1(self):
        with tempfile.TemporaryDirectory() as t:
            x = Path(t) / "r.xml"
            x.write_text('<F><requirements location="7" cef="3" cep="1" cnf="0" cnp="4" name="a.B$Inner" '
                         'suspicious-value="0.75"/></F>')
            self.assertEqual(parse_xml_jaguar1(x), [Req("a.B$Inner", 7, 0.75, 3, 1, 0, 4)])
        self.assertEqual(outer("a.B$Inner"), "a.B")
        self.assertEqual(outer("a.B"), "a.B")

    def test_selftest_data(self):
        from sbfl import selftest
        self.assertEqual(selftest.bug_line(), 6)
        with tempfile.TemporaryDirectory() as t:
            buggy, fixed = selftest.build_repo(Path(t) / "repo")
            self.assertNotEqual(buggy, fixed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
