import json
import threading
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sbfl import prepare as P
from sbfl.adapters.base import Bug
from sbfl.errors import StageError
from sbfl.home import Home, load_settings
from sbfl.toolchain.detect import WslStatus
from tests.bugsjar_fixture import make_bugsjar_repo


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                   check=True, capture_output=True)


def _head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = Home(self.root / "home").ensure()
        self.settings = load_settings(self.home)
        self.log = self.root / "log.txt"

    def fake_bugsjar(self):
        root = self.root / "bj"
        repo = root / "Proj"
        (repo / "src/main/java/a").mkdir(parents=True)
        (repo / "src/test/java/a").mkdir(parents=True)
        _git(root, "init", "-q", str(repo))
        (repo / "src/main/java/a/B.java").write_text("package a;\nclass B {\n  int f() { return 1; }\n}\n")
        (repo / "src/test/java/a/BTest.java").write_text("package a;\nclass BTest {}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "buggy")
        buggy = _head(repo)
        (repo / "src/main/java/a/B.java").write_text("package a;\nclass B {\n  int f() { return 2; }\n}\n")
        (repo / "src/test/java/a/BTest.java").write_text("package a;\nclass BTest { int x; }\n")
        _git(repo, "commit", "-qam", "fix")
        fix = _head(repo)
        _git(repo, "branch", f"bugs-dot-jar_X-1_{fix}", buggy)
        self.settings.set("bugsjar", "root", str(root))
        return fix


class PrepareTests(Base):
    def _submodulo(self):
        root = self.root / "repo"
        (root / "sub").mkdir(parents=True)
        (root / "pom.xml").write_text("<project><groupId>g</groupId><artifactId>pai</artifactId>"
                                      "<version>1</version></project>")
        (root / "sub" / "pom.xml").write_text(
            "<project><parent><groupId>g</groupId><artifactId>pai</artifactId><version>1</version>"
            "</parent><artifactId>sub</artifactId></project>")
        return root

    def test_local_submodulo_sobe_para_a_raiz_do_pai(self):
        root = self._submodulo()
        bug = P.local_bug(str(root / "sub"))
        self.assertEqual(bug.extra["path"], str(root.resolve()))
        self.assertEqual(bug.extra["module"], "sub")
        self.assertEqual(bug.project, "repo")

    def test_local_submodulo_com_modulo_informado(self):
        root = self._submodulo()
        (root / "sub" / "inner").mkdir()
        (root / "sub" / "inner" / "pom.xml").write_text("<project/>")
        bug = P.local_bug(str(root / "sub"), "inner")
        self.assertEqual(bug.extra["module"], "sub/inner")

    def test_local_submodulo_pasta_grande_nao_sobe(self):
        root = self._submodulo()
        with mock.patch.object(P, "folder_too_big", return_value="mais de 1 arquivos"):
            bug = P.local_bug(str(root / "sub"))
        self.assertEqual(bug.extra["path"], str((root / "sub").resolve()))
        self.assertNotIn("module", bug.extra)

    def test_local_sem_pai_no_disco_mantem_a_pasta(self):
        src = self.root / "solto"
        src.mkdir()
        (src / "pom.xml").write_text("<project/>")
        bug = P.local_bug(str(src))
        self.assertEqual(bug.extra, {"path": str(src.resolve())})

    def test_local_dir_copy_does_not_touch_source(self):
        src = self.root / "meu proj"
        (src / "target").mkdir(parents=True)
        (src / "target" / "x").write_text("x")
        (src / "pom.xml").write_text("<project/>")
        before = (src.stat().st_mtime_ns, (src / "pom.xml").stat().st_mtime_ns)
        bug = P.local_bug(str(src))
        self.assertEqual(bug.benchmark, "local")
        prep = P.prepare(self.home, self.settings, bug, self.log)
        self.assertTrue((prep.pristine / "pom.xml").exists())
        self.assertFalse((prep.pristine / "target").exists())
        self.assertIsNone(prep.relevant_tests)
        self.assertEqual(before, (src.stat().st_mtime_ns, (src / "pom.xml").stat().st_mtime_ns))
        self.assertTrue((src / "target" / "x").exists())

    def test_copy_keeps_test_inputs_and_drops_only_module_build_output(self):
        src = self.root / "inputs"
        files = {"pom.xml": "<project/>",
                 "target/classes/X.class": "x",                          # saída do build do módulo
                 "src/test/projects/x/target/foo.txt": "fixture",        # entrada de teste (sem pom.xml ao lado)
                 "src/test/resources/A.class": "bytes",                  # entrada de teste
                 "src/test/projects/y/pom.xml": "<project/>",
                 "src/test/projects/y/target/out.txt": "build de y",     # target ao lado de um pom.xml
                 ".idea/workspace.xml": "ide"}
        for rel, text in files.items():
            f = src / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        prep = P.prepare(self.home, self.settings, P.local_bug(str(src)), self.log)
        p = prep.pristine
        self.assertTrue((p / "src/test/projects/x/target/foo.txt").exists())
        self.assertTrue((p / "src/test/resources/A.class").exists())
        self.assertFalse((p / "target").exists())
        self.assertFalse((p / "src/test/projects/y/target").exists())
        self.assertFalse((p / ".idea").exists())
        w = P.working_copy(prep, self.home)
        self.assertTrue((w / "src/test/projects/x/target/foo.txt").exists())
        self.assertTrue((w / "src/test/resources/A.class").exists())
        self.assertFalse((w / P.MARKER).exists())

    def test_local_folder_edits_are_picked_up(self):
        src = self.root / "live"
        src.mkdir()
        (src / "a.txt").write_text("1")
        bug = P.local_bug(str(src))
        first = P.prepare(self.home, self.settings, bug, self.log)
        (first.pristine / "extra.txt").write_text("keep")
        same = P.prepare(self.home, self.settings, bug, self.log)            # nada mudou: reaproveita
        self.assertTrue((same.pristine / "extra.txt").exists())
        (src / "a.txt").write_text("2222")                                    # arquivo editado
        edited = P.prepare(self.home, self.settings, bug, self.log)
        self.assertEqual((edited.pristine / "a.txt").read_text(), "2222")
        self.assertFalse((edited.pristine / "extra.txt").exists())
        (src / "b.txt").write_text("novo")                                    # arquivo novo
        self.assertTrue((P.prepare(self.home, self.settings, bug, self.log).pristine / "b.txt").exists())
        (src / "b.txt").unlink()                                              # arquivo removido
        self.assertFalse((P.prepare(self.home, self.settings, bug, self.log).pristine / "b.txt").exists())

    def test_local_folder_ignored_changes_do_not_refresh(self):
        src = self.root / "live2"
        (src / "target").mkdir(parents=True)
        (src / "pom.xml").write_text("<project/>")
        bug = P.local_bug(str(src))
        first = P.prepare(self.home, self.settings, bug, self.log)
        (first.pristine / "extra.txt").write_text("keep")
        (src / "target" / "novo.class").write_text("saída do build")          # não é copiado: não conta
        again = P.prepare(self.home, self.settings, bug, self.log)
        self.assertTrue((again.pristine / "extra.txt").exists())

    def test_local_folder_refresh_flag_forces_recopy(self):
        src = self.root / "live3"
        src.mkdir()
        (src / "a.txt").write_text("1")
        bug = P.local_bug(str(src))
        first = P.prepare(self.home, self.settings, bug, self.log)
        (first.pristine / "extra.txt").write_text("keep")
        again = P.prepare(self.home, self.settings, bug, self.log, refresh=True)
        self.assertFalse((again.pristine / "extra.txt").exists())

    def test_bugsjar_checkout_with_fake_repo(self):
        fix = self.fake_bugsjar()
        bugs = P.list_bugs(self.settings, "bugsjar")
        self.assertEqual(len(bugs), 1)
        self.assertEqual(bugs[0].extra["fixed"], fix)
        self.assertEqual(P.bug_from_key(self.settings, bugs[0].key).extra, bugs[0].extra)
        prep = P.prepare(self.home, self.settings, bugs[0], self.log)
        self.assertEqual(prep.relevant_tests, ["a.BTest"])
        self.assertTrue(prep.truth)
        self.assertEqual(prep.truth[0].cls, "a.B")
        self.assertEqual(prep.module, ".")
        again = P.prepare(self.home, self.settings, bugs[0], self.log, refresh=True)
        self.assertEqual(again.relevant_tests, ["a.BTest"])

    def test_prepare_reuses_marker(self):
        src = self.root / "p1"
        src.mkdir()
        (src / "a.txt").write_text("1")
        bug = P.local_bug(str(src))
        first = P.prepare(self.home, self.settings, bug, self.log)
        (first.pristine / "extra.txt").write_text("keep")
        second = P.prepare(self.home, self.settings, bug, self.log)
        self.assertEqual(first.pristine, second.pristine)
        self.assertTrue((second.pristine / "extra.txt").exists())
        json.loads((first.pristine / P.MARKER).read_text(encoding="utf-8"))
        third = P.prepare(self.home, self.settings, bug, self.log, refresh=True)
        self.assertFalse((third.pristine / "extra.txt").exists())

    def test_working_copy_is_independent(self):
        src = self.root / "p2"
        (src / "target").mkdir(parents=True)
        (src / "a.txt").write_text("1")
        prep = P.prepare(self.home, self.settings, P.local_bug(str(src)), self.log)
        w = P.working_copy(prep, self.home)
        self.assertNotEqual(w, prep.pristine)
        (w / "a.txt").write_text("2")
        self.assertEqual((prep.pristine / "a.txt").read_text(), "1")
        self.assertNotEqual(w, P.working_copy(prep, self.home))

    def test_working_copy_skips_bugsjar_metadata(self):
        src = self.root / "p3"
        (src / ".bugs-dot-jar").mkdir(parents=True)
        (src / ".bugs-dot-jar" / "info.json").write_text("{}")
        (src / "a.txt").write_text("1")
        prep = P.prepare(self.home, self.settings, P.local_bug(str(src)), self.log)
        w = P.working_copy(prep, self.home)
        self.assertTrue((w / "a.txt").exists())
        self.assertFalse((w / ".bugs-dot-jar").exists())

    def test_local_url_commit_spec(self):
        b = P.local_bug("https://github.com/x/repo.git@abcdef1234567890")
        self.assertEqual((b.project, b.bug_id), ("repo", "abcdef123456"))
        with self.assertRaises(ValueError):
            P.local_bug("nao existe")

    def test_local_module_valid_invalid_and_key(self):
        src = self.root / "multi"
        (src / "a" / "b").mkdir(parents=True)
        (src / "a" / "b" / "pom.xml").write_text("<project/>")
        (src / "pom.xml").write_text("<project/>")
        plain = P.local_bug(str(src))
        self.assertNotIn("module", plain.extra)
        self.assertEqual(P.local_bug(str(src), "").key, plain.key)       # sem módulo: mesma chave de antes
        b = P.local_bug(str(src), "a\\b/")
        self.assertEqual(b.extra["module"], "a/b")
        self.assertNotEqual(b.key, plain.key)
        self.assertEqual(P.local_bug(str(src), "./a/b").key, b.key)
        for ruim in ("../x", "a/../../x", "/abs", "C:/x", "nao/existe"):
            with self.subTest(ruim=ruim), self.assertRaises(ValueError) as cm:
                P.local_bug(str(src), ruim)
            self.assertTrue(str(cm.exception))
        u = "https://github.com/x/repo.git@abcdef1234567890"
        ub = P.local_bug(u, "mod")
        self.assertEqual(ub.extra["module"], "mod")
        self.assertNotEqual(ub.key, P.local_bug(u).key)
        self.assertIn("abcdef123456-m", ub.key)
        self.assertEqual(ub.key, P.local_bug(u, "mod").key)
        with self.assertRaises(ValueError):
            P.local_bug(u, "../x")

    def test_local_prepare_returns_module(self):
        src = self.root / "multi2"
        (src / "m").mkdir(parents=True)
        (src / "pom.xml").write_text("<project/>")
        (src / "m" / "pom.xml").write_text("<project/>")
        prep = P.prepare(self.home, self.settings, P.local_bug(str(src), "m"), self.log)
        self.assertEqual(prep.module, "m")
        self.assertTrue((prep.pristine / "m" / "pom.xml").exists())

    def test_raiz_sem_pom_com_modulo_vira_o_proprio_projeto(self):
        src = self.root / "colecao"
        (src / "maven").mkdir(parents=True)
        (src / "maven" / "pom.xml").write_text("<project/>")
        (src / "maven" / "A.java").write_text("class A {}")
        (src / "maven" / ".git").mkdir()
        (src / "maven" / ".git" / "HEAD").write_text("x")
        (src / "outro").mkdir()
        (src / "outro" / "grande.bin").write_text("x" * 100)
        (src / ".git").mkdir()
        (src / ".git" / "HEAD").write_text("x")
        bug = P.local_bug(str(src), "maven")
        self.assertEqual(Path(bug.extra["path"]), (src / "maven").resolve())
        self.assertNotIn("module", bug.extra)
        self.assertEqual(bug.project, "maven")
        self.assertNotEqual(bug.key, P.local_bug(str(src)).key)
        self.assertEqual(bug.key, P.local_bug(str(src / "maven")).key)
        prep = P.prepare(self.home, self.settings, bug, self.log)
        self.assertEqual(prep.module, ".")
        self.assertTrue((prep.pristine / "pom.xml").exists())
        self.assertFalse((prep.pristine / "outro").exists())
        self.assertFalse((prep.pristine / ".git").exists())      # .git (de sub-repos) nunca vai para o pristine

    def test_modulo_continua_valendo_com_pom_na_raiz(self):
        src = self.root / "multi3"
        (src / "m").mkdir(parents=True)
        (src / "pom.xml").write_text("<project/>")
        (src / "m" / "pom.xml").write_text("<project/>")
        (src / "pom.xml").write_text("<project/>")
        self.assertEqual(P.local_bug(str(src), "m").extra["module"], "m")

    def test_pristine_local_nao_copia_nenhum_git(self):
        src = self.root / "comgit"
        (src / "sub" / ".git").mkdir(parents=True)
        (src / "sub" / ".git" / "obj").write_text("x")
        (src / ".git").mkdir()
        (src / ".git" / "HEAD").write_text("x")
        (src / "pom.xml").write_text("<project/>")
        prep = P.prepare(self.home, self.settings, P.local_bug(str(src)), self.log)
        self.assertFalse((prep.pristine / ".git").exists())
        self.assertFalse((prep.pristine / "sub" / ".git").exists())
        self.assertTrue((prep.pristine / "pom.xml").exists())

    def test_pasta_grande_demais_e_recusada(self):
        src = self.root / "enorme"
        src.mkdir()
        (src / "pom.xml").write_text("<project/>")
        for i in range(5):
            (src / f"f{i}.txt").write_text("x")
        self.settings.set("run", "max_copy_files", 3)
        with self.assertRaises(StageError) as cm:
            P.prepare(self.home, self.settings, P.local_bug(str(src)), self.log)
        self.assertIn("muito grande", cm.exception.detail)
        self.assertIn("subpasta", cm.exception.detail)
        self.settings.set("run", "max_copy_files", 100)
        self.settings.set("run", "max_copy_mb", 0.000001)
        with self.assertRaises(StageError):
            P.prepare(self.home, self.settings, P.local_bug(str(src)), self.log)

    def test_find_poms_e_bugsjar(self):
        src = self.root / "col"
        for n in ("a", "b/c"):
            (src / n).mkdir(parents=True)
            (src / n / "pom.xml").write_text("<project/>")
        (src / "vazia").mkdir()
        (src / ".oculta").mkdir()
        (src / ".oculta" / "pom.xml").write_text("<project/>")
        self.assertEqual(P.find_poms(src), ["a", "b/c"])
        self.assertFalse(P.is_bugsjar_repo(src / "a"))
        (src / "a" / ".git").mkdir()
        (src / "a" / ".bugs-dot-jar").mkdir()
        self.assertTrue(P.is_bugsjar_repo(src / "a"))
        self.assertTrue(P.is_bugsjar_collection(src))
        self.assertFalse(P.is_bugsjar_collection(src / "a"))

    def test_local_key_raises(self):
        with self.assertRaises(ValueError):
            P.bug_from_key(self.settings, "local/x/dir-1")

    def test_to_wsl_path(self):
        self.assertEqual(P.to_wsl_path(Path("E:\\a\\b")), "/mnt/e/a/b")
        self.assertEqual(P.to_wsl_path("c:/x y/z"), "/mnt/c/x y/z")

    def test_d4j_unavailable_message(self):
        wsl = WslStatus(False, [], None, "WSL indisponível: não está instalado")
        bug = Bug("d4j", "Lang", "1")
        with mock.patch.object(P, "wsl_status", return_value=wsl), \
                mock.patch.object(P, "defects4j_cmd", return_value=None):
            with self.assertRaises(StageError) as cm:
                P.prepare(self.home, self.settings, bug, self.log)
        self.assertEqual(cm.exception.status, "checkout_falhou")
        self.assertIn("WSL", cm.exception.detail)
        self.assertFalse(P._pristine_dir(self.home, bug).exists())


    def test_src_truth_with_space_and_accent_paths(self):
        b = self.root / "pasta com espaço e ç" / "b"
        f = self.root / "pasta com espaço e ç" / "f"
        for d, ret in ((b, 1), (f, 2)):
            (d / "org/x").mkdir(parents=True)
            (d / "org/x/Foo.java").write_text(f"package org.x;\nclass Foo {{\n  int f() {{ return {ret}; }}\n}}\n")
        faults = P._src_truth(b, f)
        self.assertEqual([(x.cls, x.line) for x in faults], [("org.x.Foo", 3)])
        (f / "org/x/Foo.java").write_bytes((b / "org/x/Foo.java").read_bytes())
        with self.assertRaises(StageError):
            P._src_truth(b, f)

    def test_local_repo_url_never_modifies_original(self):
        repo = self.root / "orig"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "a.txt").write_text("1")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c")
        bug = P.local_bug(f"{repo}@{_head(repo)}")
        prep = P.prepare(self.home, self.settings, bug, self.log)
        self.assertTrue((prep.pristine / "a.txt").exists())
        self.assertFalse((repo / ".git" / "worktrees").exists())
        P.prepare(self.home, self.settings, bug, self.log, refresh=True)
        self.assertFalse((repo / ".git" / "worktrees").exists())

    def test_test_fqcn_most_specific_root(self):
        self.assertEqual(P._test_fqcn("mod/tests/src/test/java/x/T.java"), "x.T")
        self.assertEqual(P._test_fqcn("src/test/java/a/BTest.java"), "a.BTest")
        self.assertEqual(P._test_fqcn("test/org/y/ZTest.java"), "org.y.ZTest")

    def test_deleted_test_files_not_relevant(self):
        fix = self.fake_bugsjar()
        repo = self.root / "bj" / "Proj"
        _git(repo, "checkout", "-q", fix)
        (repo / "src/test/java/a/OldTest.java").write_text("package a;\nclass OldTest {}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add")
        _git(repo, "rm", "-q", "src/test/java/a/OldTest.java")
        _git(repo, "commit", "-qm", "del")
        self.assertEqual(P._changed_tests(repo, "HEAD"), [])

    def test_rmtree_readonly(self):
        d = self.root / "ro"
        (d / "sub").mkdir(parents=True)
        f = d / "sub" / "obj"
        f.write_text("x")
        import os, stat
        os.chmod(f, stat.S_IREAD)
        P._rmtree(d)
        self.assertFalse(d.exists())

    def test_concurrent_prepare_same_bug(self):
        src = self.root / "conc"
        src.mkdir()
        (src / "a.txt").write_text("1")
        bug = P.local_bug(str(src))
        res, errs = [], []

        def work():
            try:
                res.append(P.prepare(self.home, self.settings, bug, self.log))
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=work) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0].pristine, res[1].pristine)
        self.assertTrue((res[0].pristine / "a.txt").exists())


