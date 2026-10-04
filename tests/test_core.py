import json, os, stat, struct, subprocess, sys, tempfile, textwrap, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sbfl import config as C
from sbfl.adapters.base import Ctx, scan_test_classes
from sbfl.truth import git_truth, patch_truth, tree_truth
from sbfl.build import detect, parse_gradle_info, gradle_path
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
                a = sys.argv; mod = a[a.index("-pl") + 1] if "-pl" in a else "."
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
                a = sys.argv; out = a[a.index("-o") + 1]
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
