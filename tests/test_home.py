import os, shutil, sys, tempfile, tomllib, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sbfl import config as C
from sbfl.home import DEFAULTS, Home, load_settings


class HomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jflp_home_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_uses_env(self):
        target = self.tmp / "meu jflp"
        with mock.patch.dict(os.environ, {"JFLP_HOME": str(target)}):
            home = Home.default()
        self.assertEqual(home.root, target)
        self.assertEqual(home.db_path, target / "jflp.db")
        self.assertEqual(home.config_path, target / "config.toml")
        self.assertEqual(home.m2, target / "m2")

    def test_ensure_creates_dirs_and_config(self):
        home = Home(self.tmp / "h").ensure()
        for name in ("tools", "m2", "work", "runs", "recipes", "datasets"):
            self.assertTrue((home.root / name).is_dir(), name)
        self.assertTrue(home.config_path.is_file())
        text = home.config_path.read_text(encoding="utf-8")
        self.assertIn("#", text)
        self.assertEqual(tomllib.loads(text)["jaguar2"]["surefire"], "3.5.3")

    def test_ensure_keeps_existing_config(self):
        home = Home(self.tmp / "h")
        home.root.mkdir(parents=True)
        home.config_path.write_text('[run]\ntest_scope = "all"\n', encoding="utf-8")
        home.ensure()
        self.assertEqual(load_settings(home).get("run", "test_scope"), "all")

    def test_settings_roundtrip(self):
        home = Home(self.tmp / "h").ensure()
        s = load_settings(home)
        s.set("run", "test_scope", "all")
        s.save()
        self.assertEqual(load_settings(home).get("run", "test_scope"), "all")

    def test_windows_paths_and_nested_tables_roundtrip(self):
        home = Home(self.tmp / "h").ensure()
        win = r"C:\Users\Nome Sobrenome\Documentos\bugsjar"
        jdks = {"11": r"C:\Program Files\Java\jdk-11", "8": "C:/Java/jdk8"}
        s = load_settings(home)
        s.set("bugsjar", "root", win)
        s.set("tools", "jdks", jdks)
        s.set("run", "maven_opts", 'a"b\\c\'d')
        s.save()
        again = load_settings(home)
        self.assertEqual(again.get("bugsjar", "root"), win)
        self.assertEqual(again.get("tools", "jdks"), jdks)
        self.assertEqual(again.get("run", "maven_opts"), 'a"b\\c\'d')

    def test_bool_int_and_list_roundtrip(self):
        home = Home(self.tmp / "h").ensure()
        s = load_settings(home)
        s.set("run", "archive_checkout", False)
        s.set("ui", "port", 8123)
        s.set("bugsjar", "projects", ["Lang", "Math"])
        s.save()
        again = load_settings(home)
        self.assertIs(again.get("run", "archive_checkout"), False)
        self.assertEqual(again.get("ui", "port"), 8123)
        self.assertEqual(again.get("bugsjar", "projects"), ["Lang", "Math"])

    def test_defaults_merged(self):
        home = Home(self.tmp / "vazio")
        s = load_settings(home)
        self.assertEqual(s.get("jaguar2", "surefire"), "3.5.3")
        self.assertEqual(s.get("run", "skip_flags"), C.DEFAULTS["maven"]["skip_flags"])
        self.assertEqual(s.section("ui"), {"port": 0})

    def test_defaults_not_mutated_by_settings(self):
        home = Home(self.tmp / "vazio")
        s = load_settings(home)
        s.get("run", "skip_flags").append("-X")
        s.get("defects4j", "projects").append("Extra")
        self.assertNotIn("-X", DEFAULTS["run"]["skip_flags"])
        self.assertNotIn("Extra", DEFAULTS["defects4j"]["projects"])

    def test_user_file_overrides_only_given_keys(self):
        home = Home(self.tmp / "h")
        home.root.mkdir(parents=True)
        home.config_path.write_text('[jaguar2]\nsurefire = "3.0.0"\n', encoding="utf-8")
        s = load_settings(home)
        self.assertEqual(s.get("jaguar2", "surefire"), "3.0.0")
        self.assertEqual(s.get("jaguar2", "junit"), "4.12")


if __name__ == "__main__":
    unittest.main()
