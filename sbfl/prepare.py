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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .adapters.base import Bug
from .diffgt import FaultLine, TEST_PATH_RE, faults_from_hunks, parse_left_hunks
from .errors import StageError
from .gitutil import add_worktree, git
from .home import Home, Settings
from .toolchain.detect import defects4j_cmd, wsl_status
from .truth import git_truth

MARKER = "prepared.json"
_IGNORE_COPY = shutil.ignore_patterns("target", ".idea", "*.class")
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


def _d4j_prepare(home: Home, settings: Settings, bug: Bug, dest: Path, log: Path) -> Prepared:
    d4j = _D4j(settings, log)
    tmp = home.work / "p" / (dest.name + "_f")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        d4j.run("checkout", "-p", bug.project, "-v", f"{bug.bug_id}b", "-w", dest)
        d4j.run("checkout", "-p", bug.project, "-v", f"{bug.bug_id}f", "-w", tmp)
        out = d4j.run("export", "-p", "dir.src.classes", "-w", dest).strip().splitlines()
        src_rel = out[-1].strip() if out else "src/main/java"
        diff = subprocess.run(
            ["git", "diff", "--no-index", "-U0", "--no-color", "--no-renames",
             str(dest / src_rel), str(tmp / src_rel)],
            capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
        left = (dest / src_rel).as_posix().lstrip("/")

        def path_fn(p):
            p = p.lstrip("/")
            return p[len(left) + 1:] if p.startswith(left + "/") and p.endswith(".java") else None

        faults = faults_from_hunks(parse_left_hunks(diff, path_fn), lambda p: p[:-5].replace("/", "."))
        if not faults:
            raise _fail("Diff entre as versões com defeito e corrigida sem linhas em código de produção.")
        rel = [x for x in d4j.run("export", "-p", "tests.relevant", "-w", dest, check=False).split() if x]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
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


def _test_fqcn(path: str) -> str | None:
    m = re.search(r"(?:^|/)(?:src/test/java|src/test|src/tests|test/java|tests?|java)/(.+)\.java$",
                  path.replace("\\", "/"))
    return m.group(1).replace("/", ".") if m else None


def _changed_tests(wt: Path, fixed: str) -> list[str]:
    names = git(wt, "show", "--name-only", "--format=", fixed).split()
    out = {_test_fqcn(n) for n in names if n.endswith(".java") and TEST_PATH_RE.search(n)}
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

def local_bug(spec: str) -> Bug:
    """`spec`: pasta existente ou `URL@commit`."""
    p = Path(spec).expanduser()
    if p.is_dir():
        p = p.resolve()
        h = hashlib.sha1(str(p).encode("utf-8")).hexdigest()[:8]
        return Bug("local", p.name, f"dir-{h}", {"path": str(p)})
    if "@" in spec:
        url, commit = spec.rsplit("@", 1)
        if url and commit and "/" not in commit:
            name = url.rstrip("/").removesuffix(".git").replace("\\", "/").rsplit("/", 1)[-1]
            name = name.rsplit(":", 1)[-1] or "repo"
            return Bug("local", name, commit[:12], {"repo": url, "commit": commit})
    raise ValueError(f"Entrada local inválida (use uma pasta existente ou URL@commit): {spec}")


def _local_prepare(home: Home, bug: Bug, dest: Path, log: Path) -> Prepared:
    e = bug.extra
    if e.get("path"):
        src = Path(e["path"])
        if not src.is_dir():
            raise _fail(f"Pasta não encontrada: {src}")
        shutil.copytree(src, dest, ignore=_IGNORE_COPY)
        return Prepared(bug, dest, ".", None, [], {})
    url, commit = e["repo"], e["commit"]
    if Path(url).expanduser().exists():
        repo = Path(url).expanduser()
    else:
        safe = re.sub(r"[^\w.-]+", "_", url.rstrip("/").removesuffix(".git").split("://")[-1])
        repo = home.work / "clones" / safe
        if not repo.exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["git", "-c", "core.longpaths=true", "clone", "-q", url, str(repo)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            _log(log, r.stdout + r.stderr)
            if r.returncode != 0:
                shutil.rmtree(repo, ignore_errors=True)
                raise _fail(f"Não foi possível clonar {url}: {r.stderr.strip()[:300]}")
    return _worktree_prepare(bug, repo, dest, commit, None)


# ------------------------------------------------------------------- Público

def _marker_data(prep: Prepared) -> dict:
    return {"pristine": str(prep.pristine), "module": prep.module,
            "relevant_tests": prep.relevant_tests,
            "truth": [f.to_dict() for f in prep.truth], "info": prep.info}


def _remove_pristine(dest: Path, repo: str | None = None) -> None:
    if repo is None:
        try:
            repo = json.loads((dest / MARKER).read_text(encoding="utf-8")).get("info", {}).get("worktree_repo")
        except (OSError, ValueError):
            pass
    if repo and Path(repo).exists() and dest.exists():
        git(repo, "worktree", "remove", "--force", str(dest), check=False)
    shutil.rmtree(dest, ignore_errors=True)
    if repo and Path(repo).exists():
        git(repo, "worktree", "prune", check=False)


def prepare(home: Home, settings: Settings, bug: Bug, log: Path, refresh: bool = False) -> Prepared:
    dest = _pristine_dir(home, bug)
    marker = dest / MARKER
    if refresh and dest.exists():
        _remove_pristine(dest)
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
            prep = _local_prepare(home, bug, dest, log)
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
    """Cópia descartável do pristine (sem target/, .git nem o marcador)."""
    dest = home.work / "w" / secrets.token_hex(4)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(prep.pristine, dest, ignore=shutil.ignore_patterns("target", ".git", MARKER))
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
