"""Leitura, edição e serialização de pom.xml preservando comentários e namespace."""
from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

POM_NS = "http://maven.apache.org/POM/4.0.0"
DEFAULT_GROUP = "org.apache.maven.plugins"
ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


class PomDoc:
    """Um pom.xml em memória (ElementTree com comentários)."""

    def __init__(self, root: ET.Element):
        self.root = root
        self.ns = root.tag[1:root.tag.index("}")] if root.tag.startswith("{") else ""
        if self.ns:                    # namespace do POM como padrão: saída sem prefixo ns0:
            ET.register_namespace("", self.ns)

    @classmethod
    def from_text(cls, text: str | bytes) -> PomDoc:
        data = text.encode("utf-8") if isinstance(text, str) else text
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        parser.feed(data)
        return cls(parser.close())

    @classmethod
    def load(cls, path: Path) -> PomDoc:
        return cls.from_text(Path(path).read_bytes())

    # ---------------------------------------------------------------- navegação
    def q(self, tag: str) -> str:
        return f"{{{self.ns}}}{tag}" if self.ns else tag

    def child(self, parent: ET.Element, tag: str, create: bool = False) -> ET.Element | None:
        el = parent.find(self.q(tag))
        if el is None and create:
            el = ET.SubElement(parent, self.q(tag))
        return el

    def set_child(self, parent: ET.Element, tag: str, value: str) -> ET.Element:
        el = self.child(parent, tag, create=True)
        el.text = value
        return el

    def path(self, parent: ET.Element, *tags: str, create: bool = False) -> ET.Element | None:
        el = parent
        for t in tags:
            el = self.child(el, t, create)
            if el is None:
                return None
        return el

    def _plugins_el(self, in_management: bool, create: bool = False) -> ET.Element | None:
        tags = ("build", "pluginManagement", "plugins") if in_management else ("build", "plugins")
        return self.path(self.root, *tags, create=create)

    # ---------------------------------------------------------------- plugins
    def _is(self, el: ET.Element, group: str, artifact: str, default_group: str | None) -> bool:
        if _text(self.child(el, "artifactId")) != artifact:
            return False
        g = _text(self.child(el, "groupId")) or default_group
        return g == group

    def find_plugin(self, group: str, artifact: str, in_management: bool = False):
        plugins = self._plugins_el(in_management)
        if plugins is None:
            return None
        for p in plugins.findall(self.q("plugin")):
            if self._is(p, group, artifact, DEFAULT_GROUP):
                return p
        return None

    def ensure_plugin(self, group: str, artifact: str, version: str | None,
                      overwrite: bool = True) -> ET.Element:
        """Plugin em `build/plugins` do próprio módulo (nunca mexe em pluginManagement)."""
        p = self.find_plugin(group, artifact)
        if p is None:
            p = ET.SubElement(self._plugins_el(False, create=True), self.q("plugin"))
            self.set_child(p, "groupId", group)
            self.set_child(p, "artifactId", artifact)
        if version and (overwrite or not _text(self.child(p, "version"))):
            self.set_child(p, "version", version)
        return p

    # ---------------------------------------------------------------- dependências
    def ensure_dependency(self, group: str, artifact: str, version: str | None,
                          scope: str | None = None) -> ET.Element:
        deps = self.child(self.root, "dependencies", create=True)
        d = next((x for x in deps.findall(self.q("dependency"))
                  if self._is(x, group, artifact, None)), None)
        if d is None:
            d = ET.SubElement(deps, self.q("dependency"))
            self.set_child(d, "groupId", group)
            self.set_child(d, "artifactId", artifact)
        if version:
            self.set_child(d, "version", version)
        if scope:
            self.set_child(d, "scope", scope)
        return d

    # ---------------------------------------------------------------- build / propriedades
    def set_build_value(self, tag: str, value: str) -> None:
        self.set_child(self.child(self.root, "build", create=True), tag, value)

    def property(self, name: str) -> str | None:
        el = self.path(self.root, "properties", name)
        return _text(el) if el is not None else None

    # ---------------------------------------------------------------- saída
    def serialize(self) -> str:
        ET.indent(self.root, space="  ")
        buf = io.StringIO()
        ET.ElementTree(self.root).write(buf, encoding="unicode", xml_declaration=False)
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + buf.getvalue() + "\n"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _coords(doc: PomDoc) -> tuple[str, str]:
    """(groupId, artifactId) efetivos do POM (groupId herdado do pai se omitido)."""
    parent = doc.child(doc.root, "parent")
    g = _text(doc.child(doc.root, "groupId")) or (_text(doc.child(parent, "groupId")) if parent is not None else "")
    return g, _text(doc.child(doc.root, "artifactId"))


