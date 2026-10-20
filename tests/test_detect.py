from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbfl.home import Home, load_settings
from sbfl.toolchain import detect

MSG = "O Subsistema do Windows para Linux não está instalado"


def _fake_run(rc: int, out: bytes):
    return mock.Mock(return_value=subprocess.CompletedProcess([], rc, out, b""))


class DecodeTests(unittest.TestCase):
    def test_decode_console_utf16(self):
        text = detect.decode_console(MSG.encode("utf-16-le"))
        self.assertIn("não está instalado", text)

    def test_decode_console_utf16_bom(self):
        text = detect.decode_console(MSG.encode("utf-16"))
        self.assertIn("não está instalado", text)

    def test_decode_console_utf8(self):
        self.assertEqual(detect.decode_console("ação".encode("utf-8")), "ação")


class WslTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = load_settings(Home(Path(self.tmp.name)))

    def test_wsl_absent_message(self):
        run = _fake_run(1, MSG.encode("utf-16-le"))
        with mock.patch.object(detect, "_is_windows", return_value=True), \
                mock.patch.object(detect.subprocess, "run", run):
            st = detect.wsl_status(self.settings)
        self.assertFalse(st.installed)
        self.assertEqual(st.distros, [])
        self.assertIsNone(st.d4j)
        self.assertIn("não está instalado", st.message)

    def test_wsl_missing_executable(self):
        with mock.patch.object(detect, "_is_windows", return_value=True), \
                mock.patch.object(detect.subprocess, "run", side_effect=FileNotFoundError):
            st = detect.wsl_status(self.settings)
        self.assertFalse(st.installed)
        self.assertTrue(st.message)

    def test_wsl_with_distro_and_d4j(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["wsl.exe", "-l"]:
                return subprocess.CompletedProcess(cmd, 0, "Ubuntu\r\n".encode("utf-16-le"), b"")
            return subprocess.CompletedProcess(cmd, 0, b"/opt/d4j/framework/bin/defects4j\n", b"")

        with mock.patch.object(detect, "_is_windows", return_value=True), \
                mock.patch.object(detect.subprocess, "run", run):
            st = detect.wsl_status(self.settings)
            cmd = detect.defects4j_cmd(self.settings, st)
        self.assertTrue(st.installed)
        self.assertEqual(st.distros, ["Ubuntu"])
        self.assertEqual(st.d4j, "/opt/d4j/framework/bin/defects4j")
        self.assertEqual(cmd[:4], ["wsl", "-d", "Ubuntu", "--"])

    def _wsl_with_probe(self, probe_out: bytes, rc: int = 0):
        def run(cmd, **kw):
            if cmd[:2] == ["wsl.exe", "-l"]:
                return subprocess.CompletedProcess(
                    cmd, 0, "docker-desktop\r\nUbuntu\r\n".encode("utf-16-le"), b"")
            self.probe_cmd = cmd
            return subprocess.CompletedProcess(cmd, rc, probe_out, b"wsl: failed to translate path\n")

        with mock.patch.object(detect, "_is_windows", return_value=True), \
                mock.patch.object(detect.subprocess, "run", run):
            return detect.wsl_status(self.settings)

    def test_wsl_distro_without_defects4j(self):
        st = self._wsl_with_probe(b"", rc=1)
        self.assertTrue(st.installed)
        self.assertIsNone(st.d4j)
        self.assertIn("Defects4J não encontrado", st.message)
        self.assertEqual(self.probe_cmd[2], "Ubuntu")  # pulou docker-desktop

    def test_wsl_noisy_banner_not_found(self):
        st = self._wsl_with_probe(b"Bem-vindo ao Ubuntu\n")
        self.assertIsNone(st.d4j)

    def test_wsl_noisy_banner_found(self):
        st = self._wsl_with_probe(b"Bem-vindo\n/usr/bin/defects4j\n")
        self.assertEqual(st.d4j, "/usr/bin/defects4j")

    def test_not_applicable_on_linux(self):
        with mock.patch.object(detect, "_is_windows", return_value=False):
            st = detect.wsl_status(self.settings)
        self.assertFalse(st.installed)
        self.assertEqual(st.message, "não se aplica")

    def test_defects4j_cmd_none_without_wsl(self):
        st = detect.WslStatus(False, [], None, "x")
        with mock.patch.object(detect, "_is_windows", return_value=True):
            self.assertIsNone(detect.defects4j_cmd(self.settings, st))


class JdkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Home(Path(self.tmp.name) / "h")
        self.home.ensure()
        self.settings = load_settings(self.home)

    def _fake_jdk(self, base: Path) -> Path:
        (base / "bin").mkdir(parents=True)
        (base / "bin" / "java.exe").write_text("")
        (base / "bin" / "java").write_text("")
        return base

    def test_find_jdks_managed_and_config(self):
        managed = self._fake_jdk(self.home.tools / "jdk-11.0.24+8")
        custom = self._fake_jdk(Path(self.tmp.name) / "meu jdk ç" / "jdk17")
        self.settings.set("tools", "jdks", {"17": str(custom), "11": str(managed)})

        def info(p):
            return (17, "17.0.1") if Path(p).name == "jdk17" else (11, "11.0.24")

        with mock.patch.object(detect, "java_info", side_effect=info), \
                mock.patch.object(detect, "_system_java_homes", return_value=[managed]):
            jdks = detect.find_jdks(self.home, self.settings)
        self.assertEqual([(j.major, j.source) for j in jdks], [(17, "config"), (11, "config")])
        self.assertEqual(len({j.home.resolve() for j in jdks}), len(jdks))

    def test_managed_and_system_dedup(self):
        managed = self._fake_jdk(self.home.tools / "jdk-11.0.24+8")
        other = self._fake_jdk(Path(self.tmp.name) / "sys" / "jdk-8")
        broken = Path(self.tmp.name) / "sys" / "vazio"
        broken.mkdir(parents=True)

        def info(p):
            return {"jdk-11.0.24+8": (11, "11.0.24"), "jdk-8": (8, "1.8.0_1")}.get(Path(p).name)

        with mock.patch.object(detect, "java_info", side_effect=info), \
                mock.patch.object(detect, "_system_java_homes", return_value=[managed, other, broken]):
            jdks = detect.find_jdks(self.home, self.settings)
        self.assertEqual([(j.major, j.source) for j in jdks], [(11, "managed"), (8, "system")])

    def test_java_info_missing_returns_none(self):
        self.assertIsNone(detect.java_info(Path(self.tmp.name) / "nao-existe"))

    def test_java_info_parses(self):
        base = self._fake_jdk(Path(self.tmp.name) / "j")
        out = b'java version "1.8.0_504"\r\nJava(TM) SE Runtime\r\n'
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, b"", out))
        with mock.patch.object(detect.subprocess, "run", run):
            self.assertEqual(detect.java_info(base), (8, "1.8.0_504"))


