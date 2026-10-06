from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..classfile import is_abstract_or_interface
from ..errors import StageError  # noqa: F401  (reexport)


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
    java_home: str | None = None
    provenance: dict = field(default_factory=dict)   # nome do arquivo -> texto (metadados do benchmark)


class Adapter:
    name = "base"

    def __init__(self, cfg):
        self.cfg = cfg

    def list_bugs(self, project=None, bug=None) -> list[Bug]:
        raise NotImplementedError

    def prepare(self, bug: Bug, log_path: Path) -> Ctx:
        raise NotImplementedError


def junit_flavor(data: bytes) -> str:
    """Pelo conteúdo do .class: junit5 (só Jupiter) | junit4 | junit3 | unknown."""
    j4 = b"org/junit/Test" in data or b"org/junit/runner/RunWith" in data
    if b"org/junit/jupiter/" in data and not j4:
        return "junit5"
    if j4:
        return "junit4"
    if b"junit/framework/TestCase" in data:
        return "junit3"
    return "unknown"


def scan_tests(tests_root: Path) -> tuple[list[str], dict]:
    """Classes de teste (heurística por nome), sem abstratas/interfaces e sem JUnit 5 puro.
    O JaguarRunner usa o runner do JUnit 4: testes Jupiter não executam nele."""
    out, stats = [], {"junit5_skipped": 0, "abstract_skipped": 0}
    for p in sorted(tests_root.rglob("*.class")):
        stem = p.stem
        if "$" in stem:
            continue
        if not (stem.endswith(("Test", "Tests", "TestCase")) or stem.startswith("Test")):
            continue
        if is_abstract_or_interface(p):
            stats["abstract_skipped"] += 1
            continue
        if junit_flavor(p.read_bytes()) == "junit5":
            stats["junit5_skipped"] += 1
            continue
        out.append(".".join(p.relative_to(tests_root).with_suffix("").parts))
    return out, stats


def scan_test_classes(tests_root: Path) -> list[str]:
    return scan_tests(tests_root)[0]
