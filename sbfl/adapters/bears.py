"""Bears: um único repositório (bears-benchmark), uma branch por bug:
`<slug do projeto>-<build buggy>-<build patched>`. Layout dos commits da branch (README do Bears):
#1 versão com defeito, #2 mudanças nos testes, #3 versão com o patch humano, #4 bears.json.
Logo, a partir do HEAD:  buggy+testes = HEAD~2,  corrigido = HEAD~1."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..errors import StageError
from ..gitutil import git
from .base import Bug
from .gitpair import GitPairAdapter

BRANCH = re.compile(r"^(?P<slug>.+)-(?P<b>\d+)-(?P<p>\d+)$")


class Bears(GitPairAdapter):
    name = "bears"
    section = "bears"

    def list_bugs(self, project=None, bug=None):
        repo = self.cfg.get("bears", "repo")
        if not repo:
            return []
        repo = Path(repo).expanduser()
        seen = {}
        for r in git(repo, "branch", "-a", "--format=%(refname:short)").split():
            n = r.split("origin/", 1)[-1] if r.startswith("origin/") else r
            if BRANCH.match(n) and (n not in seen or r.startswith("origin/")):
                seen[n] = r
        bugs = []
        for n, ref in sorted(seen.items()):
            slug = BRANCH.match(n)["slug"]
            if (project and project != slug) or (bug and bug != n):
                continue
            bugs.append(Bug(self.name, slug, n, {"repo": str(repo), "buggy": f"{ref}~2",
                                                 "fixed": f"{ref}~1", "ref": ref}))
        return bugs

    def check_layout(self, repo: Path, bug: Bug) -> None:
        ref = bug.extra["ref"]
        has = lambda rev: subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{rev}:bears.json"],
                                         capture_output=True).returncode == 0
        if not has(ref) or has(f"{ref}~1"):
            raise StageError("layout_unexpected", f"{ref}: bears.json não está só no último commit")
