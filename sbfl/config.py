from __future__ import annotations

import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

DEFAULTS = {
    "paths": {"jaguar_lib": "", "workdir": "./work", "results": "./results"},
    "java": {"java": "java", "mvn": "mvn", "java_home": ""},
    "maven": {
        "local_repo": "",
        "skip_flags": [
            "-Drat.skip=true", "-Denforcer.skip=true", "-Dcheckstyle.skip=true",
            "-Dlicense.skip=true", "-Dmaven.javadoc.skip=true",
            "-Danimal.sniffer.skip=true", "-Dfindbugs.skip=true",
        ],
        "build_timeout_s": 3600,
    },
    "jaguar": {
        "heuristic": "Ochiai",       # a que o Jaguar calcula; as demais são recalculadas offline
        "output_type": "F",
        "log_level": "DEBUG",         # precisa de DEBUG para localizar testes que derrubam a JVM
        "timeout_s": 3600,
        "max_retries": 6,
        "test_scope": "all",          # all | changed (Bugs.jar) | relevant (Defects4J)
    },
    "analysis": {
        "heuristics": ["jaguar", "ochiai", "tarantula", "jaccard", "dstar2",
                       "op2", "zoltar", "barinel", "kulczynski2"],
    },
    "defects4j": {"home": "", "projects": []},
    "bugsjar": {"repos": []},
}


class Config:
    def __init__(self, data: dict, base: Path):
        self.data, self.base = data, base

    def get(self, section: str, key: str):
        return self.data[section][key]

    def path(self, section: str, key: str) -> Path:
        p = Path(str(self.data[section][key])).expanduser()
        return p if p.is_absolute() else (self.base / p).resolve()

    @property
    def work(self) -> Path:
        return self.path("paths", "workdir")

    @property
    def results(self) -> Path:
        return self.path("paths", "results")


def load(path: str | Path) -> Config:
    path = Path(path)
    if tomllib is None:
        sys.exit("Python 3.11+ é necessário (tomllib).")
    with open(path, "rb") as f:
        user = tomllib.load(f)
    data = {s: {**vals, **user.get(s, {})} for s, vals in DEFAULTS.items()}
    return Config(data, path.parent.resolve())
