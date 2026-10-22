"""Código-fonte de uma classe a partir do arquivo do checkout de uma execução (sem dependência de web)."""
from __future__ import annotations

import tarfile
import zipfile
from collections import OrderedDict
from pathlib import Path

MAX_LINES = 20_000
MAX_BYTES = 16 * 1024 * 1024      # teto de leitura bruta de um arquivo .java
CACHE_SIZE = 4
_TEST_DIRS = {"test", "tests", "src-test", "testsrc"}

_cache: OrderedDict[tuple, list[str]] = OrderedDict()


class SourceError(Exception):
    """Falha esperada ao obter o código; a mensagem já está em português."""


def checkout_file(run_dir: Path) -> Path | None:
    for name in ("checkout.zip", "checkout.tar.zst"):
        p = Path(run_dir) / name
        if p.is_file():
            return p
    return None


def _norm(name: str) -> str:
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name.lstrip("/")


def _zstd():
    try:
        import zstandard
    except ImportError:
        raise SourceError("O checkout desta execução está em .tar.zst e o módulo opcional "
                          "'zstandard' não está instalado (pip install zstandard).") from None
    return zstandard


class _Tar:
    """tar (ou tar.zst) lido em fluxo."""
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self._f = open(self.path, "rb")
        try:
            src = self._f
            if self.path.name.endswith(".zst"):
                src = _zstd().ZstdDecompressor().stream_reader(self._f)
            self._tar = tarfile.open(fileobj=src, mode="r|")
        except BaseException:
            self._f.close()
            raise
        return self._tar

    def __exit__(self, *exc):
        self._tar.close()
        self._f.close()


def _list_members(path: Path) -> list[str]:
    out: list[str] = []
    try:
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                out = [_norm(i.filename) for i in zf.infolist() if not i.is_dir()]
        else:
            with _Tar(path) as tar:
                out = [_norm(m.name) for m in tar if m.isfile()]
    except SourceError:
        raise
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError) as e:
        raise SourceError(f"Não foi possível ler o arquivo do checkout: {e}") from None
    return [n for n in out if n.endswith(".java")]


def java_members(path: Path) -> list[str]:
    """Membros .java do arquivo, com cache LRU por (caminho, mtime, tamanho)."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _cache.get(key)
    if hit is not None:
        _cache.move_to_end(key)
        return hit
    members = _list_members(path)
    _cache[key] = members
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return members


def _is_test(member: str) -> bool:
    return any(p in _TEST_DIRS for p in member.split("/")[:-1])


def find_member(members: list[str], cls: str, module: str | None = None) -> str | None:
    """Arquivo da classe externa de `cls` (a.b.C$X -> .../a/b/C.java), com preferências de desempate."""
    tail = cls.split("$", 1)[0].replace(".", "/") + ".java"
    cands = [m for m in members if m == tail or m.endswith("/" + tail)]
    if not cands:
        return None
    mod = (module or "").strip().replace("\\", "/").strip("/")
    if mod in ("", "."):
        mod = ""

    def rank(m: str):
        in_mod = bool(mod) and (m == mod or m.startswith(mod + "/") or ("/" + mod + "/") in ("/" + m))
        return (_is_test(m), not in_mod, "/src/main/java/" not in "/" + m, len(m), m)
    return min(cands, key=rank)


def read_member(path: Path, member: str) -> str:
    """Texto do membro (UTF-8 com replace); levanta SourceError se não existe."""
    data = None
    try:
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if not info.is_dir() and _norm(info.filename) == member:
                        with zf.open(info) as f:
                            data = f.read(MAX_BYTES)
                        break
        else:
            with _Tar(path) as tar:
                for m in tar:
                    if m.isfile() and _norm(m.name) == member:
                        data = tar.extractfile(m).read(MAX_BYTES)
                        break
    except SourceError:
        raise
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError) as e:
        raise SourceError(f"Não foi possível ler o arquivo do checkout: {e}") from None
    if data is None:
        raise SourceError(f"O arquivo {member} não está no checkout.")
    return data.decode("utf-8", errors="replace")


def load_source(run_dir: Path, cls: str, module: str | None = None) -> tuple[str, list[str], bool]:
    """(membro, linhas, truncado) da classe `cls` no checkout da execução; SourceError se não der."""
    path = checkout_file(run_dir)
    if path is None:
        raise SourceError("Esta execução não guardou o checkout (arquivo checkout.zip/checkout.tar.zst ausente).")
    member = find_member(java_members(path), cls, module)
    if member is None:
        raise SourceError(f"A classe {cls.split('$', 1)[0]} não foi encontrada no checkout.")
    lines = read_member(path, member).splitlines()
    truncated = len(lines) > MAX_LINES
    return member, lines[:MAX_LINES], truncated
