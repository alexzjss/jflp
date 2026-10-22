"""Preparação de bugs: checkout único (pristine), módulo, testes relevantes e verdade.
As execuções trabalham em cópias descartáveis; o pristine nunca é tocado."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .adapters.base import Bug
from .diffgt import FaultLine, TEST_PATH_RE, faults_from_hunks, parse_left_hunks
from .errors import StageError
from .gitutil import add_worktree, git
from .home import Home, Settings
from .toolchain.detect import defects4j_cmd, wsl_status
from .truth import git_truth
from .winpath import long_path

MARKER = "prepared.json"
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _ignore_names(directory, names) -> set[str]:
    """Nomes a não copiar: `.idea` e o `target` de build de um módulo (o que está ao lado de um pom.xml).
    Um `target/` sem pom.xml ao lado, e qualquer `*.class`, são entradas de teste e ficam."""
    skip = {n for n in names if n == ".idea"}
    if "pom.xml" in names and "target" in names:
        skip.add("target")
    return skip


def _ignore_local(directory, names) -> set[str]:
    """Pristine de pasta local: sem nenhum `.git` (de sub-repositórios também); só o código importa."""
    return _ignore_names(directory, names) | {n for n in names if n == ".git"}


def _ignore_work(directory, names) -> set[str]:
    return _ignore_names(directory, names) | {n for n in names if n in (".git", ".bugs-dot-jar", MARKER)}


def tree_signature(src: Path) -> str:
    """Assinatura barata da árvore que seria copiada: nº de arquivos, mtime mais novo e tamanho total.
    Muda quando um arquivo é criado, removido ou editado (o app não relê o conteúdo)."""
    count, newest, size = 0, 0, 0
    for root, dirs, files in os.walk(long_path(src)):
        skip = _ignore_names(root, set(dirs) | set(files)) | {".git"}
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            if f in skip:
                continue
            try:
                st = os.stat(os.path.join(root, f))
            except OSError:
                continue
            count += 1
            size += st.st_size
            newest = max(newest, st.st_mtime_ns)
    return f"{count}:{newest}:{size}"


def folder_too_big(src: Path, max_bytes: int, max_files: int) -> str | None:
    """Percorre a pasta como a cópia a faria (sem .git/target/.idea) e para assim que passa de um limite.
    Devolve o motivo em português ou None se cabe."""
    count, size = 0, 0
    for root, dirs, files in os.walk(long_path(src)):
        skip = _ignore_local(root, set(dirs) | set(files))
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            if f in skip:
                continue
            count += 1
            try:
                size += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
            if count > max_files:
                return f"mais de {max_files} arquivos"
            if size > max_bytes:
                return f"mais de {max_bytes / 1048576:.0f} MB"
    return None


_D4J_TIMEOUT = 3600


@dataclass
class Prepared:
    bug: Bug
    pristine: Path
    module: str
    relevant_tests: list[str] | None
    truth: list[FaultLine]
    info: dict = field(default_factory=dict)


def to_wsl_path(p) -> str:
    """`E:\\a\\b` -> `/mnt/e/a/b`."""
    s = str(p)
    m = re.match(r"^([A-Za-z]):[\\/]*(.*)$", s)
    if not m:
        return s.replace("\\", "/")
    return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")


def _fail(detail: str) -> StageError:
    return StageError("checkout_falhou", detail)


def _log(log: Path | None, text: str) -> None:
    if log is None or not text:
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8", errors="replace") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def _rmtree(path, strict: bool = True) -> None:
    """rmtree que apaga também arquivos somente-leitura (objetos do .git no Windows)."""
    def fix(func, p, *_):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass

    path = Path(path)
    lpath = long_path(path)
    if not os.path.exists(lpath):
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(lpath, onexc=fix)
    else:
        shutil.rmtree(lpath, onerror=fix)
    if strict and os.path.exists(lpath):
        raise _fail(f"Não foi possível apagar a pasta {path}; feche programas que a estejam usando.")


def _pristine_dir(home: Home, bug: Bug) -> Path:
    return home.work / "p" / hashlib.sha1(bug.key.encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------- Defects4J

class _D4j:
    """Chamadas ao CLI do Defects4J (via WSL no Windows)."""

    def __init__(self, settings: Settings, log: Path | None = None):
        wsl = wsl_status(settings)
        prefix = defects4j_cmd(settings, wsl)
        if prefix is None:
            msg = wsl.message if wsl and wsl.message and wsl.message != "não se aplica" else ""
            raise _fail(msg or "Defects4J não encontrado")
        self.prefix, self.wsl, self.log = prefix, wsl, log
        self.windows = os.name == "nt"

    def run(self, *args, check: bool = True) -> str:
        conv = [to_wsl_path(a) if isinstance(a, Path) and self.windows else str(a) for a in args]
        if self.windows:
            cmd = self.prefix + [shlex.join([self.wsl.d4j or "defects4j", *conv])]
        else:
            cmd = self.prefix + conv
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=_D4J_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            raise _fail(f"Falha ao executar o Defects4J: {e}")
        _log(self.log, f"$ defects4j {' '.join(conv)}\n{r.stdout}{r.stderr}")
        if check and r.returncode != 0:
            raise _fail(f"defects4j {' '.join(conv[:2])} falhou: {(r.stderr or r.stdout).strip()[-400:]}")
        return r.stdout


def _src_truth(buggy_src: Path, fixed_src: Path) -> list[FaultLine]:
    """Verdade a partir de duas árvores de código-fonte (raízes dos pacotes)."""
    hunks: dict = {}
    for p in sorted(buggy_src.rglob("*.java")):
        rel = p.relative_to(buggy_src).as_posix()
        q = fixed_src / rel
        if not q.exists() or p.read_bytes() == q.read_bytes():
            continue
        d = subprocess.run(["git", "-c", "core.quotepath=false", "diff", "--no-index", "-U0",
                            "--no-color", "--no-renames", "--", str(p), str(q)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
        d = re.sub(r"^--- .*$", f"--- a/{rel}", d, count=1, flags=re.M)
        hunks.update(parse_left_hunks(d, lambda x: x if x.endswith(".java") else None))
    faults = faults_from_hunks(hunks, lambda x: x[:-5].replace("/", "."))
    if not faults:
        raise _fail("Diff entre as versões com defeito e corrigida sem linhas em código de produção.")
    return faults


def _d4j_prepare(home: Home, settings: Settings, bug: Bug, dest: Path, log: Path) -> Prepared:
    d4j = _D4j(settings, log)
    tmp = home.work / "p" / (dest.name + "_f")
    _rmtree(tmp, strict=False)
    try:
        d4j.run("checkout", "-p", bug.project, "-v", f"{bug.bug_id}b", "-w", dest)
        d4j.run("checkout", "-p", bug.project, "-v", f"{bug.bug_id}f", "-w", tmp)
        out = d4j.run("export", "-p", "dir.src.classes", "-w", dest).strip().splitlines()
        src_rel = out[-1].strip() if out else "src/main/java"
        faults = _src_truth(dest / src_rel, tmp / src_rel)
        rel = [x for x in d4j.run("export", "-p", "tests.relevant", "-w", dest, check=False).split() if x]
    finally:
        _rmtree(tmp, strict=False)
    info: dict = {}
    if not (dest / "pom.xml").exists():
        info["jaguar2_suportado"] = False
    return Prepared(bug, dest, ".", rel or None, faults, info)


# ------------------------------------------------------------------ Bugs.jar

def _bugsjar_repos(settings: Settings) -> list[Path]:
    root = settings.get("bugsjar", "root")
    if not root:
        return []
    root = Path(root).expanduser()
    if not root.exists():
        return []
    names = settings.get("bugsjar", "projects") or sorted(
        p.name for p in root.iterdir() if (p / ".git").exists())
    return [root / n for n in names if (root / n / ".git").exists()]


def _bugsjar_bugs(settings: Settings, project: str | None = None) -> list[Bug]:
    bugs = []
    for repo in _bugsjar_repos(settings):
        if project and repo.name != project:
            continue
        seen = {}
        for r in git(repo, "branch", "-a", "--format=%(refname:short)").split():
            n = r.split("origin/", 1)[-1] if r.startswith("origin/") else r
            if n.startswith("bugs-dot-jar_") and (n not in seen or r.startswith("origin/")):
                seen[n] = r
        for n, ref in sorted(seen.items()):
            bugs.append(Bug("bugsjar", repo.name, n,
                            {"repo": str(repo), "buggy": ref, "fixed": n.rsplit("_", 1)[-1]}))
    return bugs


_TEST_ROOTS = ("src/test/java/", "src/test/", "src/tests/", "test/java/", "tests/", "test/", "java/")


def _test_fqcn(path: str) -> str | None:
    """FQCN de um arquivo de teste pela raiz mais específica (última ocorrência)."""
    p = "/" + path.replace("\\", "/")
    if not p.endswith(".java"):
        return None
    for root in _TEST_ROOTS:
        i = p.rfind("/" + root)
        if i >= 0:
            return p[i + 1 + len(root):-5].replace("/", ".")
    return None


def _changed_tests(wt: Path, fixed: str) -> list[str]:
    names = git(wt, "-c", "core.quotepath=false", "show", "--name-only", "--diff-filter=AMR",
                "--format=", fixed).split("\n")
    out = {_test_fqcn(n.strip()) for n in names if n.strip().endswith(".java") and TEST_PATH_RE.search(n)}
    return sorted(c for c in out if c)


def _worktree_prepare(bug: Bug, repo: Path, dest: Path, ref: str, fixed: str | None) -> Prepared:
    """Worktree em `dest`; com `fixed`, calcula verdade e testes relevantes."""
    add_worktree(repo, dest, ref)
    info = {"worktree_repo": str(repo)}
    if not fixed:
        return Prepared(bug, dest, ".", None, [], info)
    faults, module = git_truth(dest, "HEAD", fixed)
    return Prepared(bug, dest, module, _changed_tests(dest, fixed) or None, faults, info)


def _bugsjar_prepare(bug: Bug, dest: Path) -> Prepared:
    e = bug.extra
    repo = Path(e["repo"])
    if not repo.exists():
        raise _fail(f"Repositório do Bugs.jar não encontrado: {repo}")
    return _worktree_prepare(bug, repo, dest, e["buggy"], e.get("fixed"))


# --------------------------------------------------------------------- Local

_NAO_PROJETO = {"target", "node_modules", "src", "build", "out"}


class PomList(list):
    """Lista de projetos achados; `mais` = quantos passaram do limite (a tela mostra "e mais N")."""
    mais = 0


def find_poms(folder: Path, depth: int = 2, limit: int = 50) -> list[str]:
    """Projetos (pastas com pom.xml, até `depth` níveis, sem ocultas) como caminhos relativos com `/`.
    Não desce numa pasta que já tem pom.xml: os filhos dela são módulos, não projetos."""
    out: list[str] = []

    def walk(d: Path, rel: str, level: int) -> None:
        try:
            subs = sorted(x for x in d.iterdir() if x.is_dir())
        except OSError:
            return
        for x in subs:
            if x.name.startswith(".") or x.name in _NAO_PROJETO:
                continue
            r = f"{rel}/{x.name}" if rel else x.name
            if (x / "pom.xml").is_file():
                out.append(r)
            elif level < depth:
                walk(x, r, level + 1)

    walk(Path(folder), "", 1)
    out.sort()
    res = PomList(out[:limit])
    res.mais = max(0, len(out) - limit)
    return res


def is_bugsjar_repo(folder: Path) -> bool:
    """Repositório do Bugs.jar: tem a pasta `.bugs-dot-jar/`."""
    return (Path(folder) / ".bugs-dot-jar").is_dir()


def is_bugsjar_collection(folder: Path) -> bool:
    """Raiz do Bugs.jar: pasta que não é um repositório dele, mas com subpastas que são (`.git` + `.bugs-dot-jar`)."""
    folder = Path(folder)
    if is_bugsjar_repo(folder):
        return False
    try:
        return any((x / ".git").exists() and is_bugsjar_repo(x) for x in folder.iterdir() if x.is_dir())
    except OSError:
        return False

def _norm_module(module: str | None, base: Path | None) -> str | None:
    """Módulo opcional: relativo, sem `..`, com `/`. Numa pasta local, `<pasta>/<módulo>/pom.xml` precisa existir."""
    m = (module or "").strip().strip('"').replace("\\", "/")
    parts = [x for x in m.split("/") if x and x != "."]
    if not parts:
        return None
    if m.startswith("/") or re.match(r"^[A-Za-z]:", m) or ".." in parts:
        raise ValueError(f"Módulo inválido (use uma subpasta relativa, sem '..'): {module}")
    m = "/".join(parts)
    if base is not None and not (base / m / "pom.xml").is_file():
        raise ValueError(f"Módulo sem pom.xml: {base / m}")
    return m


def local_bug(spec: str, module: str | None = None) -> Bug:
    """`spec`: pasta existente ou `URL@commit`; `module`: subpasta com pom.xml (opcional)."""
    p = Path(spec).expanduser()
    if p.is_dir():
        p = p.resolve()
        mod = _norm_module(module, None)
        if mod and not (p / "pom.xml").is_file() and (p / mod / "pom.xml").is_file():
            # raiz sem pom.xml (ex.: coleção de repositórios): o módulo é o próprio projeto,
            # copiado e executado sozinho, sem reactor
            p, mod = p / mod, None
        else:
            mod = _norm_module(module, p)
        # sem módulo a chave é a de sempre (bugs locais já registrados continuam valendo)
        h = hashlib.sha1((str(p) + (f"|{mod}" if mod else "")).encode("utf-8")).hexdigest()[:8]
        extra = {"path": str(p)}
        if mod:
            extra["module"] = mod
        return Bug("local", p.name, f"dir-{h}", extra)
    if "@" in spec:
        url, commit = spec.rsplit("@", 1)
        if url and commit and "/" not in commit:
            name = url.rstrip("/").removesuffix(".git").replace("\\", "/").rsplit("/", 1)[-1]
            name = name.rsplit(":", 1)[-1] or "repo"
            mod = _norm_module(module, None)
            key = commit[:12]
            extra = {"repo": url, "commit": commit}
            if mod:
                key += "-m" + hashlib.sha1(mod.encode("utf-8")).hexdigest()[:6]
                extra["module"] = mod
            return Bug("local", name, key, extra)
    raise ValueError(f"Entrada local inválida (use uma pasta existente ou URL@commit): {spec}")


def _local_prepare(home: Home, settings: Settings, bug: Bug, dest: Path, log: Path) -> Prepared:
    e = bug.extra
    if e.get("path"):
        src = Path(e["path"])
        if not src.is_dir():
            raise _fail(f"Pasta não encontrada: {src}")
        motivo = folder_too_big(src, int(float(settings.get("run", "max_copy_mb", 2048)) * 1048576),
                                int(settings.get("run", "max_copy_files", 200000)))
        if motivo:
            raise _fail(f"A pasta do projeto é muito grande para copiar ({motivo}): {src}. "
                        "Escolha a subpasta do projeto ou informe um módulo; os limites ficam em "
                        "run.max_copy_mb e run.max_copy_files no config.toml.")
        sig = tree_signature(src)
        info = {"source_signature": sig}
        if (src / ".git").exists():     # o pristine não leva o .git: guarda o commit para o env.json
            try:
                commit = git(src, "rev-parse", "HEAD", check=False).strip()
            except OSError:
                commit = ""
            if commit:
                info["source_commit"] = commit
        shutil.copytree(long_path(src), long_path(dest), ignore=_ignore_local)
        return Prepared(bug, dest, e.get("module") or ".", None, [], info)
    url, commit = e["repo"], e["commit"]
    is_path = Path(url).expanduser().exists()
    src = str(Path(url).expanduser().resolve()) if is_path else url
    safe = re.sub(r"[^\w.-]+", "_", src.rstrip("/\\").removesuffix(".git").split("://")[-1])[-60:]
    repo = home.work / "clones" / f"{safe}-{hashlib.sha1(src.encode('utf-8')).hexdigest()[:6]}"
    if not repo.exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        args = ["git", "-c", "core.longpaths=true", "clone", "-q"] + (["--no-hardlinks"] if is_path else [])
        r = subprocess.run(args + [src, str(repo)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        _log(log, r.stdout + r.stderr)
        if r.returncode != 0:
            _rmtree(repo, strict=False)
            raise _fail(f"Não foi possível clonar {url}: {r.stderr.strip()[:300]}")
    prep = _worktree_prepare(bug, repo, dest, commit, None)
    if e.get("module"):
        prep.module = e["module"]
    return prep


# ------------------------------------------------------------------- Público

def _marker_data(prep: Prepared) -> dict:
    return {"pristine": str(prep.pristine), "module": prep.module,
            "relevant_tests": prep.relevant_tests,
            "truth": [f.to_dict() for f in prep.truth], "info": prep.info}


def _local_changed(bug: Bug, marker: Path) -> bool:
    """Bug local por pasta: a origem mudou desde a cópia (assinatura da árvore diferente)?"""
    path = bug.extra.get("path") if bug.benchmark == "local" else None
    if not path or not Path(path).is_dir():
        return False
    try:
        old = json.loads(marker.read_text(encoding="utf-8")).get("info", {}).get("source_signature")
    except (OSError, ValueError):
        return False
    return old != tree_signature(Path(path))


def _remove_pristine(dest: Path, repo: str | None = None) -> None:
    if repo is None:
        try:
            repo = json.loads((dest / MARKER).read_text(encoding="utf-8")).get("info", {}).get("worktree_repo")
        except (OSError, ValueError):
            pass
    if repo and Path(repo).exists() and dest.exists():
        git(repo, "worktree", "remove", "--force", str(dest), check=False)
    _rmtree(dest)
    if repo and Path(repo).exists():
        git(repo, "worktree", "prune", check=False)


def prepare(home: Home, settings: Settings, bug: Bug, log: Path, refresh: bool = False) -> Prepared:
    dest = _pristine_dir(home, bug)
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(str(dest), threading.Lock())
    with lock:  # duas threads preparando o mesmo bug não se atropelam
        return _prepare_locked(home, settings, bug, log, refresh, dest)


def _prepare_locked(home: Home, settings: Settings, bug: Bug, log: Path, refresh: bool,
                    dest: Path) -> Prepared:
    marker = dest / MARKER
    if refresh and dest.exists():
        _remove_pristine(dest)
    if marker.exists() and _local_changed(bug, marker):
        _remove_pristine(dest)          # a pasta de origem foi editada: copia de novo
    if marker.exists():
        try:
            d = json.loads(marker.read_text(encoding="utf-8"))
            return Prepared(bug, dest, d["module"], d["relevant_tests"],
                            [FaultLine(**f) for f in d["truth"]], d.get("info", {}))
        except (OSError, ValueError, KeyError, TypeError):
            _remove_pristine(dest)
    elif dest.exists():
        _remove_pristine(dest)  # sobra de uma preparação interrompida
    dest.parent.mkdir(parents=True, exist_ok=True)
    repo = bug.extra.get("repo") if bug.benchmark == "bugsjar" else None
    try:
        if bug.benchmark == "d4j":
            prep = _d4j_prepare(home, settings, bug, dest, log)
        elif bug.benchmark == "bugsjar":
            prep = _bugsjar_prepare(bug, dest)
        elif bug.benchmark == "local":
            prep = _local_prepare(home, settings, bug, dest, log)
        else:
            raise _fail(f"Benchmark desconhecido: {bug.benchmark}")
        marker.write_text(json.dumps(_marker_data(prep), ensure_ascii=False, indent=1), encoding="utf-8")
        return prep
    except StageError as e:
        _remove_pristine(dest, repo)
        raise _fail(e.detail) if e.status != "checkout_falhou" else e
    except Exception as e:
        _remove_pristine(dest, repo)
        raise _fail(f"Falha ao preparar {bug.key}: {e}")


def working_copy(prep: Prepared, home: Home) -> Path:
    """Cópia descartável do pristine (sem o target/ de módulos, .git nem o marcador)."""
    dest = home.work / "w" / secrets.token_hex(4)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(long_path(prep.pristine), long_path(dest), ignore=_ignore_work)
    return dest


def list_bugs(settings: Settings, benchmark: str, project: str | None = None) -> list[Bug]:
    if benchmark == "bugsjar":
        return _bugsjar_bugs(settings, project)
    if benchmark == "d4j":
        d4j = _D4j(settings)
        projects = [project] if project else settings.get("defects4j", "projects")
        return [Bug("d4j", p, bid) for p in projects for bid in d4j.run("bids", "-p", p).split()]
    raise ValueError(f"Benchmark sem listagem: {benchmark}")


def bug_from_key(settings: Settings, key: str) -> Bug:
    """Inverso de `Bug.key` para d4j e bugsjar."""
    parts = key.split("/", 2)
    if len(parts) != 3:
        raise ValueError(f"Chave inválida: {key}")
    benchmark, project, bug_id = parts
    if benchmark == "d4j":
        return Bug("d4j", project, bug_id)
    if benchmark == "bugsjar":
        for b in _bugsjar_bugs(settings, project):
            if b.bug_id == bug_id:
                return b
        raise ValueError(f"Bug não encontrado no Bugs.jar: {key}")
    raise ValueError(f"Bugs locais não podem ser reconstruídos pela chave: {key}")
