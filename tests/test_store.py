import json, os, shutil, sys, tempfile, unittest, zipfile
from datetime import datetime as real_datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sbfl import store
from sbfl.adapters.base import Bug
from sbfl.home import Home


def _rm(func, path, exc_info):
    store.make_writable(path)
    func(path)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jflp_store_"))
        self.home = Home(self.tmp).ensure()
        self.bug = Bug("bugsjar", "commons-lang", "Bug:1*")

    def tearDown(self):
        shutil.rmtree(self.tmp, onerror=_rm)

    def test_finish_atomic_and_readonly(self):
        w = store.RunWriter(self.home, self.bug, "maven")
        self.assertTrue(w.dir.name.endswith(".partial"))
        w.write_text("log.txt", "oi")
        final = w.finish({"status": "ok", "tests": {"total": 5, "failed": 1, "error": 1}})
        self.assertTrue(final.is_dir())
        self.assertFalse(w.dir.name.endswith(".partial"))
        self.assertEqual(list(final.parent.glob(".*.partial")), [])
        self.assertFalse(os.access(final / "run.json", os.W_OK))
        self.assertFalse(os.access(final / "log.txt", os.W_OK))
        run = store.load_run(self.home, w.rel)
        self.assertEqual(run["bug"], "bugsjar/commons-lang/Bug:1*")
        self.assertEqual(run["bug_id"], "Bug:1*")
        row = store.list_runs(self.home, kind="maven")[0]
        self.assertEqual((row["n_tests"], row["n_failed"], row["status"]), (5, 2, "ok"))
        with self.assertRaises(RuntimeError):
            w.finish({"status": "ok"})

    def test_never_overwrites(self):
        with mock.patch("sbfl.store.datetime") as dt:
            dt.now.return_value = real_datetime(2026, 1, 2, 3, 4, 5)
            a = store.RunWriter(self.home, self.bug, "maven")
            b = store.RunWriter(self.home, self.bug, "maven")
            self.assertNotEqual(a.dir, b.dir)
            a.finish({"status": "ok"})
            c = store.RunWriter(self.home, self.bug, "maven")
            b.finish({"status": "ok"})
            c.finish({"status": "ok"})
        self.assertEqual(len({a.rel, b.rel, c.rel}), 3)

    def test_exception_marks_desconhecido(self):
        with self.assertRaises(ValueError):
            with store.RunWriter(self.home, self.bug, "jaguar2") as w:
                raise ValueError("boom")
        run = store.load_run(self.home, w.rel)
        self.assertEqual(run["status"], "desconhecido")
        self.assertIn("boom", run["error"])

    def test_reindex_matches(self):
        for st in ("ok", "timeout"):
            with store.RunWriter(self.home, self.bug, "maven") as w:
                w.attempt_dir(1)
                w.finish({"status": st})
            w2 = store.RunWriter(self.home, Bug("d4j", "Lang", "1"), "jaguar1", parent=w.rel)
            w2.finish({"status": st})
        before = store.list_runs(self.home)
        self.assertEqual(len(before), 4)
        self.home.db_path.unlink()
        self.assertEqual(store.reindex(self.home), len(before))
        self.assertEqual(store.list_runs(self.home), before)

    def test_recover_partials_marks_interrompido(self):
        w = store.RunWriter(self.home, self.bug, "maven")
        w.write_text("a.txt", "x")
        rels = store.recover_partials(self.home)
        self.assertEqual(len(rels), 1)
        self.assertEqual(list(self.home.runs.rglob("*.partial")), [])
        rows = store.list_runs(self.home)
        self.assertEqual([r["status"] for r in rows], ["interrompido"])
        self.assertEqual(rows[0]["kind"], "maven")
        self.assertEqual(store.load_run(self.home, rels[0])["status"], "interrompido")

    def test_archive_checkout_zip_fallback(self):
        src = self.tmp / "src"
        (src / "target").mkdir(parents=True)
        (src / "A.java").write_text("class A{}")
        (src / "target" / "A.class").write_bytes(b"\xca\xfe")
        w = store.RunWriter(self.home, self.bug, "maven")
        with mock.patch.dict(sys.modules, {"zstandard": None}):
            name = w.archive_checkout(src)
        self.assertEqual(name, "checkout.zip")
        with zipfile.ZipFile(w.dir / name) as zf:
            self.assertEqual(sorted(zf.namelist()), ["A.java", "target/A.class"])
        w.finish({"status": "ok"})

    def test_zip_fallback_accepts_pre_1980_files(self):
        src = self.tmp / "src"
        src.mkdir()
        old = src / "old.class"
        old.write_bytes(b"x")
        os.utime(old, (0, 0))
        w = store.RunWriter(self.home, self.bug, "maven")
        with mock.patch.dict(sys.modules, {"zstandard": None}):
            name = w.archive_checkout(src)
        with zipfile.ZipFile(w.dir / name) as zf:
            self.assertEqual(zf.namelist(), ["old.class"])
        w.finish({"status": "ok"})

    def test_path_components_sanitized(self):
        self.assertEqual(store._safe("owner/repo"), "owner_repo")
        self.assertEqual(store._safe("a\\b"), "a_b")
        self.assertEqual(store._safe(".."), "_")
        self.assertEqual(store._safe("."), "_")
        self.assertEqual(store._safe(".hidden"), "_hidden")      # nunca parece uma .partial
        self.assertEqual(store._safe("..x"), "__x")
        self.assertEqual(store._safe("a.b"), "a.b")
        for name in ("CON", "nul", "Com1", "lpt9.txt", "aux.x.y"):
            self.assertEqual(store._safe(name), "_" + name)
        self.assertEqual(store._safe("console"), "console")
        w = store.RunWriter(self.home, Bug("b", "p", "owner/repo"), "maven")
        self.assertEqual(w.rel.split("/")[:3], ["b", "p", "owner_repo"])
        w.finish({"status": "ok"})
        self.assertEqual(store.load_run(self.home, w.rel)["bug"], "b/p/owner/repo")
        # recover/reindex continuam achando tudo em 3 níveis
        w2 = store.RunWriter(self.home, Bug("b", "p", "x/y"), "maven")
        self.assertEqual(len(store.recover_partials(self.home)), 1)
        self.assertEqual(store.reindex(self.home), 2)

    def test_recover_keeps_complete_run_json(self):
        w = store.RunWriter(self.home, self.bug, "maven")
        doc = {"kind": "maven", "bug": self.bug.key, "status": "ok", "started": w.started}
        (w.dir / "run.json").write_text(json.dumps(doc), encoding="utf-8")
        rels = store.recover_partials(self.home)
        self.assertEqual(store.load_run(self.home, rels[0])["status"], "ok")
        self.assertEqual(store.list_runs(self.home)[0]["status"], "ok")
        self.assertFalse(os.access(run_json := store.run_dir(self.home, rels[0]) / "run.json", os.W_OK))
        self.assertFalse((run_json.parent / ".writer.json").exists())

    def test_recover_uses_manifest(self):
        w = store.RunWriter(self.home, Bug("b", "p", "id:1"), "jaguar1", parent="b/p/id_1/old")
        rels = store.recover_partials(self.home)
        run = store.load_run(self.home, rels[0])
        self.assertEqual((run["status"], run["bug"], run["kind"], run["parent"], run["started"]),
                         ("interrompido", "b/p/id:1", "jaguar1", "b/p/id_1/old", w.started))
        self.assertFalse(os.access(store.run_dir(self.home, rels[0]) / "run.json", os.W_OK))

    def test_reindex_ignores_nested_run_json(self):
        w = store.RunWriter(self.home, self.bug, "maven")
        w.write_text("checkout/run.json", "{}")
        w.finish({"status": "ok"})
        self.assertEqual(store.reindex(self.home), 1)


if __name__ == "__main__":
    unittest.main()
