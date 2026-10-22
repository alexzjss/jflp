"""App de desktop: navegador em modo app, atalhos, ui.log sem console, ui.json e encerramento da janela."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sbfl import desktop
from sbfl.home import Home


_sleep = time.sleep


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        self.home = Home(self.tmp / "home")
        self.home.root.mkdir()


class TestNavegador(Tmp):
    def test_windows_prefere_edge_x86_e_depois_chrome(self):
        env = {"ProgramFiles": r"C:\PF", "ProgramFiles(x86)": r"C:\PF86", "LOCALAPPDATA": r"C:\L"}
        edge = Path(r"C:\PF86") / "Microsoft/Edge/Application/msedge.exe"
        chrome = Path(r"C:\L") / "Google/Chrome/Application/chrome.exe"
        found = desktop.find_app_browser(env, is_file=lambda p: p in (edge, chrome), windows=True)
        self.assertEqual(found, str(edge))
        found = desktop.find_app_browser(env, is_file=lambda p: p == chrome, windows=True)
        self.assertEqual(found, str(chrome))
        self.assertIsNone(desktop.find_app_browser(env, is_file=lambda p: False, windows=True))

    def test_linux_usa_which_na_ordem(self):
        achados = {"google-chrome": "/usr/bin/google-chrome", "microsoft-edge": "/usr/bin/edge"}
        self.assertEqual(desktop.find_app_browser(which=achados.get, windows=False), "/usr/bin/google-chrome")
        self.assertIsNone(desktop.find_app_browser(which=lambda n: None, windows=False))

    def test_comando_app(self):
        cmd = desktop.app_command("/b/edge", "http://127.0.0.1:1/?t=x", self.tmp / "navegador")
        self.assertEqual(cmd[0], "/b/edge")
        self.assertIn("--app=http://127.0.0.1:1/?t=x", cmd)
        self.assertIn(f"--user-data-dir={self.tmp / 'navegador'}", cmd)
        self.assertIn("--no-first-run", cmd)


class TestAtalhos(Tmp):
    def test_script_powershell_escapa_aspas_simples_espaco_e_acento(self):
        alvo = r"C:\Users\João D'Arc\env site\pythonw.exe"
        pasta = r"E:\Meus Projetos\o'neil\jflp"
        icone = r"E:\Meus Projetos\o'neil\jflp\sbfl\web\static\jflp.ico"
        s = desktop.powershell_create_script(alvo, pasta, icone)
        self.assertIn("$alvo='C:\\Users\\João D''Arc\\env site\\pythonw.exe'", s)
        self.assertIn("$pasta='E:\\Meus Projetos\\o''neil\\jflp'", s)
        self.assertIn("$icone='E:\\Meus Projetos\\o''neil\\jflp\\sbfl\\web\\static\\jflp.ico'", s)
        self.assertIn("GetFolderPath('Desktop')", s)
        self.assertIn("GetFolderPath('Programs')", s)
        self.assertIn("-m sbfl ui --app", s)
        self.assertIn("WScript.Shell", s)
        # nenhum caminho aparece fora dos literais escapados
        self.assertEqual(s.count("João"), 1)

    def test_run_ps_usa_encodedcommand_sem_caminhos_na_linha(self):
        with mock.patch.object(desktop.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="C:\\a b\\x.lnk\n", stderr="")
            out = desktop._run_ps("Write-Output 'João'")
        self.assertEqual(out, ["C:\\a b\\x.lnk"])
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[-2], "-EncodedCommand")
        self.assertEqual(base64.b64decode(cmd[-1]).decode("utf-16-le"), "Write-Output 'João'")

    def test_script_remocao(self):
        s = desktop.powershell_remove_script()
        self.assertIn("Remove-Item -LiteralPath", s)
        self.assertIn("JFLP Studio.lnk", s)

    @unittest.skipIf(os.name == "nt", "caminho de .desktop só em Linux/mac")
    def test_desktop_entry_no_home(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
            (self.tmp / "x").mkdir()
            paths = desktop.create_shortcuts()
            self.assertEqual(paths, [str(self.tmp / ".local/share/applications/jflp.desktop")])
            txt = Path(paths[0]).read_text(encoding="utf-8")
            self.assertIn("-m sbfl ui --app", txt)
            self.assertEqual(desktop.remove_shortcuts(), paths)
            self.assertFalse(Path(paths[0]).exists())

    def test_desktop_entry_texto(self):
        txt = desktop.desktop_entry("/opt/meu env/bin/python", "/home/a b/jflp", "/i/favicon.png")
        self.assertIn('Exec="/opt/meu env/bin/python" -m sbfl ui --app', txt)
        self.assertIn("Path=/home/a b/jflp", txt)
        self.assertIn("Terminal=false", txt)

    def test_icone_ico_multitamanho(self):
        data = desktop.icon_path().read_bytes()
        self.assertEqual(data[:4], b"\x00\x00\x01\x00")
        self.assertEqual(int.from_bytes(data[4:6], "little"), 4)
        self.assertEqual(data[70:74], b"\x89PNG")     # após as 4 entradas vêm as imagens PNG


class TestSemConsole(Tmp):
    def test_stdout_none_vai_para_ui_log(self):
        old = sys.stdout, sys.stderr
        try:
            sys.stdout = sys.stderr = None
            desktop.ensure_streams(self.home)
            print("olá")
            print("erro", file=sys.stderr)
            sys.stdout.flush()
            log = sys.stdout
        finally:
            sys.stdout, sys.stderr = old
            try:
                log.close()
            except Exception:
                pass
        self.assertEqual((self.home.root / "ui.log").read_text(encoding="utf-8"), "olá\nerro\n")

    def test_com_console_nao_mexe(self):
        out, err = sys.stdout, sys.stderr
        desktop.ensure_streams(self.home)
        self.assertIs(sys.stdout, out)
        self.assertIs(sys.stderr, err)
        self.assertFalse((self.home.root / "ui.log").exists())


class TestUiJson(Tmp):
    def test_pid_morto_e_ignorado(self):
        desktop.ui_json(self.home).write_text(json.dumps({"url": "http://127.0.0.1:1/?t=a", "pid": 99}))
        self.assertIsNone(desktop.running_instance(self.home, responds=lambda u: True, alive=lambda p: False))

    def test_url_que_nao_responde_e_ignorada(self):
        desktop.ui_json(self.home).write_text(json.dumps({"url": "http://127.0.0.1:1/?t=a", "pid": 99}))
        self.assertIsNone(desktop.running_instance(self.home, responds=lambda u: False, alive=lambda p: True))

    def test_json_invalido_ou_ausente(self):
        self.assertIsNone(desktop.running_instance(self.home))
        desktop.ui_json(self.home).write_text("{ruim")
        self.assertIsNone(desktop.running_instance(self.home))

    def test_vivo_e_respondendo_so_abre_a_url(self):
        from sbfl.web import app as webapp
        url = "http://127.0.0.1:1/?t=a"
        desktop.ui_json(self.home).write_text(json.dumps({"url": url, "pid": 99}))
        with mock.patch.object(desktop, "running_instance", return_value=url), \
                mock.patch.object(desktop, "open_app_window") as win, \
                mock.patch.object(webapp, "Worker") as worker, \
                mock.patch.object(webapp.webbrowser, "open") as wb:
            rc = webapp.serve(self.home, 0, True, False, True)
        self.assertEqual(rc, 0)
        win.assert_called_once_with(url, self.home)
        wb.assert_not_called()
        worker.assert_not_called()          # nenhum servidor/worker novo

    def test_vivo_sem_app_usa_navegador_padrao(self):
        from sbfl.web import app as webapp
        url = "http://127.0.0.1:1/?t=a"
        with mock.patch.object(desktop, "running_instance", return_value=url), \
                mock.patch.object(webapp.webbrowser, "open") as wb:
            self.assertEqual(webapp.serve(self.home, 0, True, False, False), 0)
        wb.assert_called_once_with(url)

    def test_remove_so_o_proprio(self):
        desktop.ui_json(self.home).write_text(json.dumps({"url": "u", "pid": os.getpid() + 12345}))
        desktop.remove_ui_state(self.home)
        self.assertTrue(desktop.ui_json(self.home).exists())
        desktop.write_ui_state(self.home, "u")
        desktop.remove_ui_state(self.home)
        self.assertFalse(desktop.ui_json(self.home).exists())


class TestServeApp(Tmp):
    def _fake(self, code):
        return mock.patch.object(desktop, "open_app_window", side_effect=lambda url, home: subprocess.Popen(
            [sys.executable, "-c", code]))

    def test_encerra_quando_o_navegador_termina(self):
        from sbfl.web import app as webapp
        t0 = time.monotonic()
        with self._fake("import time; time.sleep(1); raise SystemExit(1)"):
            rc = webapp.serve(self.home, 0, False, False, True)
        self.assertEqual(rc, 0)
        self.assertLess(time.monotonic() - t0, 30)
        self.assertFalse(desktop.ui_json(self.home).exists())      # ui.json apagado ao sair

    def test_repasse_espera_o_perfil_liberar(self):
        from sbfl.web import app as webapp
        estados = iter([True, True, False, False, False])
        with self._fake("pass"), \
                mock.patch.object(desktop, "_profile_in_use", side_effect=lambda p: next(estados, False)), \
                mock.patch.object(desktop.time, "sleep", lambda s: _sleep(0.01)):
            rc = webapp.serve(self.home, 0, False, False, True)
        self.assertEqual(rc, 0)

    def test_wait_for_window_sem_como_saber_segue_ate_keep_going_cair(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        n = iter(range(5))
        with mock.patch.object(desktop.time, "sleep", lambda s: None):
            desktop.wait_for_window(p, "x", lambda: next(n, None) is not None, in_use=lambda d: None)
        self.assertEqual(list(n), [])


if __name__ == "__main__":
    unittest.main()