def chain(module_dir: Path) -> list[Path]:
    """pom.xml do módulo seguido dos pais alcançáveis no disco (via relativePath)."""
    module_dir = Path(module_dir)
    out: list[Path] = []
    seen: set[Path] = set()
    pom = module_dir / "pom.xml"
    expected: tuple[str, str] | None = None
    while pom.is_file() and pom.resolve() not in seen:
        try:
            doc = PomDoc.load(pom)
        except ET.ParseError:
            break
        if expected is not None and _coords(doc) != expected:
            break
        seen.add(pom.resolve())
        out.append(pom)
        parent = doc.child(doc.root, "parent")
        if parent is None:
            break
        rel = doc.child(parent, "relativePath")
        rel_text = _text(rel) if rel is not None else "../pom.xml"
        if not rel_text:               # <relativePath/> vazio: o pai não está no disco
            break
        expected = (_text(doc.child(parent, "groupId")), _text(doc.child(parent, "artifactId")))
        nxt = (pom.parent / rel_text)
        pom = nxt / "pom.xml" if nxt.is_dir() else nxt
    return out


# ------------------------------------------------------------------ contexto
_PKG = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_JUNIT4 = re.compile(r"<groupId>\s*junit\s*</groupId>\s*<artifactId>\s*junit\s*</artifactId>")
_JUNIT5 = re.compile(r"org\.junit\.jupiter|junit-jupiter")
MAX_SAMPLES = 200      # arquivos lidos por categoria (teste / principal)


@dataclass
class PomContext:
    module_dir: Path
    chain_texts: list[str]
    jdk: int
    versions: dict
    test_dir: str | None
    main_dir: str | None
    has_junit4: bool
    has_junit5_only: bool
    surefire_argline: str | None


def is_test_name(stem: str) -> bool:
    return stem.endswith(("Test", "Tests", "TestCase")) or stem.startswith("Test")


def _java_root(path: Path) -> Path | None:
    """Raiz de fontes de um .java: o diretório do arquivo sem o caminho do `package`."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return None
    m = _PKG.search(head)
    parent = path.parent
    if not m:
        return parent
    parts = m.group(1).split(".")
    if len(parent.parts) < len(parts) or list(parent.parts[-len(parts):]) != parts:
        return None
    return Path(*parent.parts[:-len(parts)])


def detect_source_dirs(module_dir: Path) -> tuple[str | None, str | None]:
    """(test_dir, main_dir) relativos ao módulo, inferidos do `package` dos .java em src/."""
    src = Path(module_dir) / "src"
    if not src.is_dir():
        return None, None
    tests: Counter = Counter()
    mains: Counter = Counter()
    n_test = n_main = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith(".java"):
                continue
            is_test = is_test_name(name[:-5])
            if (n_test if is_test else n_main) >= MAX_SAMPLES:
                continue
            root = _java_root(Path(dirpath) / name)
            if root is None or src not in (root, *root.parents):
                continue
            if is_test:
                n_test += 1
                tests[root] += 1
            else:
                n_main += 1
                mains[root] += 1
        if n_test >= MAX_SAMPLES and n_main >= MAX_SAMPLES:
            break

    def rel(p: Path) -> str:
        return p.relative_to(module_dir).as_posix()

    main = mains.most_common(1)[0][0] if mains else None
    for r in [r for r, _ in tests.most_common() if r != main]:
        return rel(r), (rel(main) if main else None)
    return None, (rel(main) if main else None)


def _surefire_argline(texts: list[str]) -> str | None:
    for text in texts:
        try:
            doc = PomDoc.from_text(text)
        except ET.ParseError:
            continue
        for mgmt in (False, True):
            p = doc.find_plugin(DEFAULT_GROUP, "maven-surefire-plugin", in_management=mgmt)
            if p is None:
                continue
            a = doc.path(p, "configuration", "argLine")
            if a is not None and _text(a):
                return _text(a)
    return None


def build_context(module_dir: Path, settings, jdk: int) -> PomContext:
    module_dir = Path(module_dir)
    texts = [_read(p) for p in chain(module_dir)]
    joined = "\n".join(texts)
    has4 = bool(_JUNIT4.search(joined))
    test_dir, main_dir = detect_source_dirs(module_dir)
    versions = {k: settings.get("jaguar2", k) for k in ("version", "jacoco", "surefire", "compiler", "junit")}
    return PomContext(module_dir, texts, jdk, versions, test_dir, main_dir, has4,
                      bool(_JUNIT5.search(joined)) and not has4, _surefire_argline(texts))
