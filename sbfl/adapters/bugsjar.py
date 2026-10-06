"""Bugs.jar / bugs-dot-jar: um clone git por projeto, uma branch por bug
(`bugs-dot-jar_<ISSUE>_<hash-do-fix>`).

Configuração mais simples: `root` (pasta com os clones) e, opcionalmente, `projects`;
`python -m sbfl fetch` clona o que faltar a partir de `url_template`."""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..errors import StageError
from ..gitutil import git
from ..truth import crosscheck_classes
from .base import Bug
from .gitpair import GitPairAdapter

PATCH_FILE = "developer-patch.diff"


def configured_repos(cfg) -> list[Path]:
    c = cfg.section("bugsjar")
    if c.get("repos"):
        return [Path(p).expanduser() for p in c["repos"]]
    if not c.get("root"):
        return []
    root = Path(c["root"]).expanduser()
    if not root.exists():
        return []
    names = c.get("projects") or sorted(p.name for p in root.iterdir() if (p / ".git").exists())
    return [root / n for n in names if (root / n / ".git").exists()]


def fetch_repos(cfg, only: str | None = None):
    """Clona (ou atualiza) cada projeto do Bugs.jar. Gera (projeto, ação)."""
    c = cfg.section("bugsjar")
    if not c.get("root") or not c.get("projects"):
        raise StageError("config", "defina [bugsjar] root e projects no config.toml")
    root = Path(c["root"]).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    for proj in c["projects"]:
        if only and proj != only:
            continue
        dest = root / proj
        if (dest / ".git").exists():
            r = subprocess.run(["git", "-C", str(dest), "fetch", "--all", "--prune", "-q"],
                               capture_output=True, text=True)
            action = "atualizado"
        else:
            url = c["url_template"].format(project=proj)
            r = subprocess.run(["git", "-c", "core.longpaths=true", "clone", "-q", url, str(dest)],
                               capture_output=True, text=True)
            action = "clonado"
        if r.returncode != 0:
            raise StageError("git_failed", f"{proj}: {r.stderr.strip()[:300]}")
        yield proj, action


class BugsJar(GitPairAdapter):
    name = "bugsjar"
    section = "bugsjar"

    def list_bugs(self, project=None, bug=None):
        bugs = []
        for repo in configured_repos(self.cfg):
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

    def provenance(self, repo, bug, wt) -> dict:
        d, out = Path(wt) / ".bugs-dot-jar", {}
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.is_file() and f.stat().st_size <= 1_000_000:
                    out[f.name] = f.read_text(encoding="utf-8", errors="replace")
        return out

    def crosscheck(self, prov, faults):
        return crosscheck_classes(prov.get(PATCH_FILE), faults)
