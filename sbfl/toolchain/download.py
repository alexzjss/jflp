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
from ..winpath import inside
from .detect import _is_windows

_TIMEOUT = 60
_CHUNK = 1 << 16
_HEADERS = {"User-Agent": "jflp"}

# URL -> SHA-256 fixo (o download é recusado se o arquivo não conferir).
_JAGUAR1_JARS = {
    "https://github.com/saeg/jaguar/releases/download/jaguar-0.0.6-4f9d6f0/"
    "br.usp.each.saeg.jaguar.core-0.0.6-SNAPSHOT-jar-with-dependencies.jar":
        "cc7e4a3f987b7cca047f8adee623592c92d1b5879ad7180b1a5ba7bffaa7941a",
    "https://github.com/saeg/jaguar/releases/download/jaguar-cli-0.0.1/jacocoagent.jar":
        "d72599cbdbd42b647f61cafbc7148321aaf157d4f62799b01cea00b8a1c24ab1",
}


# JDK 7 (legado, perfil de cobertura): Azul Zulu, pois o Adoptium não publica o 7. SHA-256 do zip oficial.
_ZULU7_RELEASE = "zulu7.56.0.11-ca-jdk7.0.352"
_ZULU7_WIN_URL = f"https://cdn.azul.com/zulu/bin/{_ZULU7_RELEASE}-win_x64.zip"
_ZULU7_WIN_SHA256 = "a0255d1e29d4ab9d685b698217d9adca6a29b2e493608d4e73feda84e7759085"
_ZULU7_WIN_SIZE = 68056121
_MAVEN_305_URL = "https://archive.apache.org/dist/maven/binaries/apache-maven-3.0.5-bin.zip"


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
    if major == 7:
        if os_name != "windows" or arch != "x64":
            raise DownloadError("O JDK 7 só tem download automático para Windows x64; "
                                "instale um JDK 7 e informe o caminho em Configurações.")
        return Download("JDK 7 (Zulu)", _ZULU7_WIN_URL, _ZULU7_WIN_SIZE, _ZULU7_WIN_SHA256, "sha256", True,
                        home.tools / f"jdk-{_ZULU7_RELEASE}", home.tools / "_dl")
    url = (f"https://api.adoptium.net/v3/assets/latest/{major}/hotspot"
           f"?os={os_name}&architecture={arch}&image_type=jdk&vendor=eclipse")
    try:
        item = json.loads(_read(url))[0]
        pkg = item["binary"]["package"]
        release = item["release_name"]
        if not pkg.get("checksum"):
            raise DownloadError(f"O Adoptium não informou o checksum do JDK {major}; "
                                "o download foi recusado por segurança.")
        return Download(f"JDK {major} (Temurin)", pkg["link"], pkg.get("size"), pkg["checksum"],
                        "sha256", True, home.tools / f"jdk-{release}", home.tools / "_dl")
    except DownloadError:
        raise
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise DownloadError(f"Resposta inesperada do Adoptium para o JDK {major}: {e}") from e


def plan_maven(home: Home, version: str) -> Download:
    if version == "3.0.5":      # fora do diretório maven-3/: só há sha1 (e md5) publicados
        text = _read(_MAVEN_305_URL + ".sha1").decode("utf-8", "replace").split()
        if not text:
            raise DownloadError("Arquivo .sha1 vazio para o Maven 3.0.5")
        return Download("Maven 3.0.5", _MAVEN_305_URL, None, text[0].lower(), "sha1", True,
                        home.tools / "apache-maven-3.0.5", home.tools / "_dl")
    url = (f"https://archive.apache.org/dist/maven/maven-3/{version}/binaries/"
           f"apache-maven-{version}-bin.zip")
    text = _read(url + ".sha512").decode("utf-8", "replace").split()
    if not text:
        raise DownloadError(f"Arquivo .sha512 vazio para o Maven {version}")
    return Download(f"Maven {version}", url, None, text[0].lower(), "sha512", True,
                    home.tools / f"apache-maven-{version}", home.tools / "_dl")


def plan_jaguar1(home: Home) -> list[Download]:
    dest = home.tools / "jaguar1/lib"
    return [Download(f"Jaguar 1 ({url.rsplit('/', 1)[1]})", url, None, sha, "sha256", False, dest,
                     home.tools / "_dl") for url, sha in _JAGUAR1_JARS.items()]


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
    if not d.checksum or not d.algo:     # falha fechada: nada é instalado sem verificação
        raise DownloadError(f"Sem checksum para {d.name}: o download foi recusado por segurança.")
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
    target = Path(os.path.abspath(root / norm))
    if not inside(target, root):
        raise DownloadError(f"Arquivo recusado, caminho fora do destino: {name}")
    return target


def safe_extract(archive: Path, dest: Path) -> None:
    """Extrai zip ou tar.gz recusando caminhos absolutos, '..' e links para fora."""
    dest.mkdir(parents=True, exist_ok=True)
    root = Path(os.path.abspath(dest))
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
                        link = os.path.abspath(base / m.linkname.replace("\\", "/"))
                        if m.linkname.startswith("/") or not inside(link, root):
                            raise DownloadError(f"Arquivo recusado, link para fora: {m.name}")
                try:
                    t.extractall(root, members=members, filter="data")
                except TypeError:  # Python sem o parâmetro filter
                    t.extractall(root, members=members)
    except DownloadError:
        raise
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as e:
        raise DownloadError(f"Não foi possível extrair {archive.name}: {e}") from e
