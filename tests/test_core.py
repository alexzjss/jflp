import json, os, stat, struct, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sbfl import config as C
from sbfl.adapters.base import Ctx, scan_test_classes
from sbfl.truth import git_truth, patch_truth, tree_truth, crosscheck_classes
from sbfl.jdk import declared_level, pick_home, auto_java_home
from sbfl.build import detect, parse_gradle_info, gradle_path, resolve_tool, split_cmd
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


    # ------------------------------------------------ generalização
    def _repo(self, t):
        g = lambda *a: subprocess.run(["git", "-C", str(t), *a], check=True, capture_output=True, text=True).stdout.strip()
        g("init", "-q"); g("config", "user.email", "a@b"); g("config", "user.name", "x")
        return g

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

    def test_build_helpers(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            self.assertIsNone(detect(t))
            (t / "build.gradle").write_text(""); self.assertEqual(detect(t), "gradle")
            (t / "pom.xml").write_text(""); self.assertEqual(detect(t), "maven")
        info = parse_gradle_info("noise\nSBFL_MAIN=/x/build/classes/java/main\nSBFL_TEST=/x/build/classes/java/test\nSBFL_CP=a.jar:b.jar\n")
        self.assertEqual(info["SBFL_TEST"], "/x/build/classes/java/test")
        self.assertEqual((gradle_path("."), gradle_path("a/b")), ("", ":a:b:"))

    def test_windows_helpers(self):
        self.assertEqual(split_cmd(r"ant -Dx=C:\\a\\b compile", posix=False), ["ant", r"-Dx=C:\\a\\b", "compile"])
        self.assertEqual(resolve_tool("ferramenta-que-nao-existe"), "ferramenta-que-nao-existe")

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

    def test_bugsjar_fetch_root_provenance(self):
        from sbfl.adapters.bugsjar import BugsJar, fetch_repos
        from sbfl.gitutil import add_worktree
        with tempfile.TemporaryDirectory() as t:
            t = Path(t); src = t / "origin" / "maven"; src.mkdir(parents=True); g = self._repo(src)
            f = src / "mod/src/main/java/a/B.java"; f.parent.mkdir(parents=True); f.write_text("l1\nl2\n")
            g("add", "."); g("commit", "-qm", "fix"); fix = g("rev-parse", "--short=8", "HEAD")
            f.write_text("l1\n")
            (src / ".bugs-dot-jar").mkdir(); (src / ".bugs-dot-jar/developer-patch.diff").write_text("x")
            g("add", "."); g("commit", "-qm", "buggy")
            g("branch", f"bugs-dot-jar_MNG-1_{fix}")
            (t / "cfg.toml").write_text(
                f'[paths]\nworkdir="{t.as_posix()}/w"\n[bugsjar]\nroot="{t.as_posix()}/root"\nprojects=["maven"]\n'
                f'url_template="{t.as_posix()}/origin/{{project}}"\n')
            cfg = C.load(t / "cfg.toml")
            self.assertEqual(list(fetch_repos(cfg)), [("maven", "clonado")])
            self.assertEqual(list(fetch_repos(cfg)), [("maven", "atualizado")])
            ad = BugsJar(cfg); bugs = ad.list_bugs()
            self.assertEqual([b.bug_id for b in bugs], [f"bugs-dot-jar_MNG-1_{fix}"])
            self.assertEqual(bugs[0].extra["fixed"], fix)
            wt = t / "wt"; cleanup = add_worktree(Path(bugs[0].extra["repo"]), wt, bugs[0].extra["buggy"])
            prov = ad.provenance(Path(bugs[0].extra["repo"]), bugs[0], wt)
            self.assertEqual(prov, {"developer-patch.diff": "x"})
            self.assertEqual(ad.crosscheck(prov, [FaultLine("a.B", 2, "anchor")]), "unparsable")
            cleanup()
            self.assertEqual(ad.list_bugs(project="outro"), [])

    # ------------------------------------------------ autoteste, triagem, qualidade, guardas
    def _tools(self, t, test_path, test_method, test_cls, xml):
        """mvn e java falsos. O java falso devolve `xml` como relatório do Jaguar."""
        mvn = t / "fakemvn"
        mvn.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import sys, os
            a = sys.argv
            if "--version" in a:
                print("Apache Maven fake"); sys.exit(0)
            mod = a[a.index("-pl") + 1] if "-pl" in a else "."
            if "install" in a:
                base = os.path.join(os.getcwd(), mod, "target")
                os.makedirs(base + "/classes", exist_ok=True)
                tp = base + "/test-classes/" + {test_path!r}
                os.makedirs(os.path.dirname(tp), exist_ok=True)
                open(tp, "wb").write({classfile(0x21)!r} + b"org/junit/Test")
            else:
                os.makedirs("target/dependency", exist_ok=True)
            """))
        java = t / "fakejava"
        java.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import sys, os
            a = sys.argv
            if "-version" in a:
                print("openjdk version fake", file=sys.stderr); sys.exit(0)
            out = a[a.index("-o") + 1]
            print("10:00:00.000 [main] DEBUG JaguarLogger - Test {test_method}({test_cls}) : Failed", flush=True)
            os.makedirs(".jaguar", exist_ok=True)
            p = os.path.abspath(".jaguar/" + out + ".xml")
            open(p, "w").write({xml!r})
            print("10:00:01.000 [main] INFO JaguarLogger - Output xml created at: " + p)
            """))
        for f in (mvn, java):
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        (t / "lib").mkdir(exist_ok=True)
        (t / "lib" / "fake.jar").write_bytes(b"jar")
        (t / "cfg.toml").write_text(f'[paths]\njaguar_lib="{t.as_posix()}/lib"\nworkdir="{t.as_posix()}/w"\nresults="{t.as_posix()}/r"\n'
                                    f'[java]\njava="{java}"\nmvn="{mvn}"\n')
        return C.load(t / "cfg.toml")

    def test_triage(self):
        import gzip
        from sbfl import triage
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)

            def mk(bug, meta, log=None):
                d = t / "bugsjar" / "maven" / bug
                d.mkdir(parents=True)
                (d / "meta.json").write_text(json.dumps(meta))
                if log:
                    with gzip.open(d / "jaguar.log.gz", "wt", encoding="utf-8") as f:
                        f.write(log)

            mk("b1", {"status": "build_failed", "detail": "x"},
               "[ERROR] Blocked mirror for repositories: [x (http://y, default, releases+snapshots)]")
            mk("b2", {"status": "build_failed"}, "[ERROR] Failed to execute goal: Could not find artifact a:b:jar:1.0")
            mk("b3", {"status": "jaguar_crash", "tail": "java.lang.WeirdException 12345 at /tmp/x"})
            mk("b4", {"status": "ok", "quality": ["instrumentation_errors"]})
            mk("b5", {"status": "jaguar_timeout"})
            rep = triage.build_report(t)
            self.assertEqual((rep["total"], rep["ok"]), (5, 1))
            rules = {c["rule"]: c for c in rep["clusters"]}
            self.assertIn("maven_http_blocked", rules)
            self.assertIn("dependency_unresolvable", rules)
            self.assertIn("WeirdException N at <path>", [c["signature"] for c in rep["clusters"]][0:3] + [rules["unclassified"]["signature"]] if False else str(rep["clusters"]))
            self.assertIn("timeout_s", [c for c in rep["clusters"] if c["status"] == "jaguar_timeout"][0]["hint"])
            self.assertEqual(list(rep["flagged"]), ["instrumentation_errors"])
            md = triage.render_markdown(rep)
            self.assertIn("maven_http_blocked", md); self.assertIn("dados duvidosos", md)
            self.assertEqual(triage.classify("COMPILATION ERROR :")[0], "compilation_error")
            self.assertEqual(triage.classify("Unsupported class file major version 61")[0], "unsupported_classfile")
            self.assertEqual(triage.classify("tudo bem"), (None, None))

    def test_jdk_selection(self):
        self.assertEqual(declared_level("<maven.compiler.source>1.7</maven.compiler.source>"), 7)
        self.assertEqual(declared_level("<java.version>11</java.version>"), 11)
        self.assertEqual(declared_level("<maven.compiler.source>${x}</maven.compiler.source><source>1.6</source>"), 6)
        self.assertIsNone(declared_level("<project/>"))
        homes = {"8": "j8", "11": "j11"}
        self.assertEqual((pick_home(6, homes), pick_home(8, homes), pick_home(11, homes), pick_home(17, homes)),
                         ("j8", "j8", "j11", "j11"))
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "cfg.toml").write_text('[java.homes]\n8 = "/j8"\n11 = "/j11"\n')
            cfg = C.load(t / "cfg.toml")
            self.assertEqual(auto_java_home(cfg, [t]), (None, None))      # sem pom
            (t / "pom.xml").write_text("<java.version>1.7</java.version>")
            self.assertEqual(auto_java_home(cfg, [t]), ("/j8", 7))
            (t / "cfg2.toml").write_text('[paths]\n')
            self.assertEqual(auto_java_home(C.load(t / "cfg2.toml"), [t]), (None, None))  # sem [java.homes]

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

    def test_clean(self):
        import contextlib, io
        from sbfl.cli import main
        with tempfile.TemporaryDirectory() as t:
            t = Path(t); (t / "cfg.toml").write_text(f'[paths]\nworkdir="{t.as_posix()}/w"\nresults="{t.as_posix()}/r"\n')
            for n in ("bugsjar/x", "_clones/y", "_selftest/z"):
                (t / "w" / n).mkdir(parents=True); (t / "w" / n / "f.txt").write_text("x")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(main(["-c", str(t / "cfg.toml"), "clean"]), 0)
            self.assertFalse((t / "w/bugsjar").exists())
            self.assertTrue((t / "w/_clones").exists() and (t / "w/_selftest").exists())
            with contextlib.redirect_stdout(buf):
                main(["-c", str(t / "cfg.toml"), "clean", "--all"])
            self.assertEqual(list((t / "w").iterdir()), [])

    # ------------------------------------------------ compare (com e sem Jaguar)
    def test_compare_parsers_and_rank(self):
        import gzip
        from sbfl.compare import compare_tests, jaguar_rank, render
        from sbfl.outputs import parse_jaguar1_log, parse_surefire
        with tempfile.TemporaryDirectory() as t:
            t = Path(t); rep = t / "sf"; rep.mkdir()
            (rep / "TEST-x.FooTest.xml").write_text(
                '<testsuite><testcase classname="x.FooTest" name="a"/>'
                '<testcase classname="x.FooTest" name="b"><failure message="m"/></testcase>'
                '<testcase classname="x.FooTest" name="c"><skipped/></testcase>'
                '<testcase classname="x.FooTest" name="d"><error/></testcase></testsuite>')
            m = parse_surefire(rep)
            self.assertEqual(m, {"x.FooTest#a": "passed", "x.FooTest#b": "failed",
                                 "x.FooTest#c": "skipped", "x.FooTest#d": "error"})
            log = t / "j.log.gz"
            lines = ["# sbfl attempt 1", "10:00 DEBUG JaguarLogger - Test z(x.Crash) : Passed",
                     "# sbfl attempt 2", "10:01 DEBUG JaguarLogger - Test a(x.FooTest) : Passed",
                     "10:01 INFO JaguarLogger - Test b(x.FooTest) : Failed",
                     "10:01 DEBUG JaguarLogger - Test e(x.FooTest) : Passed"]
            with gzip.open(log, "wt") as f:
                f.write("\n".join(lines) + "\n")
            j = parse_jaguar1_log(log)
            self.assertEqual(j, {"x.FooTest#a": "passed", "x.FooTest#b": "failed", "x.FooTest#e": "passed"})  # só a última tentativa
            cmp = compare_tests(m, j)
            self.assertEqual(cmp["only_maven"], ["x.FooTest#d"])        # o skipped não conta como "só no Maven"
            self.assertEqual(cmp["only_jaguar"], ["x.FooTest#e"])
            self.assertEqual(cmp["outcome_differs"], [])
            self.assertEqual(cmp["maven_failing"], ["x.FooTest#b", "x.FooTest#d"])
            xml = t / "r.xml"
            xml.write_text('<F><requirements location="3" cef="1" cep="0" cnf="0" cnp="3" name="a.B" suspicious-value="1.0"/>'
                           '<requirements location="9" cef="1" cep="0" cnf="0" cnp="3" name="a.B" suspicious-value="1.0"/>'
                           '<requirements location="5" cef="1" cep="2" cnf="0" cnp="1" name="a.B" suspicious-value="0.5"/></F>')
            rk = jaguar_rank(xml, [FaultLine("a.B", 9, "removed"), FaultLine("a.B", 77, "removed")])
            self.assertTrue(rk["sorted_desc"]); self.assertEqual(rk["not_covered"], 1)
            row = rk["lines"][0]
            self.assertEqual((row["position_in_file"], row["rank_best"], row["rank_worst"], row["tied"]), (2, 1, 2, 2))
            txt = render("b/p/x", {"excluded_classes": []}, cmp, rk)
            self.assertIn("1–2 de 3", txt); self.assertIn("ordenado", txt)

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
