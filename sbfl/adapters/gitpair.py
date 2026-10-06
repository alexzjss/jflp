"""Base genérica: qualquer benchmark em que um bug = (commit com defeito, commit/patch de correção)."""
from __future__ import annotations

from pathlib import Path

from .. import build as build_mod
from ..errors import StageError
from ..gitutil import add_worktree, ensure_repo, git
from ..jdk import auto_java_home
from ..proc import make_env
from ..truth import git_truth, patch_truth
from .base import Adapter, Bug, Ctx, scan_tests


class GitPairAdapter(Adapter):
    """Subclasses só precisam implementar `list_bugs` preenchendo Bug.extra com:
       repo (caminho/URL), buggy (ref), fixed (ref) OU fix_patch (arquivo),
       opcionais: test_patch, build (auto|maven|gradle|custom), module, custom, java_home."""

    section = ""

    def java_home(self, bug):
        return bug.extra.get("java_home") or self.cfg.section(self.section).get("java_home") or None

    def check_layout(self, repo: Path, bug: Bug) -> None:
        pass

    def provenance(self, repo: Path, bug: Bug, wt: Path) -> dict:
        """Metadados do benchmark a guardar junto do resultado (nome -> texto)."""
        return {}

    def crosscheck(self, prov: dict, faults: list) -> str | None:
        """Confere o ground truth contra algo do próprio benchmark (ou None)."""
        return None

    def prepare(self, bug: Bug, log_path: Path) -> Ctx:
        cfg, e = self.cfg, bug.extra
        repo = ensure_repo(cfg, e["repo"])
        self.check_layout(repo, bug)
        wt = cfg.work / bug.benchmark / bug.project / bug.bug_id
        cleanup = add_worktree(repo, wt, e["buggy"])
        try:
            if e.get("fix_patch"):
                faults, module = patch_truth(e["fix_patch"])
            elif e.get("fixed"):
                faults, module = git_truth(wt, "HEAD", e["fixed"])
            else:
                raise StageError("no_truth", "entrada sem 'fixed' nem 'fix_patch'")
            module = e.get("module") or module
            prov = self.provenance(repo, bug, wt)
            xcheck = self.crosscheck(prov, faults)
            if e.get("test_patch"):
                try:
                    git(wt, "apply", "--whitespace=nowarn",
                        str(Path(e["test_patch"]).expanduser().resolve()))
                except StageError as ex:
                    raise StageError("test_patch_failed", ex.detail)
            jh, level = self.java_home(bug), None
            if not jh:   # [java.homes]: escolhe o JDK pelo nível declarado no pom.xml
                jh, level = auto_java_home(cfg, [wt if module == "." else wt / module, wt])
            b = build_mod.build(cfg, wt, module, e.get("build", "auto"), log_path,
                                make_env(cfg, jh), e.get("custom"))
            classes, stats = scan_tests(b.project_dir / b.tests_dir)
            if cfg.get("jaguar", "test_scope") == "changed" and e.get("fixed"):
                names = git(wt, "show", "--name-only", "--format=", e["fixed"]).split()
                from ..diffgt import TEST_PATH_RE, fqcn_from_path
                chg = {fqcn_from_path(n) for n in names if n.endswith(".java") and TEST_PATH_RE.search(n)}
                classes = [c for c in classes if c in chg] or classes
            if not classes:
                why = "junit5_only" if stats["junit5_skipped"] else "no_tests"
                raise StageError(why, f"{stats} em {b.project_dir / b.tests_dir}"
                                 + (" — o JaguarRunner usa JUnit 4; testes Jupiter não rodam nele"
                                    if why == "junit5_only" else ""))
            return Ctx(b.project_dir, b.classes_dir, b.tests_dir, b.extra_cp, classes, faults,
                       cleanup, {"module": module, "build": e.get("build", "auto"), **stats,
                                 **({"truth_crosscheck": xcheck} if xcheck else {}),
                                 **({"java_level": level} if level else {})}, jh, prov)
        except BaseException:
            cleanup()
            raise
