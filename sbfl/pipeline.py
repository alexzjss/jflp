from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import time
from pathlib import Path

from .errors import StageError
from .diffgt import FaultLine, select_lines
from .jaguar import run_jaguar
from .proc import make_env, java_exe
from .report import evaluate, parse_xml


def out_dir_for(cfg, bug) -> Path:
    d = cfg.results / bug.benchmark / bug.project / bug.bug_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def java_version(cfg, java_home=None) -> str:
    try:
        r = subprocess.run([java_exe(cfg, java_home), "-version"], capture_output=True,
                           text=True, env=make_env(cfg, java_home))
        return (r.stderr or r.stdout).splitlines()[0]
    except Exception as e:
        return f"erro: {e}"


def analyze_dir(cfg, d: Path) -> str:
    """Recalcula métricas a partir do XML + truth.json salvos (sem rodar o Jaguar de novo)."""
    xmls = sorted(d.glob("jaguar_*.xml"))
    truth = json.loads((d / "truth.json").read_text(encoding="utf-8"))
    faults = select_lines([FaultLine(**t) for t in truth])
    status, info, rows = evaluate(parse_xml(xmls[0]), faults, cfg.get("analysis", "heuristics"))
    _save(d / "metrics.json", rows)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    meta.update(info)
    meta["status"] = status
    _save(d / "meta.json", meta)
    return status


def process(cfg, adapter, bug, force=False, keep_workdir=False) -> dict:
    d = out_dir_for(cfg, bug)
    mp = d / "meta.json"
    if mp.exists() and not force:
        return json.loads(mp.read_text(encoding="utf-8"))
    for old in d.glob("*"):
        old.unlink()
    meta = {"bug": bug.key, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "error"}
    log = d / "jaguar.log"
    t0, ctx = time.time(), None
    try:
        ctx = adapter.prepare(bug, log)
        meta["prepare_seconds"] = round(time.time() - t0, 1)
        meta["java"] = java_version(cfg, ctx.java_home)
        meta.update(n_test_classes=len(ctx.test_classes), **ctx.info)
        _save(d / "truth.json", [f.to_dict() for f in ctx.truth])
        jm = run_jaguar(cfg, ctx, d)
        meta.update({k: v for k, v in jm.items() if k != "status"})
        meta["status"] = jm["status"]
        if jm["status"] == "ok":
            _save(mp, meta)
            meta["status"] = analyze_dir(cfg, d)
            meta = json.loads(mp.read_text(encoding="utf-8"))
    except StageError as e:
        meta.update(status=e.status, detail=e.detail[-1500:])
    except Exception as e:  # nunca derruba o lote inteiro
        meta.update(status="error", detail=repr(e)[-1500:])
    finally:
        if ctx and not keep_workdir:
            ctx.cleanup()
    meta["total_seconds"] = round(time.time() - t0, 1)
    _save(mp, meta)
    if log.exists():
        with open(log, "rb") as src, gzip.open(str(log) + ".gz", "wb") as dst:
            shutil.copyfileobj(src, dst)
        log.unlink()
    return meta
