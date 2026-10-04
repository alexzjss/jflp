from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .errors import StageError


def git(repo, *args, check=True) -> str:
    r = subprocess.run(["git", "-c", "core.longpaths=true", "-C", str(repo), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise StageError("git_failed", f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout


def ensure_repo(cfg, repo: str) -> Path:
    """Caminho local ou URL (clona uma vez em work/_clones/)."""
    p = Path(repo).expanduser()
    if p.exists():
        return p
    if "://" not in repo and not repo.startswith("git@"):
        raise StageError("repo_missing", f"{repo} não existe")
    dest = cfg.work / "_clones" / re.sub(r"[^\w.-]+", "_", repo.rstrip("/").removesuffix(".git").split("://")[-1])
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "-q", repo, str(dest)], capture_output=True, text=True)
        if r.returncode != 0:
            raise StageError("git_failed", f"clone {repo}: {r.stderr.strip()[:300]}")
    return dest


def add_worktree(repo: Path, wt: Path, ref: str):
    if wt.exists():
        git(repo, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wt, ignore_errors=True)
    wt.parent.mkdir(parents=True, exist_ok=True)
    if subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{ref}^{{commit}}"],
                      capture_output=True).returncode != 0:
        git(repo, "fetch", "--all", "-q", check=False)  # ref pode estar só no remoto
    git(repo, "worktree", "add", "--detach", "-f", str(wt), ref)

    def cleanup():
        git(repo, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wt, ignore_errors=True)

    return cleanup
