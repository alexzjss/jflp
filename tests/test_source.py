from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from sbfl import source


def make_zip(d: Path, files: dict[str, bytes | str]) -> Path:
    p = d / "checkout.zip"
    with zipfile.ZipFile(p, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return p


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        source._cache.clear()

    def test_prefers_main_over_test_and_module(self):
        make_zip(self.tmp, {
            "a/src/test/java/p/Foo.java": "class Foo { /* teste a */ }",
            "a/src/main/java/p/Foo.java": "class Foo { /* main a */ }",
            "b/src/main/java/p/Foo.java": "class Foo { /* main b */ }",
            "b/src/test/java/p/Foo.java": "class Foo { /* teste b */ }",
        })
        _, lines, _ = source.load_source(self.tmp, "p.Foo", "b")
        self.assertIn("main b", lines[0])
        _, lines, _ = source.load_source(self.tmp, "p.Foo", "a")
        self.assertIn("main a", lines[0])
        _, lines, _ = source.load_source(self.tmp, "p.Foo")           # sem módulo: main, ordem estável
        self.assertIn("main a", lines[0])

    def test_only_test_file_is_used_and_inner_class_maps_to_outer(self):
        make_zip(self.tmp, {"x/src/test/java/p/Bar.java": "class Bar {}", "./y/p/Baz.java": "class Baz {}"})
        member, _, _ = source.load_source(self.tmp, "p.Bar$Inner$1")
        self.assertEqual(member, "x/src/test/java/p/Bar.java")
        self.assertEqual(source.load_source(self.tmp, "p.Baz")[0], "y/p/Baz.java")

    def test_not_found_and_suffix_must_align_on_slash(self):
        make_zip(self.tmp, {"src/main/java/p/XFoo.java": "class XFoo {}"})
        with self.assertRaises(source.SourceError) as cm:
            source.load_source(self.tmp, "p.Foo")
        self.assertIn("não foi encontrada", str(cm.exception))

    def test_missing_checkout(self):
        with self.assertRaises(source.SourceError) as cm:
            source.load_source(self.tmp, "p.Foo")
        self.assertIn("checkout", str(cm.exception))

    def test_large_file_truncated(self):
        text = "\n".join(f"int v{i};" for i in range(source.MAX_LINES + 50))
        make_zip(self.tmp, {"p/Big.java": text})
        _, lines, truncated = source.load_source(self.tmp, "p.Big")
        self.assertTrue(truncated)
        self.assertEqual(len(lines), source.MAX_LINES)

    def test_non_utf8_is_replaced(self):
        make_zip(self.tmp, {"p/Lat.java": b"// caf\xe9\nclass Lat {}"})
        _, lines, truncated = source.load_source(self.tmp, "p.Lat")
        self.assertFalse(truncated)
        self.assertIn("caf�", lines[0])

    def test_tar_zst_without_zstandard_gives_clear_error(self):
        (self.tmp / "checkout.tar.zst").write_bytes(b"\x28\xb5\x2f\xfd")
        with mock.patch.dict(sys.modules, {"zstandard": None}):
            with self.assertRaises(source.SourceError) as cm:
                source.load_source(self.tmp, "p.Foo")
        self.assertIn("zstandard", str(cm.exception))

    def test_tar_zst_roundtrip_when_installed(self):
        try:
            import zstandard
            import tarfile
        except ImportError:
            self.skipTest("zstandard não instalado")
        src = self.tmp / "src"
        (src / "p").mkdir(parents=True)
        (src / "p" / "Foo.java").write_text("class Foo {}", encoding="utf-8")
        with open(self.tmp / "checkout.tar.zst", "wb") as raw:
            with zstandard.ZstdCompressor().stream_writer(raw) as zw:
                with tarfile.open(fileobj=zw, mode="w|") as tar:
                    tar.add(src, arcname=".")
        member, lines, _ = source.load_source(self.tmp, "p.Foo")
        self.assertEqual((member, lines), ("p/Foo.java", ["class Foo {}"]))

    def test_index_cache_is_lru_by_mtime(self):
        p = make_zip(self.tmp, {"p/A.java": "class A {}"})
        with mock.patch.object(source, "_list_members", wraps=source._list_members) as lm:
            source.java_members(p)
            source.java_members(p)
            self.assertEqual(lm.call_count, 1)
            make_zip(self.tmp, {"p/A.java": "class A {}", "p/B.java": "class B {}"})   # muda tamanho/mtime
            self.assertIn("p/B.java", source.java_members(p))
            self.assertEqual(lm.call_count, 2)
        for i in range(source.CACHE_SIZE + 2):
            d = self.tmp / f"r{i}"
            d.mkdir()
            source.java_members(make_zip(d, {"p/A.java": ""}))
        self.assertLessEqual(len(source._cache), source.CACHE_SIZE)


if __name__ == "__main__":
    unittest.main()
