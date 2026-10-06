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
from sbfl.jaguar import run_jaguar
from sbfl.report import Req, evaluate, parse_xml

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

    def test_evaluate(self):
        reqs = [Req("a.B", 5, 1.0, 10, 0, 0, 20), Req("a.B", 6, 1.0, 10, 0, 0, 20),
                Req("a.C", 1, 0.5, 10, 10, 0, 10), Req("a.C", 2, 0.0, 0, 5, 10, 15)]
        st, info, rows = evaluate(reqs, [FaultLine("a.B", 5, "removed")], ["ochiai", "tarantula", "jaguar"])
        self.assertEqual(st, "ok")
        r = {x["heuristic"]: x for x in rows}["ochiai"]
        self.assertEqual((r["rank_best"], r["rank_avg"], r["rank_worst"]), (1, 1.5, 2))
        self.assertEqual((r["top1"], r["top3"]), (0, 1))  # empate -> pior caso
        self.assertEqual(r["class_rank_worst"], 1)
        # sem teste falhando (o caso do primeiro relatório do chat): não calcula métrica
        z = [Req("a.B", 5, 1.0, 0, 2, 0, 6)]
        self.assertEqual(evaluate(z, [FaultLine("a.B", 5, "removed")], ["ochiai"])[0], "no_failing_tests")
        self.assertEqual(evaluate(reqs, [FaultLine("a.Z", 1, "removed")], ["ochiai"])[0], "fault_not_covered")

    def test_inner_class_matches_outer(self):
        reqs = [Req("a.B$Inner", 7, 1.0, 3, 0, 0, 3)]
        self.assertEqual(evaluate(reqs, [FaultLine("a.B", 7, "removed")], ["ochiai"])[0], "ok")

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

    def test_bears_layout(self):
        from sbfl.adapters.bears import Bears
        with tempfile.TemporaryDirectory() as t:
            t = Path(t); g = self._repo(t)
            f = t / "src/main/java/a/B.java"; f.parent.mkdir(parents=True)
            f.write_text("l1\nl2\nl3\n"); g("add", "."); g("commit", "-qm", "#1 buggy")
            (t / "src/test/java").mkdir(parents=True); (t / "src/test/java/BTest.java").write_text("t\n")
            g("add", "."); g("commit", "-qm", "#2 tests")
            f.write_text("l1\nl2\nl2b\nl3\n"); g("commit", "-qam", "#3 patched")
            (t / "bears.json").write_text("{}"); g("add", "."); g("commit", "-qm", "#4 json")
            g("branch", "INRIA-spoon-100-200")
            (t / "cfg.toml").write_text(f'[bears]\nrepo="{t}"\n[paths]\nworkdir="{t}/w"\n')
            a = Bears(C.load(t / "cfg.toml"))
            bugs = a.list_bugs()
            self.assertEqual([b.bug_id for b in bugs], ["INRIA-spoon-100-200"])
            self.assertEqual(bugs[0].project, "INRIA-spoon")
            e = bugs[0].extra
            a.check_layout(t, bugs[0])  # layout ok, sem exceção
            faults, module = git_truth(t, e["buggy"], e["fixed"])
            self.assertEqual({(x.line, x.kind) for x in faults}, {(2, "anchor"), (3, "anchor")})
            self.assertEqual(module, ".")

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

    def test_manifest(self):
        from sbfl.adapters.manifest import Manifest
        from sbfl.errors import StageError
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "m.jsonl").write_text('# comentario\n{"project":"foo","bug_id":"foo-1","repo":"/r","buggy":"a","fix_patch":"f.diff"}\n')
            (t / "cfg.toml").write_text('[manifest]\nfiles=["m.jsonl"]\n')
            bugs = Manifest(C.load(t / "cfg.toml")).list_bugs()
            self.assertEqual(bugs[0].key, "manifest/foo/foo-1")
            self.assertTrue(bugs[0].extra["fix_patch"].endswith("f.diff"))
            (t / "m.jsonl").write_text('{"project":"foo"}\n')
            with self.assertRaises(StageError):
                Manifest(C.load(t / "cfg.toml")).list_bugs()

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


    def test_end_to_end_with_fakes(self):
        """manifest -> worktree -> truth -> build (mvn falso) -> Jaguar (java falso) -> métricas."""
        from sbfl.adapters.manifest import Manifest
        from sbfl.pipeline import process
        with tempfile.TemporaryDirectory() as t:
            t = Path(t); g = self._repo(t)
            (t / "mod/src/main/java/a").mkdir(parents=True)
            (t / "mod/pom.xml").write_text("<project/>")
            f = t / "mod/src/main/java/a/B.java"; f.write_text("l1\nl2\nl3\n")
            g("add", "."); g("commit", "-qm", "buggy"); buggy = g("rev-parse", "HEAD")
            f.write_text("l1\nl2X\nl3\n"); g("commit", "-qam", "fix"); fixed = g("rev-parse", "HEAD")
            mvn = t / "fakemvn"; mvn.write_text(textwrap.dedent(f'''\
                #!/usr/bin/env python3
                import sys, os
                a = sys.argv
                if "--version" in a: print("Apache Maven fake"); sys.exit(0)
                mod = a[a.index("-pl") + 1] if "-pl" in a else "."
                if "install" in a:
                    base = os.path.join(os.getcwd(), mod, "target")
                    os.makedirs(base + "/classes", exist_ok=True); os.makedirs(base + "/test-classes/x", exist_ok=True)
                    open(base + "/test-classes/x/FooTest.class", "wb").write({classfile(0x21)!r} + b"org/junit/Test")
                else:
                    os.makedirs("target/dependency", exist_ok=True)
            ''')); mvn.chmod(mvn.stat().st_mode | stat.S_IEXEC)
            java = t / "fakejava"; java.write_text(textwrap.dedent('''\
                #!/usr/bin/env python3
                import sys, os
                a = sys.argv
                if "-version" in a: print("openjdk version fake", file=sys.stderr); sys.exit(0)
                out = a[a.index("-o") + 1]
                print("10:00:00.000 [main] DEBUG JaguarLogger - Test t(x.FooTest) : Failed", flush=True)
                os.makedirs(".jaguar", exist_ok=True); p = os.path.abspath(f".jaguar/{out}.xml")
                open(p, "w").write("<F>" + "".join(
                    f'<requirements location="{l}" cef="{ef}" cep="{ep}" cnf="0" cnp="3" name="a.B" suspicious-value="{s}"/>'
                    for l, ef, ep, s in ((1, 1, 3, 0.5), (2, 1, 0, 1.0), (3, 1, 3, 0.5))) + "</F>")
                print(f"10:00:01.000 [main] INFO JaguarLogger - Output xml created at: {p}")
            ''')); java.chmod(java.stat().st_mode | stat.S_IEXEC)
            (t / "m.jsonl").write_text(json.dumps({"project": "p", "bug_id": "p-1", "repo": str(t),
                                                   "buggy": buggy, "fixed": fixed}) + "\n")
            (t / "cfg.toml").write_text(f'[paths]\njaguar_lib="{t}"\nworkdir="{t}/w"\nresults="{t}/r"\n'
                                        f'[java]\njava="{java}"\nmvn="{mvn}"\n[manifest]\nfiles=["m.jsonl"]\n')
            cfg = C.load(t / "cfg.toml")
            ad = Manifest(cfg); bug = ad.list_bugs()[0]
            meta = process(cfg, ad, bug)
            self.assertEqual(meta["status"], "ok", meta)
            rows = json.loads((t / "r/manifest/p/p-1/metrics.json").read_text())
            och = [r for r in rows if r["heuristic"] == "ochiai"][0]
            self.assertEqual((och["rank_best"], och["top1"]), (1, 1))
            self.assertTrue((t / "r/manifest/p/p-1/jaguar.log.gz").exists())
            self.assertFalse((t / "w/manifest/p/p-1").exists())  # worktree limpo
            self.assertEqual(meta["junit5_skipped"], 0)

            # reexecução com subpasta (provenance/) deixada por uma execução anterior
            (t / "r/manifest/p/p-1/provenance").mkdir()
            self.assertEqual(process(cfg, ad, bug, force=True)["status"], "ok")

            # CLI: run (pula), run --retry ok (refaz, com progresso), status e list --summary
            import contextlib, io
            from sbfl.cli import main
            cp = str(t / "cfg.toml"); buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(main(["-c", cp, "run", "--benchmark", "manifest"]), 0)
                self.assertEqual(main(["-c", cp, "run", "--benchmark", "manifest", "--retry", "ok"]), 0)
                self.assertEqual(main(["-c", cp, "status", "--benchmark", "manifest", "--md", str(t / "s.md")]), 0)
                self.assertEqual(main(["-c", cp, "list", "--benchmark", "manifest", "--summary"]), 0)
                self.assertEqual(main(["-c", cp, "inspect", "p-1"]), 0)
            out = buf.getvalue()
            self.assertIn("0 a executar", out)
            self.assertIn("[1/1]", out)
            self.assertIn("manifest/p: 1", out)
            self.assertIn("top 10 por Ochiai", out)
            self.assertTrue((t / "s.md").read_text().startswith("| Projeto"))


    # ------------------------------------------------ campanha Bugs.jar
    def test_campaign_levels_and_summary(self):
        from sbfl import campaign
        self.assertEqual([campaign.level({"status": s}) for s in
                          ("ok", "fault_not_covered", "no_failing_tests", "build_failed")], [3, 2, 1, 0])
        rows = [{"benchmark": "bugsjar", "project": "maven", "bug": f"b{i}", "status": st,
                 "level": campaign.level({"status": st}), "seconds": 100}
                for i, st in enumerate(["ok", "ok", "build_failed", "no_failing_tests"])]
        s = campaign.summarize(rows, {("bugsjar", "maven"): 14})[0]
        self.assertEqual((s["total"], s["done"], s["remaining"]), (14, 4, 10))
        self.assertEqual((s["l1"], s["l2"], s["l3"]), (3, 2, 2))
        self.assertEqual(s["eta_s"], 1000)
        md = campaign.render_markdown([s])
        self.assertIn("bugsjar/maven", md); self.assertIn("`ok` 2", md)
        self.assertEqual(campaign.fmt_dur(3725), "1h02m")

    def test_needs_run_retry(self):
        from sbfl.adapters.base import Bug
        from sbfl.pipeline import needs_run
        with tempfile.TemporaryDirectory() as t:
            t = Path(t); (t / "cfg.toml").write_text(f'[paths]\nresults="{t}/r"\n')
            cfg = C.load(t / "cfg.toml"); bug = Bug("bugsjar", "maven", "b1")
            self.assertTrue(needs_run(cfg, bug))                     # nunca rodou
            d = t / "r/bugsjar/maven/b1"; d.mkdir(parents=True)
            (d / "meta.json").write_text(json.dumps({"status": "build_failed"}))
            self.assertFalse(needs_run(cfg, bug))                    # já feito
            self.assertTrue(needs_run(cfg, bug, retry={"build_failed"}))
            self.assertFalse(needs_run(cfg, bug, retry={"jaguar_crash"}))
            self.assertTrue(needs_run(cfg, bug, force=True))

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
                f'[paths]\nworkdir="{t}/w"\n[bugsjar]\nroot="{t}/root"\nprojects=["maven"]\n'
                f'url_template="{t}/origin/{{project}}"\n')
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

    def test_inspect_render(self):
        from sbfl.inspect import find_dir, render
        with tempfile.TemporaryDirectory() as t:
            t = Path(t); d = t / "r/bugsjar/maven/bug1"; d.mkdir(parents=True)
            (d / "meta.json").write_text(json.dumps({"bug": "bugsjar/maven/bug1", "status": "ok",
                                                     "failed_tests": ["x.FooTest#t"], "attempts": 2}))
            (d / "truth.json").write_text(json.dumps([{"cls": "a.B", "line": 2, "kind": "removed"}]))
            (d / "jaguar_Ochiai.xml").write_text(
                '<F><requirements location="1" cef="1" cep="3" cnf="0" cnp="3" name="a.B" suspicious-value="0.5"/>'
                '<requirements location="2" cef="1" cep="0" cnf="0" cnp="3" name="a.B" suspicious-value="1.0"/></F>')
            self.assertEqual(find_dir(t / "r", "bug1"), d)
            self.assertEqual(find_dir(t / "r", "bugsjar/maven/bug1"), d)
            self.assertIsNone(find_dir(t / "r", "nao-existe"))
            txt = render(d)
            self.assertIn("* 1.0000  a.B:2", txt); self.assertIn("attempts: 2", txt)


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
        (t / "cfg.toml").write_text(f'[paths]\njaguar_lib="{t}/lib"\nworkdir="{t}/w"\nresults="{t}/r"\n'
                                    f'[java]\njava="{java}"\nmvn="{mvn}"\n')
        return C.load(t / "cfg.toml")

    def test_selftest_with_fakes(self):
        import contextlib, io
        from sbfl import selftest
        ln = selftest.bug_line()
        self.assertEqual(ln, 6)
        mk = lambda rows: "<F>" + "".join(
            f'<requirements location="{l}" cef="{ef}" cep="{ep}" cnf="0" cnp="3" name="demo.Calc" '
            f'suspicious-value="{s}"/>' for l, ef, ep, s in rows) + "</F>"
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            cfg = self._tools(t, "demo/CalcTest.class", "testAbsNegative", "demo.CalcTest",
                              mk([(ln - 1, 1, 1, 0.5), (ln, 1, 0, 1.0), (ln + 2, 0, 1, 0.0)]))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = selftest.run(cfg)
            self.assertEqual(rc, 0, buf.getvalue())
            self.assertIn("[OK] linha defeituosa em 1º", buf.getvalue())
        with tempfile.TemporaryDirectory() as t:       # relatório que não cobre a linha: deve FALHAR
            t = Path(t)
            cfg = self._tools(t, "demo/CalcTest.class", "testAbsNegative", "demo.CalcTest", mk([(99, 1, 0, 1.0)]))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = selftest.run(cfg)
            self.assertEqual(rc, 1)
            self.assertIn("FALHOU", buf.getvalue())
            self.assertIn("fault_not_covered", buf.getvalue())

    def test_quality_flags(self):
        from sbfl import campaign
        f = campaign.quality_flags({"n_failed_tests": 194, "n_passed_tests": 65, "analysis_exceptions": 19,
                                    "n_test_classes": 10, "excluded_classes": ["a", "b"],
                                    "truth_crosscheck": "disagree"})
        self.assertEqual(set(f), {"many_failures", "instrumentation_errors", "tests_excluded", "truth_disagrees"})
        self.assertEqual(campaign.quality_flags({"n_failed_tests": 1, "n_passed_tests": 300,
                                                 "n_test_classes": 50, "excluded_classes": ["a"]}), [])
        rows = [{"benchmark": "b", "project": "p", "bug": "1", "status": "ok", "level": 3, "seconds": 1, "quality": []},
                {"benchmark": "b", "project": "p", "bug": "2", "status": "ok", "level": 3, "seconds": 1,
                 "quality": ["many_failures"]}]
        s = campaign.summarize(rows)[0]
        self.assertEqual((s["l3"], s["clean"], s["flags"]), (2, 1, {"many_failures": 1}))
        self.assertIn("`many_failures` 1", campaign.render_markdown([s]))

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

    def test_run_guards_and_env(self):
        import contextlib, io
        from sbfl.cli import main
        from sbfl.envinfo import snapshot
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            cfg = self._tools(t, "x/FooTest.class", "t", "x.FooTest", "<F/>")
            snap = snapshot(cfg)
            self.assertEqual((snap["java"], snap["maven"]), ("openjdk version fake", "Apache Maven fake"))
            self.assertIn("fake.jar", snap["jaguar_jars_sha256_12"])
            entries = [{"project": "p", "bug_id": f"p-{i}", "repo": "/nao/existe", "buggy": "a", "fixed": "b"}
                       for i in range(3)]
            (t / "m.jsonl").write_text("\n".join(json.dumps(e) for e in entries))
            with open(t / "cfg.toml", "a") as f:
                f.write('[manifest]\nfiles=["m.jsonl"]\n')
            cp, out = str(t / "cfg.toml"), io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = main(["-c", cp, "run", "--benchmark", "manifest", "--abort-after", "2",
                           "--notify-cmd", f'echo "$SBFL_SUMMARY" > {t}/notify.txt'])
            self.assertEqual(rc, 3)
            self.assertIn("2 falhas seguidas", out.getvalue())
            self.assertIn("repo_missing", (t / "notify.txt").read_text())
            self.assertTrue((t / "r" / "_env.json").exists())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = main(["-c", cp, "run", "--benchmark", "manifest", "--force", "--max-hours", "0.0000001"])
            self.assertEqual(rc, 0)
            self.assertIn("limite", out.getvalue())

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
            t = Path(t); (t / "cfg.toml").write_text(f'[paths]\nworkdir="{t}/w"\nresults="{t}/r"\n')
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
            (t / "cfg.toml").write_text(f'[paths]\njaguar_lib="{t}"\nresults="{t}/r"\n[java]\njava="{fake}"\n')
            cfg = C.load(t / "cfg.toml")
            proj = t / "proj"; proj.mkdir()
            ctx = Ctx(proj, "target/classes", "target/test-classes", [],
                      ["x.OkTest", "x.CrashTest", "x.FailTest", "x.LastTest"], [])
            out = t / "out"; out.mkdir()
            meta = run_jaguar(cfg, ctx, out)
            self.assertEqual(meta["status"], "ok")
            self.assertEqual(meta["excluded_classes"], ["x.CrashTest"])
            self.assertTrue((out / "jaguar_Ochiai.xml").exists())
            self.assertEqual(len(parse_xml(out / "jaguar_Ochiai.xml")), 1)
            self.assertEqual(meta["failed_tests"], ["x.FailTest#t"])

    def test_aggregate_smoke(self):
        from sbfl.aggregate import aggregate
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            for i, st in enumerate(["ok", "ok", "jaguar_crash"]):
                d = t / "r" / "bugsjar" / "maven" / f"bug{i}"; d.mkdir(parents=True)
                (d / "meta.json").write_text(json.dumps({"status": st}))
                if st == "ok":
                    reqs = [Req("a.B", 5, 1.0, 5, 0, 0, 9), Req("a.B", 6, 0.5, 5, 3, 0, 6), Req("a.C", 1, 0.2, 2, 3, 3, 6)]
                    _, _, rows = evaluate(reqs, [FaultLine("a.B", 5, "removed")], ["ochiai", "tarantula"])
                    (d / "metrics.json").write_text(json.dumps(rows))
            made = aggregate(t / "r", t / "out")
            self.assertTrue(all(p.exists() for p in made))
            self.assertGreaterEqual(len(made), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
