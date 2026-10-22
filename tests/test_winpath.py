"""Caminhos longos no Windows (LongPathsEnabled=0): operações de arquivo além de MAX_PATH."""
import os, shutil, sys, tempfile, unittest, zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sbfl import prepare as prep_mod, store
from sbfl.adapters.base import Bug
from sbfl.home import Home
from sbfl.winpath import long_path


@unittest.skipUnless(os.name == "nt", "caminhos longos só importam no Windows")
class LongPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jflp_lp_"))
        self.addCleanup(lambda: shutil.rmtree(long_path(self.tmp), ignore_errors=True))
        self.home = Home(self.tmp / "home").ensure()
        # árvore cujo caminho absoluto passa de 300 caracteres
        self.src = self.tmp / "pristine"
        deep = self.src
        for i in range(6):
            deep = deep / f"diretorio_bem_comprido_numero_{i}_{'x' * 20}"
        self.deep = deep
        os.makedirs(long_path(deep))
        self.fname = "Arquivo.java"
        with open(long_path(deep / self.fname), "w", encoding="utf-8") as f:
            f.write("class A {}")
        with open(long_path(self.src / prep_mod.MARKER), "w") as f:
            f.write("{}")
        self.assertGreater(len(str(deep / self.fname)), 300)

    def _rel(self, root: Path) -> Path:
        return Path(*self.deep.relative_to(self.src).parts) / self.fname

    def test_long_path_forms(self):
        bs = chr(92)
        pre = bs * 2 + "?" + bs
        self.assertEqual(long_path("C:" + bs + "a" + bs + "b"), pre + "C:" + bs + "a" + bs + "b")
        self.assertEqual(long_path(bs * 2 + "srv" + bs + "share" + bs + "x"),
                         pre + "UNC" + bs + "srv" + bs + "share" + bs + "x")
        already = pre + "C:" + bs + "a"
        self.assertEqual(long_path(already), already)

    def test_working_copy_and_rmtree(self):
        prep = prep_mod.Prepared(Bug("local", "p", "x"), self.src, ".", None, [], {})
        dest = prep_mod.working_copy(prep, self.home)
        self.assertTrue(os.path.isfile(long_path(dest / self._rel(dest))))
        self.assertFalse(os.path.exists(long_path(dest / prep_mod.MARKER)))
        prep_mod._rmtree(dest)
        self.assertFalse(os.path.exists(long_path(dest)))

    def test_copy_into_archive_zip_and_lock(self):
        bug = Bug("local", "p", "x")
        w = store.RunWriter(self.home, bug, "maven")
        w.copy_into(self.src, "outputs/pristine")
        self.assertTrue(os.path.isfile(long_path(w.dir / "outputs" / "pristine" / self._rel(w.dir))))
        with mock.patch.dict(sys.modules, {"zstandard": None}):
            name = w.archive_checkout(self.src)
        self.assertEqual(name, "checkout.zip")
        with zipfile.ZipFile(long_path(w.dir / name)) as zf:
            names = zf.namelist()
        self.assertIn(self._rel(self.src).as_posix(), names)
        self.assertTrue(all(not n.startswith("\\?") and ":" not in n for n in names))
        final = w.finish({"status": "ok"})
        copied = final / "outputs" / "pristine" / self._rel(final)
        self.assertFalse(os.access(long_path(copied), os.W_OK))
        store.make_writable(final)
        self.assertTrue(os.access(long_path(copied), os.W_OK))


if __name__ == "__main__":
    unittest.main()
