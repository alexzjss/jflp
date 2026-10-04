"""Adaptador universal: qualquer coleção de bugs descrita em JSONL (um bug por linha).

{"project": "foo", "bug_id": "foo-1", "repo": "~/clones/foo",        # caminho ou URL git
 "buggy": "abc123", "fixed": "def456",                              # OU "fix_patch": "patches/foo-1.diff"
 "test_patch": "patches/foo-1.test.diff",                           # opcional: aplicado antes do build
 "build": "auto",                                                   # auto | maven | gradle | custom
 "module": "core",                                                  # opcional (senão, vem do diff)
 "java_home": "/usr/lib/jvm/java-8",                                # opcional
 "custom": {"cmds": ["ant compile"], "classes": "build/classes",    # só para build=custom
            "tests": "build/test-classes", "classpath": ["lib/*"]}}
"""
from __future__ import annotations

import json
from pathlib import Path

from ..errors import StageError
from .base import Bug
from .gitpair import GitPairAdapter

KNOWN = {"repo", "buggy", "fixed", "fix_patch", "test_patch", "build", "module", "java_home", "custom"}


class Manifest(GitPairAdapter):
    name = "manifest"
    section = "manifest"

    def list_bugs(self, project=None, bug=None):
        bugs = []
        for f in self.cfg.get("manifest", "files"):
            fp = Path(f).expanduser()
            fp = fp if fp.is_absolute() else (self.cfg.base / fp)
            for n, line in enumerate(fp.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                d = json.loads(line)
                miss = [k for k in ("project", "bug_id", "repo", "buggy") if k not in d]
                if miss or not (d.get("fixed") or d.get("fix_patch")):
                    raise StageError("manifest_invalid", f"{fp}:{n}: faltam {miss or ['fixed|fix_patch']}")
                if (project and project != d["project"]) or (bug and bug != d["bug_id"]):
                    continue
                for k in ("fix_patch", "test_patch"):
                    if d.get(k) and not Path(d[k]).expanduser().is_absolute():
                        d[k] = str((fp.parent / d[k]).resolve())
                bugs.append(Bug(d.get("benchmark", "manifest"), d["project"], d["bug_id"],
                                {k: v for k, v in d.items() if k in KNOWN}))
        return bugs
