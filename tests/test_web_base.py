from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi.testclient import TestClient
    from sbfl.web.app import create_app
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

from sbfl import queue as jq
from sbfl.adapters.base import Bug
from sbfl.home import Home, load_settings

TOKEN = "tok-de-teste"
BASE = "http://127.0.0.1:8123"


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class WebBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Home(self.tmp / "home").ensure()
        self.settings = load_settings(self.home)
        worker = mock.Mock()
        worker.status.return_value = {"alive": True, "last_error": None, "current": None}
        self.worker = worker
        self.app = create_app(self.home, self.settings, worker, TOKEN, 8123)
        self.client = TestClient(self.app, base_url=BASE)

    def authed(self) -> TestClient:
        c = TestClient(self.app, base_url=BASE)
        c.cookies.set("jflp_token", TOKEN)
        return c

    def no_tools(self):
        wsl = mock.Mock(installed=False, distros=[], d4j=None, message="WSL indisponível")
        return [mock.patch("sbfl.web.routes_env.find_jdks", return_value=[]),
                mock.patch("sbfl.web.routes_env.find_mavens", return_value=[]),
                mock.patch("sbfl.web.routes_env.wsl_status", return_value=wsl),
                mock.patch("sbfl.web.routes_env._git", return_value=None),
                mock.patch("sbfl.toolchain.profiles.find_jdks", return_value=[]),
                mock.patch("sbfl.toolchain.profiles.find_mavens", return_value=[])]

    def test_requires_token(self):
        self.assertEqual(self.client.get("/").status_code, 403)
        self.assertEqual(self.client.get("/ambiente").status_code, 403)
        self.assertEqual(self.client.get("/?t=errado").status_code, 403)

    def test_token_sets_cookie(self):
        r = self.client.get(f"/?t={TOKEN}", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/")
        cookie = r.headers["set-cookie"]
        self.assertIn(f"jflp_token={TOKEN}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        page = self.client.get("/")   # o cliente guardou o cookie
        self.assertEqual(page.status_code, 200)
        self.assertIn("Painel", page.text)

    def test_bad_host_rejected(self):
        c = self.authed()
        self.assertEqual(c.get("/", headers={"host": "evil.example.com"}).status_code, 403)
        self.assertEqual(c.get("/", headers={"host": "127.0.0.1:9999"}).status_code, 403)
        self.assertEqual(c.get("/", headers={"host": "localhost:8123"}).status_code, 200)

    def test_foreign_origin_post_rejected(self):
        c = self.authed()
        r = c.post("/ambiente/apontar", data={"kind": "jdk", "path": str(self.tmp)},
                   headers={"origin": "http://evil.example.com"})
        self.assertEqual(r.status_code, 403)

    def test_painel_shows_queue_and_error(self):
        self.worker.status.return_value = {"alive": True, "last_error": "RuntimeError('boom')",
                                           "current": None}
        jq.create_batch(self.home, "t", [Bug("bugsjar", "commons-lang", "7")], ["maven"], {})
        r = self.authed().get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("bugsjar/commons-lang/7", r.text)
        self.assertIn("boom", r.text)

    def test_ambiente_renders_without_tools(self):
        patches = self.no_tools()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        r = self.authed().get("/ambiente")
        self.assertEqual(r.status_code, 200)
        self.assertIn("não encontrado", r.text)
        self.assertIn("baixe na tela Ambiente", r.text)

    def test_plano_shows_unknown_size_and_checksum(self):
        from sbfl.toolchain.download import Download
        d = Download("Maven 3.9.9", "https://x/y.zip", None, None, None, True, self.tmp / "m")
        with mock.patch("sbfl.web.routes_env.plan_maven", return_value=d):
            r = self.authed().get("/ambiente/plano?tipo=maven&v=3.9.9")
        self.assertIn("https://x/y.zip", r.text)
        self.assertIn("desconhecido", r.text)
        self.assertIn("não publicado", r.text)
        bad = self.authed().get("/ambiente/plano?tipo=maven&v=1.0")
        self.assertIn("não oferecida", bad.text)

    def test_download_runs_in_thread_with_progress(self):
        from sbfl.toolchain.download import Download
        d = Download("Maven 3.9.9", "https://x/y.zip", None, None, None, True, self.tmp / "m")

        def fake_fetch(dl, progress=None):
            progress(50, 100)
            return dl.dest

        c = self.authed()
        with mock.patch("sbfl.web.routes_env.plan_maven", return_value=d), \
                mock.patch("sbfl.web.routes_env.fetch", side_effect=fake_fetch):
            r = c.post("/ambiente/baixar", data={"tipo": "maven", "v": "3.9.9"})
            self.assertEqual(r.status_code, 200)
            tid = next(iter(self.app.state.tasks))
            for _ in range(100):
                if self.app.state.tasks[tid]["done"]:
                    break
                time.sleep(0.05)
        task = self.app.state.tasks[tid]
        self.assertTrue(task["done"])
        self.assertIsNone(task["error"])
        self.assertEqual(task["bytes"], 50)
        self.assertIn("Concluído", c.get(f"/ambiente/progresso/{tid}").text)

    def test_apontar_invalid_and_valid(self):
        c = self.authed()
        empty = self.tmp / "vazia"
        empty.mkdir()
        patches = self.no_tools()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        r = c.post("/ambiente/apontar", data={"kind": "jdk", "path": str(empty)})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Não é um JDK válido", r.text)
        lib = self.tmp / "lib"
        lib.mkdir()
        (lib / "jacocoagent.jar").write_bytes(b"x")
        r = c.post("/ambiente/apontar", data={"kind": "jaguar1", "path": str(lib)})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(load_settings(self.home).get("jaguar1", "lib"), str(lib))
        with mock.patch("sbfl.web.routes_env.java_info", return_value=(11, "11.0.2")):
            r = c.post("/ambiente/apontar", data={"kind": "jdk", "path": str(empty)})
        self.assertEqual(load_settings(self.home).get("tools", "jdks"), {"11": str(empty)})


if __name__ == "__main__":
    unittest.main()
