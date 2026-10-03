from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..classfile import is_abstract_or_interface


class StageError(Exception):
    def __init__(self, status: str, detail: str = ""):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


@dataclass
class Bug:
    benchmark: str
    project: str
    bug_id: str
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.benchmark}/{self.project}/{self.bug_id}"


@dataclass
class Ctx:
    project_dir: Path
    classes_dir: str       # relativo a project_dir
    tests_dir: str         # relativo a project_dir
    extra_cp: list
    test_classes: list
    truth: list            # list[FaultLine]
    cleanup: Callable = lambda: None
    info: dict = field(default_factory=dict)


class Adapter:
    name = "base"

    def __init__(self, cfg):
        self.cfg = cfg

    def list_bugs(self, project=None, bug=None) -> list[Bug]:
        raise NotImplementedError

    def prepare(self, bug: Bug, log_path: Path) -> Ctx:
        raise NotImplementedError


def scan_test_classes(tests_root: Path) -> list[str]:
    """FQCNs das classes de teste (heurística por nome), ignorando abstratas/interfaces."""
    out = []
    for p in sorted(tests_root.rglob("*.class")):
        stem = p.stem
        if "$" in stem:
            continue
        if not (stem.endswith(("Test", "Tests", "TestCase")) or stem.startswith("Test")):
            continue
        if is_abstract_or_interface(p):
            continue
        out.append(".".join(p.relative_to(tests_root).with_suffix("").parts))
    return out