class MavenTests(unittest.TestCase):
    def test_maven_version_parse(self):
        from sbfl.envcheck import maven_version
        self.assertEqual(maven_version("Apache Maven 3.9.16 (2bdd...)"), (3, 9, 16))

    def test_find_mavens_managed_and_config(self):
        with tempfile.TemporaryDirectory() as t:
            home = Home(Path(t) / "h")
            home.ensure()
            settings = load_settings(home)
            m = home.tools / "apache-maven-3.6.3"
            (m / "bin").mkdir(parents=True)
            (m / "bin" / "mvn.cmd").write_text("")
            (m / "bin" / "mvn").write_text("")
            with mock.patch.object(detect, "maven_info", return_value=(3, 6, 3)), \
                    mock.patch.object(detect, "_system_maven_homes", return_value=[m]):
                mv = detect.find_mavens(home, settings)
        self.assertEqual([(x.version, x.source) for x in mv], [((3, 6, 3), "managed")])


class MavenManagedJdkTests(unittest.TestCase):
    def test_only_managed_jdk_is_used_for_maven(self):
        with tempfile.TemporaryDirectory() as t:
            home = Home(Path(t) / "h")
            home.ensure()
            settings = load_settings(home)
            jdk = home.tools / "jdk-11.0.24+8"
            (jdk / "bin").mkdir(parents=True)
            (jdk / "bin" / "java.exe").write_text("")
            (jdk / "bin" / "java").write_text("")
            m = home.tools / "apache-maven-3.9.9"
            (m / "bin").mkdir(parents=True)
            (m / "bin" / "mvn.cmd").write_text("")
            (m / "bin" / "mvn").write_text("")
            seen = {}

            def run(cmd, env=None, **kw):
                seen["java_home"] = env and env.get("JAVA_HOME")
                return subprocess.CompletedProcess(cmd, 0, b"Apache Maven 3.9.9 (x)\n", b"")

            with mock.patch.object(detect, "java_info", return_value=(11, "11.0.24")), \
                    mock.patch.object(detect, "_system_java_homes", return_value=[]), \
                    mock.patch.object(detect, "_system_maven_homes", return_value=[]), \
                    mock.patch.object(detect.subprocess, "run", run):
                mv = detect.find_mavens(home, settings)
        self.assertEqual([x.version for x in mv], [(3, 9, 9)])
        self.assertEqual(seen["java_home"], str(jdk))


@unittest.skipUnless(shutil.which("java"), "java ausente")
class RealMachineTests(unittest.TestCase):
    def test_detect_real_machine(self):
        with tempfile.TemporaryDirectory() as t:
            home = Home(Path(t) / "h")
            home.ensure()
            jdks = detect.find_jdks(home, load_settings(home))
        self.assertTrue(any(j.major > 0 for j in jdks))


if __name__ == "__main__":
    unittest.main()
