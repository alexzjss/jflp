from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbfl.home import Home, load_settings
from sbfl.toolchain import profiles
from sbfl.toolchain.detect import Jdk, Maven
from sbfl.toolchain.profiles import MissingTool, resolve


def _jdks(*majors, base="/j"):
    return [Jdk(Path(f"{base}/jdk{m}"), m, f"{m}.0.1", "managed") for m in majors]


def _mavens(*versions, base="/m"):
    return [Maven(Path(f"{base}/mvn{'.'.join(map(str, v))}"), v, "managed") for v in versions]


class ProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Home(self.tmp / "Pasta Ção")
        self.settings = load_settings(self.home)

    def _resolve(self, profile, jdks, mavens, **kw):
        with mock.patch.object(profiles, "find_jdks", return_value=jdks), \
                mock.patch.object(profiles, "find_mavens", return_value=mavens):
            return resolve(profile, self.home, self.settings, **kw)

    def test_resolve_jaguar2_picks_11_and_399(self):
        te = self._resolve("jaguar2", _jdks(8, 11, 17), _mavens((3, 6, 3), (3, 9, 9)))
        self.assertEqual(te.jdk.major, 11)
        self.assertEqual(te.maven.version, (3, 9, 9))

    def test_resolve_missing_raises_pt(self):
        with self.assertRaises(MissingTool) as cm:
            self._resolve("jaguar2", _jdks(8, 17), _mavens((3, 9, 9)))
        self.assertEqual(cm.exception.kind, "jdk")
        self.assertIn("11", cm.exception.message)
        with self.assertRaises(MissingTool) as cm:
            self._resolve("jaguar2", _jdks(11), _mavens((3, 6, 3)))
        self.assertEqual(cm.exception.kind, "maven")

    def test_jaguar1_prefers_363_within_range(self):
        te = self._resolve("jaguar1", _jdks(8), _mavens((3, 6, 3), (3, 8, 0), (3, 9, 9)))
        self.assertEqual(te.maven.version, (3, 6, 3))
        te = self._resolve("jaguar1", _jdks(8), _mavens((3, 8, 0), (3, 9, 9)))
        self.assertEqual(te.maven.version, (3, 8, 0))

    def test_maven_profile_uses_pom_level(self):
        pom = self.tmp / "proj"
        pom.mkdir()
        (pom / "pom.xml").write_text(
            "<project><properties><maven.compiler.source>11</maven.compiler.source>"
            "</properties></project>", encoding="utf-8")
        mv = _mavens((3, 6, 3), (3, 9, 9))
        self.assertEqual(self._resolve("maven", _jdks(8, 11, 17), mv, pom_dir=pom).jdk.major, 11)
        self.assertEqual(self._resolve("maven", _jdks(8, 11, 17), mv).jdk.major, 8)
        self.assertEqual(self._resolve("maven", _jdks(8, 11, 17), mv).maven.version, (3, 6, 3))

    def test_overrides(self):
        te = self._resolve("maven", _jdks(8, 17), _mavens((3, 6, 3), (3, 9, 9)),
                           jdk=17, maven="3.9.9")
        self.assertEqual((te.jdk.major, te.maven.version), (17, (3, 9, 9)))
        with self.assertRaises(MissingTool) as cm:
            self._resolve("maven", _jdks(8), _mavens((3, 6, 3)), jdk=21)
        self.assertEqual(cm.exception.kind, "jdk")
        with self.assertRaises(MissingTool) as cm:
            self._resolve("maven", _jdks(8), _mavens((3, 6, 3)), maven="3.8.8")
        self.assertEqual(cm.exception.kind, "maven")

    def test_env_is_sanitized(self):
        self.settings.set("run", "maven_opts", "-Xmx1g")
        fake = {"JAVA_TOOL_OPTIONS": "-Xfoo", "M2_HOME": "x", "MAVEN_ARGS": "y", "PATH": "/usr/bin"}
        with mock.patch.dict(os.environ, fake, clear=False):
            te = self._resolve("jaguar2", _jdks(11), _mavens((3, 9, 9)))
        for var in ("JAVA_TOOL_OPTIONS", "M2_HOME", "MAVEN_ARGS", "_JAVA_OPTIONS", "MAVEN_HOME"):
            self.assertNotIn(var, te.env)
        parts = te.env["PATH"].split(os.pathsep)
        self.assertEqual(parts[0], str(Path("/j/jdk11") / "bin"))
        self.assertEqual(parts[1], str(Path("/m/mvn3.9.9") / "bin"))
        self.assertEqual(te.env["JAVA_HOME"], str(Path("/j/jdk11")))
        self.assertEqual(te.env["MAVEN_OPTS"], "-Xmx1g")

    def test_paths_with_space_and_accent(self):
        te = self._resolve("jaguar2", _jdks(11), _mavens((3, 9, 9)))
        cmd = te.mvn("test")
        self.assertIn(f"-Dmaven.repo.local={self.home.m2}", cmd)
        self.assertIn("Pasta Ção", self.home.m2.as_posix())
        self.assertEqual(cmd[1], "-B")
        self.assertEqual(cmd[-1], "test")
        self.assertTrue(cmd[0].endswith("mvn.cmd" if os.name == "nt" else "mvn"))

    def test_describe(self):
        d = self._resolve("jaguar2", _jdks(11), _mavens((3, 9, 9))).describe()
        self.assertEqual(d["maven"]["version"], "3.9.9")
        self.assertEqual(d["jdk"]["major"], 11)
        self.assertEqual(d["m2"], str(self.home.m2))

    @unittest.skipUnless(shutil.which("mvn") and shutil.which("java"), "precisa de mvn e java")
    def test_real_mvn_version_with_spaces(self):
        from sbfl.toolchain.detect import find_jdks, find_mavens
        jdks = find_jdks(self.home, self.settings)
        mavens = find_mavens(self.home, self.settings)
        if not (jdks and mavens):
            self.skipTest("sem JDK/Maven detectados")
        self.home.ensure()
        te = profiles.ToolEnv(jdks[0], mavens[0],
                              profiles._build_env(jdks[0], mavens[0], self.settings), self.home.m2)
        p = subprocess.run(te.mvn("-v"), env=te.env, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
