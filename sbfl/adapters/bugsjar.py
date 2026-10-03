"""Bugs.jar / bugs-dot-jar: um clone git por projeto, uma branch por bug
(`bugs-dot-jar_<ISSUE>_<hash-do-fix>`). Usa `git worktree`, sem mexer no seu clone."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from ..diffgt import (parse_left_hunks, faults_from_hunks, fqcn_from_path,
                      TEST_PATH_RE)
from ..proc import run, make_env
from .base import Adapter, Bug, Ctx, StageError, scan_test_classes


def git(repo, *args, check=True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise StageError("git_failed", f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout


def bugsjar_truth(repo, buggy_ref: str, fix: str):
    """Faults (list[FaultLine]) e módulo Maven a partir do diff buggy -> fix."""
    diff = git(repo, "diff", "-U0", "--no-color", "--no-renames", buggy_ref, fix, "--", "*.java")

    def path_fn(p):
        return None if TEST_PATH_RE.search(p) or not p.endswith(".java") else p

    hunks = parse_left_hunks(diff, path_fn)
    faults = faults_from_hunks(hunks, fqcn_from_path)
    if not faults:
        raise StageError("no_truth", "diff buggy->fix sem linhas em código de produção")
    first = sorted(hunks)[0]
    m = re.match(r"(.*?)/?src/", first)
    module = (m.group(1) if m else "") or "."
    return faults, module


class BugsJar(Adapter):
    name = "bugsjar"

    def _repos(self):
        return [Path(p).expanduser() for p in self.cfg.get("bugsjar", "repos")]

    def list_bugs(self, project=None, bug=None):
        bugs = []
        for repo in self._repos():
            if project and repo.name != project:
                continue
            refs = git(repo, "branch", "-a", "--format=%(refname:short)").split()
            seen = {}
            for r in refs:
                name = r.split("origin/", 1)[-1] if r.startswith("origin/") else r
                if name.startswith("bugs-dot-jar_") and (name not in seen or r.startswith("origin/")):
                    seen[name] = r
            for name, ref in sorted(seen.items()):
                if bug and bug != name:
                    continue
                bugs.append(Bug("bugsjar", repo.name, name, {"repo": str(repo), "ref": ref}))
        return bugs

    def prepare(self, bug: Bug, log_path: Path) -> Ctx:
        cfg = self.cfg
        repo, ref = Path(bug.extra["repo"]), bug.extra["ref"]
        fix = bug.bug_id.rsplit("_", 1)[-1]
        wt = cfg.work / "bugsjar" / bug.project / bug.bug_id
        if wt.exists():
            git(repo, "worktree", "remove", "--force", str(wt), check=False)
            shutil.rmtree(wt, ignore_errors=True)
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "--detach", "-f", str(wt), ref)

        def cleanup():
            git(repo, "worktree", "remove", "--force", str(wt), check=False)
            shutil.rmtree(wt, ignore_errors=True)

        try:
            git(wt, "cat-file", "-e", f"{fix}^{{commit}}")
            faults, module = bugsjar_truth(wt, "HEAD", fix)
            mod_dir = wt if module == "." else wt / module
            env = make_env(cfg)
            mvn = cfg.get("java", "mvn")
            skips = list(cfg.get("maven", "skip_flags"))
            lr = cfg.get("maven", "local_repo")
            if lr:
                skips.append(f"-Dmaven.repo.local={Path(lr).expanduser().resolve()}")
            tmo = cfg.get("maven", "build_timeout_s")
            steps = [(wt, [mvn, "-B", "-q", "install", "-DskipTests", *skips]
                      + ([] if module == "." else ["-pl", module, "-am"])),
                     (mod_dir, [mvn, "-B", "-q", "dependency:copy-dependencies",
                                "-DoutputDirectory=target/dependency", *skips])]
            for cwd, cmd in steps:
                r = run(cmd, cwd=cwd, env=env, timeout=tmo, log_path=log_path)
                if r["rc"] != 0:
                    raise StageError("build_failed", "".join(r["tail"][-15:]))
            tests_root = mod_dir / "target" / "test-classes"
            classes = scan_test_classes(tests_root)
            scope = cfg.get("jaguar", "test_scope")
            if scope == "changed":
                names = git(wt, "show", "--name-only", "--format=", fix).split()
                chg = {fqcn_from_path(n) for n in names if n.endswith(".java") and TEST_PATH_RE.search(n)}
                classes = [c for c in classes if c in chg] or classes
            if not classes:
                raise StageError("no_tests", f"nenhuma classe de teste em {tests_root}")
            return Ctx(mod_dir, "target/classes", "target/test-classes",
                       [str(Path("target/dependency") / "*")], classes, faults, cleanup,
                       {"module": module, "fix": fix})
        except BaseException:
            cleanup()
            raise
