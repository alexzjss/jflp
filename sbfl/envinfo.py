"""Registro do ambiente da campanha (results/_env.json), para reprodutibilidade e para o relatório."""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .build import resolve_tool
from .pipeline import java_version
from .proc import make_env


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _first_line(cmd, env) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
        return (r.stdout or r.stderr).splitlines()[0]
    except Exception as e:
        return f"erro: {e}"


def snapshot(cfg) -> dict:
    lib = cfg.path("paths", "jaguar_lib")
    jars = {p.name: _sha(p) for p in sorted(lib.glob("*.jar"))} if lib.exists() else {}
    env = make_env(cfg)
    return {
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sbfl": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "java": java_version(cfg),
        "java_homes": cfg.get("java", "homes") or None,
        "maven": _first_line([resolve_tool(cfg.get("java", "mvn")), "--version"], env),
        "jaguar_jars_sha256_12": jars,
        "jaguar": {k: cfg.get("jaguar", k) for k in ("heuristic", "output_type", "test_scope",
                                                       "timeout_s", "max_retries")},
        "maven_flags": {"skip_flags": cfg.get("maven", "skip_flags"),
                        "extra_args": cfg.get("maven", "extra_args"),
                        "local_repo": cfg.get("maven", "local_repo")},
    }


def write(cfg) -> Path:
    cfg.results.mkdir(parents=True, exist_ok=True)
    p = cfg.results / "_env.json"
    p.write_text(json.dumps(snapshot(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
    return p
