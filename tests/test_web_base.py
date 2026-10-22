from __future__ import annotations

import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi.testclient import TestClient
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

from sbfl.home import Home, load_settings
from tests.web_helpers import BASE, TOKEN, WebFixture


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class WebBase(WebFixture, unittest.TestCase):
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
        self.assertIn("Rodar", page.text)

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

    def test_ambiente_renders_without_tools(self):
        patches = self.no_tools()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        r = self.authed().get("/ambiente")
        self.assertEqual(r.status_code, 200)
        for nome in ("Maven puro", "Jaguar 1", "Jaguar 2"):
            self.assertIn(f"<h2>{nome}</h2>", r.text)
        self.assertIn("✗ falta JDK 11", r.text)            # Jaguar 2 precisa de JDK 11
        self.assertIn("Baixar JDK 11", r.text)
        self.assertIn("não encontrado", r.text)            # detalhes continuam no <details>
        self.assertIn("<details", r.text)
        self.assertIn("<summary>Detalhes</summary>", r.text)

    def test_ambiente_cartao_pronto_sem_botao_de_download(self):
        from sbfl.toolchain.detect import Jdk, Maven
        jdks = [Jdk(self.tmp / "j8", 8, "1.8", "config"), Jdk(self.tmp / "j11", 11, "11", "config")]
        mavens = [Maven(self.tmp / "m369", (3, 6, 9), "config"), Maven(self.tmp / "m399", (3, 9, 9), "config")]
        lib = self.tmp / "lib"
        lib.mkdir()
        (lib / "jacocoagent.jar").write_bytes(b"x")
        self.settings.set("jaguar1", "lib", str(lib))
        with mock.patch("sbfl.web.routes_env.find_jdks", return_value=jdks),              mock.patch("sbfl.web.routes_env.find_mavens", return_value=mavens),              mock.patch("sbfl.toolchain.profiles.find_jdks", return_value=jdks),              mock.patch("sbfl.toolchain.profiles.find_mavens", return_value=mavens),              mock.patch("sbfl.web.routes_env.wsl_status",
                        return_value=mock.Mock(installed=False, distros=[], d4j=None, message="x")),              mock.patch("sbfl.web.routes_env._git", return_value=None):
            r = self.authed().get("/ambiente")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text.count('class="perfil pronto"'), 3)
        self.assertNotIn("Baixar", r.text.split('<div id="dialogo">')[0])

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
        (empty / "release").write_text("x")
        with mock.patch("sbfl.web.routes_env.java_info", return_value=(11, "11.0.2")):
            r = c.post("/ambiente/apontar", data={"kind": "jdk", "path": str(empty)})
        self.assertEqual(load_settings(self.home).get("tools", "jdks"), {"11": str(empty)})

    def test_non_ascii_token_denied_not_500(self):
        self.assertEqual(self.client.get("/?t=%C3%A9").status_code, 403)
        c = TestClient(self.app, base_url=BASE)
        r = c.get("/", headers={"cookie": "jflp_token=é".encode("utf-8")})
        self.assertEqual(r.status_code, 403)

    def test_unauthenticated_requests_denied(self):
        for method, path in [("get", "/static/htmx.min.js"), ("get", "/ambiente/progresso/x"),
                             ("post", "/ambiente/baixar"),
                             ("post", "/ambiente/prefetch"), ("post", "/ambiente/apontar")]:
            with self.subTest(method=method, path=path):
                self.assertEqual(getattr(self.client, method)(path).status_code, 403)

    def test_same_origin_without_cookie_denied(self):
        r = self.client.post("/ambiente/apontar", data={"kind": "jdk", "path": "x"},
                             headers={"origin": BASE})
        self.assertEqual(r.status_code, 403)

    def test_apontar_requires_absolute_and_markers(self):
        c = self.authed()
        for p in self.no_tools():
            p.start()
            self.addCleanup(p.stop)
        r = c.post("/ambiente/apontar", data={"kind": "jdk", "path": "relativo"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("absoluto", r.text)
        fake = self.tmp / "fakejdk"
        (fake / "bin").mkdir(parents=True)
        with mock.patch("sbfl.web.routes_env.java_info") as ji:
            r = c.post("/ambiente/apontar", data={"kind": "jdk", "path": str(fake)})
            self.assertEqual(r.status_code, 400)
            ji.assert_not_called()   # sem marcadores estruturais, nada é executado
        mv = self.tmp / "fakemvn"
        (mv / "bin").mkdir(parents=True)
        with mock.patch("sbfl.web.routes_env.maven_info") as mi:
            r = c.post("/ambiente/apontar", data={"kind": "maven", "path": str(mv)})
            self.assertEqual(r.status_code, 400)
            mi.assert_not_called()
        (fake / "release").write_text("x")
        with mock.patch("sbfl.web.routes_env.java_info", return_value=(8, "1.8")) as ji:
            r = c.post("/ambiente/apontar", data={"kind": "jdk", "path": str(fake)})
            self.assertEqual(r.status_code, 200)
            ji.assert_called_once()

    def test_duplicate_download_reuses_task(self):
        import threading
        from sbfl.toolchain.download import Download
        d = Download("Maven 3.9.9", "https://x/y.zip", None, None, None, True, self.tmp / "m")
        gate = threading.Event()
        calls = []

        def slow_fetch(dl, progress=None):
            calls.append(1)
            gate.wait(5)
            return dl.dest

        c = self.authed()
        with mock.patch("sbfl.web.routes_env.plan_maven", return_value=d),                 mock.patch("sbfl.web.routes_env.fetch", side_effect=slow_fetch):
            c.post("/ambiente/baixar", data={"tipo": "maven", "v": "3.9.9"})
            c.post("/ambiente/baixar", data={"tipo": "maven", "v": "3.9.9"})
            self.assertEqual(len(self.app.state.tasks), 1)
            gate.set()
            tid = next(iter(self.app.state.tasks))
            for _ in range(100):
                if self.app.state.tasks[tid]["done"]:
                    break
                time.sleep(0.05)
        self.assertEqual(len(calls), 1)

    def test_duplicate_prefetch_reuses_task(self):
        import threading
        gate = threading.Event()
        c = self.authed()
        with mock.patch("sbfl.web.routes_env.prefetch_jaguar2",
                        side_effect=lambda h, s: gate.wait(5) or True) as pf:
            c.post("/ambiente/prefetch")
            c.post("/ambiente/prefetch")
            self.assertEqual(len(self.app.state.tasks), 1)
            gate.set()
            tid = next(iter(self.app.state.tasks))
            for _ in range(100):
                if self.app.state.tasks[tid]["done"]:
                    break
                time.sleep(0.05)
        self.assertEqual(pf.call_count, 1)

@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class ServeBind(unittest.TestCase):
    def test_serve_occupied_port_returns_error(self):
        import contextlib
        import io
        import socket
        from sbfl.web import app as webapp
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            out = io.StringIO()
            with mock.patch("webbrowser.open") as wb, contextlib.redirect_stdout(out),                     contextlib.redirect_stderr(out):
                rc = webapp.serve(Home(tmp / "h"), port, True)
        self.assertNotEqual(rc, 0)
        wb.assert_not_called()
        self.assertIn("porta", out.getvalue().lower())

    def test_serve_malformed_config_returns_1_with_message(self):
        import contextlib
        import io
        from sbfl.web import app as webapp
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        home = Home(tmp / "h")
        home.root.mkdir(parents=True)
        home.config_path.write_text("[run\n", encoding="utf-8")
        err = io.StringIO()
        with mock.patch("webbrowser.open") as wb, contextlib.redirect_stderr(err):
            rc = webapp.serve(home, 0, True)
        self.assertEqual(rc, 1)
        wb.assert_not_called()
        self.assertIn("config.toml", err.getvalue())


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class TelasRemovidas(WebFixture, unittest.TestCase):
    """Painel, Executar, Bugs, Lotes, Fila, Comparar, Receitas e Exportar saíram da interface."""
    REMOVIDAS = ("/painel", "/painel/estado", "/executar", "/executar/1", "/bugs", "/bug/bugsjar/maven/MNG-1",
                 "/lotes", "/fila", "/lote/1/progresso", "/comparar", "/receitas", "/exportar")

    def test_urls_antigas_dao_404(self):
        c = self.authed()
        for path in self.REMOVIDAS:
            with self.subTest(path=path):
                self.assertEqual(c.get(path).status_code, 404)

    def test_barra_lateral_so_tem_rodar_resultados_e_ambiente(self):
        html = self.authed().get("/rodar").text
        nav = html[html.index('<nav class="lateral">'):html.index("</nav>")]
        self.assertEqual(re.findall(r'<a href="(/[^"]*)"', nav), ["/rodar", "/resultados", "/ambiente"])
        self.assertNotIn("<details", nav)
        self.assertNotIn("Mais", nav)