if __name__ == "__main__":
    unittest.main()


class BugsjarLocal(Base):
    """Pasta local dentro de um repositório do Bugs.jar: verdade, módulo e testes vêm de `.bugs-dot-jar`."""

    def prep(self, escolha="apache-maven", repo=None):
        repo = repo or make_bugsjar_repo(self.root)
        bug = P.local_bug(str(repo / escolha) if escolha else str(repo))
        return repo, bug, P.prepare(self.home, self.settings, bug, self.log)

    def test_sobe_a_raiz_e_usa_o_modulo_do_patch(self):
        repo, bug, prep = self.prep()
        self.assertEqual(Path(bug.extra["path"]), repo.resolve())
        self.assertEqual(bug.extra["module"], "apache-maven")      # escolha do usuário, antes do prepare
        self.assertEqual(prep.module, "maven-core")
        w = prep.info["warnings"]
        self.assertTrue(any("apache-maven" in t and "não tem código-fonte (empacotamento pom)" in t for t in w))
        self.assertTrue(any("maven-core" in t for t in w))

    def test_verdade_so_com_codigo_de_producao(self):
        _, _, prep = self.prep()
        got = {(f.cls, f.line, f.kind) for f in prep.truth}
        self.assertEqual({c for c, _, _ in got}, {"org.x.Foo"})
        self.assertIn(("org.x.Foo", 4, "removed"), got)
        self.assertIn(("org.x.Foo", 10, "anchor"), got)

    def test_testes_relevantes_vem_do_test_results(self):
        _, _, prep = self.prep()
        self.assertEqual(prep.relevant_tests, ["org.x.BoomTest", "org.x.FooTest"])

    def test_sem_test_results_usa_testes_do_patch(self):
        repo = make_bugsjar_repo(self.root, results=None)
        _, _, prep = self.prep(repo=repo)
        self.assertEqual(prep.relevant_tests, ["org.x.FooTest"])

    def test_test_results_so_com_secoes_resolve_pelo_nome_simples(self):
        res = "Results :\n\nFailed tests: \n  FooTest.testA:340 expected\n\nTests run: 1, Failures: 1\n"
        repo = make_bugsjar_repo(self.root, results=res)
        _, _, prep = self.prep(repo=repo)
        self.assertEqual(prep.relevant_tests, ["org.x.FooTest"])

    def test_modulo_certo_do_usuario_nao_gera_aviso(self):
        _, _, prep = self.prep(escolha="maven-core")
        self.assertEqual(prep.module, "maven-core")
        self.assertEqual(prep.info.get("warnings", []), [])

    def test_avisos_sobrevivem_ao_marcador(self):
        _, bug, prep = self.prep()
        again = P.prepare(self.home, self.settings, bug, self.log)
        self.assertEqual(again.info["warnings"], prep.info["warnings"])
        self.assertEqual(again.module, "maven-core")
        self.assertEqual(again.truth, prep.truth)
        self.assertEqual(again.relevant_tests, prep.relevant_tests)

    def test_patch_ilegivel_nao_derruba_a_preparacao(self):
        repo = make_bugsjar_repo(self.root, patch="lixo\n")
        _, _, prep = self.prep(repo=repo)
        self.assertEqual(prep.truth, [])
        self.assertEqual(prep.module, "apache-maven")

    def test_sem_bugs_dot_jar_nada_muda(self):
        src = self.root / "plain"
        (src / "src/main/java").mkdir(parents=True)
        (src / "pom.xml").write_text("<project/>")
        prep = P.prepare(self.home, self.settings, P.local_bug(str(src)), self.log)
        self.assertEqual(prep.truth, [])
        self.assertIsNone(prep.relevant_tests)
        self.assertEqual(prep.module, ".")
        self.assertNotIn("warnings", prep.info)

    def test_modulo_sem_codigo_avisa_mesmo_sem_bugsjar(self):
        src = self.root / "dist"
        src.mkdir()
        (src / "pom.xml").write_text("<project><packaging>pom</packaging></project>")
        prep = P.prepare(self.home, self.settings, P.local_bug(str(src)), self.log)
        self.assertTrue(any("não tem código-fonte (empacotamento pom)" in t for t in prep.info["warnings"]))

    def test_helpers(self):
        repo = make_bugsjar_repo(self.root)
        self.assertEqual(P.find_bugsjar_repo(repo / "apache-maven"), repo)
        self.assertEqual(P.find_bugsjar_repo(repo), repo)
        self.assertIsNone(P.find_bugsjar_repo(self.root))
        self.assertEqual(P.modules_with_code(repo), ["maven-core"])
        self.assertTrue(P.module_without_code(repo, "apache-maven"))
        self.assertFalse(P.module_without_code(repo, "maven-core"))


