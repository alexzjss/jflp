from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..home import Home
from .detect import _is_windows

_TIMEOUT = 60
_CHUNK = 1 << 16
_HEADERS = {"User-Agent": "jflp"}

_JAGUAR1_URLS = (
    "https://github.com/saeg/jaguar/releases/download/jaguar-0.0.6-4f9d6f0/"
    "br.usp.each.saeg.jaguar.core-0.0.6-SNAPSHOT-jar-with-dependencies.jar",
    "https://github.com/saeg/jaguar/releases/download/jaguar-cli-0.0.1/jacocoagent.jar",
)


class DownloadError(Exception):
    """Falha ao baixar, verificar ou extrair (mensagem em português)."""


@dataclass
class Download:
    name: str
    url: str
    size: int | None
    checksum: str | None
    algo: str | None
    archive: bool
    dest: Path
    cache: Path | None = None  # pasta dos .part (padrão: home.tools/_dl)


def _open(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=_TIMEOUT)


def _read(url: str) -> bytes:
    try:
        with _open(url) as r:
            return r.read()
    except Exception as e:
        raise DownloadError(f"Não foi possível consultar {url}: {e}") from e


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "aarch64"
    return "x64"


def plan_jdk(home: Home, major: int, os_name: str | None = None, arch: str | None = None) -> Download:
    os_name = os_name or ("windows" if _is_windows() else "linux")
    arch = arch or _arch()
    url = (f"https://api.adoptium.net/v3/assets/latest/{major}/hotspot"
           f"?os={os_name}&architecture={arch}&image_type=jdk&vendor=eclipse")
    try:
        item = json.loads(_read(url))[0]
        pkg = item["binary"]["package"]
        release = item["release_name"]
        return Download(f"JDK {major} (Temurin)", pkg["link"], pkg.get("size"), pkg.get("checksum"),
                        "sha256", True, home.tools / f"jdk-{release}", home.tools / "_dl")
    except DownloadError:
        raise
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise DownloadError(f"Resposta inesperada do Adoptium para o JDK {major}: {e}") from e


def plan_maven(home: Home, version: str) -> Download:
    url = (f"https://archive.apache.org/dist/maven/maven-3/{version}/binaries/"
           f"apache-maven-{version}-bin.zip")
    text = _read(url + ".sha512").decode("utf-8", "replace").split()
    if not text:
        raise DownloadError(f"Arquivo .sha512 vazio para o Maven {version}")
    return Download(f"Maven {version}", url, None, text[0].lower(), "sha512", True,
                    home.tools / f"apache-maven-{version}", home.tools / "_dl")


def plan_jaguar1(home: Home) -> list[Download]:
    dest = home.tools / "jaguar1/lib"
    return [Download(f"Jaguar 1 ({url.rsplit('/', 1)[1]})", url, None, None, None, False, dest,
                     home.tools / "_dl") for url in _JAGUAR1_URLS]


def _rm(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        try:
            p.unlink()
        except OSError:
            pass


def fetch(d: Download, progress: Callable[[int, int | None], None] | None = None) -> Path:
    """Baixa, verifica o checksum e instala em d.dest; nunca deixa dest parcial."""
    cache = d.cache or d.dest.parent / "_dl"
    fname = urllib.parse.urlparse(d.url).path.rsplit("/", 1)[-1] or "download"
    part = cache / (fname + ".part")
    tmp: Path | None = None
    try:
        if d.archive and d.dest.exists():
            raise DownloadError(f"{d.dest} já existe; remova-o antes de baixar de novo")
        cache.mkdir(parents=True, exist_ok=True)
        _download(d, part, progress)
        if d.archive:
            d.dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=f".{d.dest.name}.", suffix=".tmp", dir=d.dest.parent))
            archive = part.with_name(fname)
            os.replace(part, archive)
            part = archive
            safe_extract(part, tmp)
            entries = list(tmp.iterdir())
            root = entries[0] if len(entries) == 1 and entries[0].is_dir() else tmp
            os.replace(root, d.dest)
        else:
            d.dest.mkdir(parents=True, exist_ok=True)
            os.replace(part, d.dest / fname)
        return d.dest
    except DownloadError:
        raise
    except Exception as e:
        raise DownloadError(f"Falha ao baixar {d.name}: {e}") from e
    finally:
        _rm(part)
        if tmp is not None:
            _rm(tmp)


def _download(d: Download, part: Path, progress) -> None:
    h = hashlib.new(d.algo) if d.checksum and d.algo else None
    done = 0
    try:
        with _open(d.url) as r, open(part, "wb") as f:
            total = d.size
            try:
                total = int(r.headers.get("Content-Length")) if r.headers.get("Content-Length") else total
            except (TypeError, ValueError):
                pass
            while True:
                chunk = r.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                if h:
                    h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
            if progress and done == 0:
                progress(0, total)
    except Exception as e:
        raise DownloadError(f"Falha de rede ao baixar {d.url}: {e}") from e
    if h and h.hexdigest().lower() != d.checksum.lower():
        raise DownloadError(
            f"Checksum {d.algo} não confere para {d.name} (esperado {d.checksum}, obtido {h.hexdigest()})")


def _check_name(root: Path, name: str) -> Path:
    norm = name.replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":") or ".." in norm.split("/"):
        raise DownloadError(f"Arquivo recusado, caminho inseguro: {name}")
    target = (root / norm).resolve()
    if target != root and root not in target.parents:
        raise DownloadError(f"Arquivo recusado, caminho fora do destino: {name}")
    return target


def safe_extract(archive: Path, dest: Path) -> None:
    """Extrai zip ou tar.gz recusando caminhos absolutos, '..' e links para fora."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as z:
                infos = z.infolist()
                for i in infos:
                    _check_name(root, i.filename)
                    if stat.S_ISLNK(i.external_attr >> 16):
                        raise DownloadError(f"Arquivo recusado, link simbólico: {i.filename}")
                for i in infos:
                    target = _check_name(root, i.filename)
                    if i.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(i) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                    mode = (i.external_attr >> 16) & 0o777
                    if mode and not _is_windows():
                        target.chmod(mode)
        else:
            with tarfile.open(archive, "r:*") as t:
                members = t.getmembers()
                for m in members:
                    target = _check_name(root, m.name)
                    if m.isdev() or m.isfifo():
                        raise DownloadError(f"Arquivo recusado, tipo especial: {m.name}")
                    if m.issym() or m.islnk():
                        base = target.parent if m.issym() else root
                        link = (base / m.linkname.replace("\\", "/")).resolve()
                        if m.linkname.startswith("/") or (link != root and root not in link.parents):
                            raise DownloadError(f"Arquivo recusado, link para fora: {m.name}")
                try:
                    t.extractall(root, members=members, filter="data")
                except TypeError:  # Python sem o parâmetro filter
                    t.extractall(root, members=members)
    except DownloadError:
        raise
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as e:
        raise DownloadError(f"Não foi possível extrair {archive.name}: {e}") from e
