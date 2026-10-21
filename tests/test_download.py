from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from sbfl.home import Home
from sbfl.toolchain import download as dl


class _Resp(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _router(routes: dict[str, bytes]):
    def fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if url not in routes:
            raise AssertionError(f"URL inesperada: {url}")
        return _Resp(routes[url])
    return fake


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = Home(self.root / "home")

    def _dl(self, data: bytes, **kw) -> dl.Download:
        base = dict(name="x", url="https://example.test/a.zip", size=len(data),
                    checksum=hashlib.sha256(data).hexdigest(), algo="sha256",
                    archive=True, dest=self.home.tools / "apache-maven-3.9.9")
        base.update(kw)
        return dl.Download(**base)

    def test_plan_jdk_parses_adoptium(self):
        payload = [{"release_name": "jdk-11.0.24+8", "binary": {"package": {
            "link": "https://github.com/adoptium/jdk.zip", "size": 123, "checksum": "ab" * 32}}}]
        mock_open = mock.Mock(return_value=_Resp(json.dumps(payload).encode()))
        with mock.patch("urllib.request.urlopen", mock_open):
            d = dl.plan_jdk(self.home, 11, os_name="windows", arch="x64")
        self.assertEqual(d.url, "https://github.com/adoptium/jdk.zip")
        self.assertEqual(d.size, 123)
        self.assertEqual(d.checksum, "ab" * 32)
        self.assertEqual(d.algo, "sha256")
        self.assertTrue(d.archive)
        self.assertEqual(d.dest, self.home.tools / "jdk-jdk-11.0.24+8")
        asked = mock_open.call_args[0][0].full_url
        self.assertEqual(asked, "https://api.adoptium.net/v3/assets/latest/11/hotspot"
                                "?os=windows&architecture=x64&image_type=jdk&vendor=eclipse")

    def test_plan_maven_urls(self):
        base = "https://archive.apache.org/dist/maven/maven-3/3.9.9/binaries/apache-maven-3.9.9-bin.zip"
        sha = "cd" * 64
        fake = _router({base + ".sha512": f"{sha}  apache-maven-3.9.9-bin.zip\n".encode()})
        with mock.patch("urllib.request.urlopen", fake):
            d = dl.plan_maven(self.home, "3.9.9")
        self.assertEqual(d.url, base)
        self.assertEqual((d.checksum, d.algo), (sha, "sha512"))
        self.assertEqual(d.dest, self.home.tools / "apache-maven-3.9.9")

    def test_plan_maven_hash_alone(self):
        base = "https://archive.apache.org/dist/maven/maven-3/3.6.3/binaries/apache-maven-3.6.3-bin.zip"
        with mock.patch("urllib.request.urlopen", _router({base + ".sha512": b"ee" * 64 + b"\n"})):
            d = dl.plan_maven(self.home, "3.6.3")
        self.assertEqual(d.checksum, "ee" * 64)

    def test_plan_jdk_without_checksum_fails_closed(self):
        payload = [{"release_name": "jdk-11.0.24+8", "binary": {"package": {
            "link": "https://github.com/adoptium/jdk.zip", "size": 123}}}]
        with mock.patch("urllib.request.urlopen", mock.Mock(return_value=_Resp(json.dumps(payload).encode()))):
            with self.assertRaises(dl.DownloadError) as cm:
                dl.plan_jdk(self.home, 11, os_name="windows", arch="x64")
        self.assertIn("checksum", str(cm.exception).lower())

    def test_fetch_without_checksum_fails_before_touching_the_network(self):
        d = dl.Download("sem", "https://example.test/x.jar", None, None, None, False, self.home.tools / "l")
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("rede")):
            with self.assertRaises(dl.DownloadError) as cm:
                dl.fetch(d)
        self.assertIn("checksum", str(cm.exception).lower())
        self.assertFalse(d.dest.exists())

    def test_plan_jaguar1_pins_sha256(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("rede")):
            ds = dl.plan_jaguar1(self.home)
        for d in ds:
            self.assertEqual(d.algo, "sha256")
            self.assertRegex(d.checksum, r"^[0-9a-f]{64}$")
        self.assertEqual(len({d.checksum for d in ds}), 2)

    def test_jaguar1_jar_with_wrong_digest_is_rejected(self):
        d = dl.plan_jaguar1(self.home)[1]
        with mock.patch("urllib.request.urlopen", _router({d.url: b"jar adulterado"})):
            with self.assertRaises(dl.DownloadError) as cm:
                dl.fetch(d)
        self.assertIn("checksum", str(cm.exception).lower())
        self.assertFalse((d.dest / "jacocoagent.jar").exists())

    def test_plan_jaguar1_no_network(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("rede")):
            ds = dl.plan_jaguar1(self.home)
        self.assertEqual(len(ds), 2)
        for d in ds:
            self.assertFalse(d.archive)
            self.assertIsNone(d.size)
            self.assertEqual(d.dest, self.home.tools / "jaguar1/lib")
        self.assertTrue(ds[1].url.endswith("/jaguar-cli-0.0.1/jacocoagent.jar"))

    def test_fetch_checksum_mismatch(self):
        data = _zip_bytes({"apache-maven-3.9.9/bin/mvn": b"x"})
        d = self._dl(data, checksum="0" * 64)
        with mock.patch("urllib.request.urlopen", _router({d.url: data})):
            with self.assertRaises(dl.DownloadError) as cm:
                dl.fetch(d)
        self.assertIn("checksum", str(cm.exception).lower())
        self.assertFalse(d.dest.exists())
        dldir = self.home.tools / "_dl"
        self.assertEqual(list(dldir.glob("*")) if dldir.exists() else [], [])

    def test_fetch_network_error_cleans_up(self):
        d = self._dl(b"abc")
        with mock.patch("urllib.request.urlopen", side_effect=OSError("sem rede")):
            with self.assertRaises(dl.DownloadError):
                dl.fetch(d)
        self.assertFalse(d.dest.exists())

    def test_fetch_extraction_error_cleans_up(self):
        data = _zip_bytes({"../evil.txt": b"x"})
        d = self._dl(data)
        with mock.patch("urllib.request.urlopen", _router({d.url: data})):
            with self.assertRaises(dl.DownloadError):
                dl.fetch(d)
        self.assertFalse(d.dest.exists())
        self.assertEqual([p.name for p in self.home.tools.iterdir()], ["_dl"])
        self.assertEqual(list((self.home.tools / "_dl").iterdir()), [])

    def test_safe_extract_rejects_traversal(self):
        z = self.root / "evil.zip"
        z.write_bytes(_zip_bytes({"../evil.txt": b"x"}))
        out = self.root / "out"
        out.mkdir()
        with self.assertRaises(dl.DownloadError):
            dl.safe_extract(z, out)
        self.assertFalse((self.root / "evil.txt").exists())
        self.assertEqual(list(out.iterdir()), [])

    def test_safe_extract_rejects_absolute_zip(self):
        z = self.root / "abs.zip"
        z.write_bytes(_zip_bytes({"/abs.txt": b"x"}))
        out = self.root / "out"
        out.mkdir()
        with self.assertRaises(dl.DownloadError):
            dl.safe_extract(z, out)

    def test_safe_extract_tar_rejects_escaping_symlink(self):
        t = self.root / "l.tar.gz"
        with tarfile.open(t, "w:gz") as tf:
            info = tarfile.TarInfo("r/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../outside"
            tf.addfile(info)
        out = self.root / "out"
        out.mkdir()
        with self.assertRaises(dl.DownloadError):
            dl.safe_extract(t, out)

    def test_safe_extract_tar_rejects_traversal(self):
        t = self.root / "t.tar.gz"
        with tarfile.open(t, "w:gz") as tf:
            info = tarfile.TarInfo("../evil.txt")
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
        out = self.root / "out"
        out.mkdir()
        with self.assertRaises(dl.DownloadError):
            dl.safe_extract(t, out)
        self.assertFalse((self.root / "evil.txt").exists())

    def test_fetch_zip_single_root(self):
        data = _zip_bytes({"apache-maven-3.9.9/bin/mvn": b"#!/bin/sh"})
        d = self._dl(data)
        seen = []
        with mock.patch("urllib.request.urlopen", _router({d.url: data})):
            out = dl.fetch(d, progress=lambda a, b: seen.append((a, b)))
        self.assertEqual(out, d.dest)
        self.assertTrue((d.dest / "bin" / "mvn").exists())
        self.assertEqual(seen[-1], (len(data), len(data)))
        names = sorted(p.name for p in self.home.tools.iterdir() if p.name != "_dl")
        self.assertEqual(names, [d.dest.name])

    def test_fetch_targz(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("jdk-11/bin/java")
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
        data = buf.getvalue()
        d = self._dl(data, url="https://example.test/j.tar.gz", dest=self.home.tools / "jdk-11")
        with mock.patch("urllib.request.urlopen", _router({d.url: data})):
            dl.fetch(d)
        self.assertTrue((d.dest / "bin" / "java").exists())

    def test_fetch_plain_file(self):
        data = b"jar-bytes"
        d = dl.Download("jaguar", "https://example.test/x/jacocoagent.jar", None,
                        hashlib.sha256(data).hexdigest(), "sha256", False, self.home.tools / "jaguar1/lib")
        with mock.patch("urllib.request.urlopen", _router({d.url: data})):
            out = dl.fetch(d)
        self.assertEqual(out, d.dest)
        self.assertEqual((d.dest / "jacocoagent.jar").read_bytes(), data)
        self.assertEqual([p.name for p in d.dest.iterdir()], ["jacocoagent.jar"])


if __name__ == "__main__":
    unittest.main()