class BugsjarRepoBugs(unittest.TestCase):
    """Listagem de bugs de um repositório do Bugs.jar (git de verdade) e resumo do bug-report.yml."""

    def test_lista_resumo_e_rotulo(self):
        import shutil
        import tempfile
        from sbfl.prepare import bugsjar_label, bugsjar_repo_bugs, bugsjar_summary
        from tests.bugsjar_fixture import make_bugsjar_git, ramo_de
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = make_bugsjar_git(tmp, ("MNG-1", "MNG-2"))
        bugs = bugsjar_repo_bugs(repo)
        self.assertEqual([b.bug_id for b in bugs], sorted([ramo_de("MNG-1"), ramo_de("MNG-2")]))
        self.assertEqual(bugs[0].key, f"bugsjar/maven/{bugs[0].bug_id}")
        self.assertEqual(bugs[0].extra["fixed"], bugs[0].bug_id.rsplit("_", 1)[-1])
        self.assertEqual(bugsjar_label(ramo_de("MNG-2")), "MNG-2")
        self.assertEqual(bugsjar_summary(repo, ramo_de("MNG-2")), "resumo do MNG-2")
        self.assertEqual(bugsjar_summary(repo, "master"), "")
        (repo / ".bugs-dot-jar" / "bug-report.yml").write_text(
            "BugID: X\nSummary: 'uma frase longa\n  continua aqui'\nDescription: d\n", encoding="utf-8")
        for args in (("add", "-A"), ("commit", "-q", "-m", "m")):
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                           check=True, capture_output=True)
        self.assertEqual(bugsjar_summary(repo, "HEAD"), "uma frase longa continua aqui")

    def test_sem_git_lista_vazia(self):
        import tempfile
        from sbfl.prepare import bugsjar_repo_bugs
        self.assertEqual(bugsjar_repo_bugs(Path(tempfile.gettempdir()) / "nao-existe-bj"), [])
