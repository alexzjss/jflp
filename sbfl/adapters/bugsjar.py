"""Bugs.jar / bugs-dot-jar: um clone git por projeto, uma branch por bug
(`bugs-dot-jar_<ISSUE>_<hash-do-fix>`)."""
from __future__ import annotations

from pathlib import Path

from ..gitutil import git
from .base import Bug
from .gitpair import GitPairAdapter


class BugsJar(GitPairAdapter):
    name = "bugsjar"
    section = "bugsjar"

    def list_bugs(self, project=None, bug=None):
        bugs = []
        for repo in (Path(p).expanduser() for p in self.cfg.get("bugsjar", "repos")):
            if project and repo.name != project:
                continue
            seen = {}
            for r in git(repo, "branch", "-a", "--format=%(refname:short)").split():
                n = r.split("origin/", 1)[-1] if r.startswith("origin/") else r
                if n.startswith("bugs-dot-jar_") and (n not in seen or r.startswith("origin/")):
                    seen[n] = r
            for n, ref in sorted(seen.items()):
                if bug and bug != n:
                    continue
                bugs.append(Bug(self.name, repo.name, n,
                                {"repo": str(repo), "buggy": ref, "fixed": n.rsplit("_", 1)[-1]}))
        return bugs
