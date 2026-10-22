"""Execução do JaguarRunner com timeout e laço de recuperação de crashes."""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .proc import run

TEST_RE = re.compile(r"Test (\S+?)\(([\w.$]+)\) : (Passed|Failed)")
OUT_RE = re.compile(r"Output xml created at: (.+\.xml)\s*$")
NOISE = r"to classFilesCache|Time to (analyze|receive|read|collect)|#lines = \d+"


@dataclass
class JaguarOptions:
    """Entradas explícitas do JaguarRunner (substitui o config legado)."""
    lib: Path
    java: str
    env: dict = field(default_factory=dict)
    heuristic: str = "Ochiai"
    output_type: str = "F"
    log_level: str = "DEBUG"
    timeout_s: int = 3600
    max_retries: int = 6
    jvm_args: list = field(default_factory=list)


def write_test_list(path: Path, classes: list[str]) -> None:
    # UTF-8 SEM BOM e sem CRLF: `Out-File -Encoding utf8` do PowerShell põe BOM e
    # isso pode corromper o primeiro nome de classe.
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(classes) + "\n")


LOGBACK_XML = """<configuration>
  <appender name="CONSOLE" class="ch.qos.logback.core.ConsoleAppender">
    <encoder><pattern>%d{{HH:mm:ss.SSS}} [%thread] %-5level %logger{{36}} - %msg%n</pattern></encoder>
  </appender>
  <appender name="JFLPFILE" class="ch.qos.logback.core.FileAppender">
    <file>{file}</file>
    <append>true</append>
    <immediateFlush>true</immediateFlush>
    <encoder><charset>UTF-8</charset><pattern>%msg%n</pattern></encoder>
  </appender>
  <logger name="JaguarLogger" level="DEBUG"><appender-ref ref="JFLPFILE"/></logger>
  <root level="DEBUG"><appender-ref ref="CONSOLE"/></root>
</configuration>
"""


def write_logback(config: Path, test_log: Path) -> None:
    """Configuração do logback que também grava o JaguarLogger em arquivo: um teste que troca ou fecha
    System.out derruba o console, mas não o arquivo."""
    from xml.sax.saxutils import escape
    config.write_text(LOGBACK_XML.format(file=escape(test_log.resolve().as_posix())),
                      encoding="utf-8", newline="\n")


def build_cmd(opts: JaguarOptions, ctx, tf: Path, out_base: str, heuristic: str,
              logback: Path | None = None) -> list:
    lib = Path(opts.lib)
    sep = os.pathsep
    cp = sep.join([str(lib / "*"), ctx.classes_dir, ctx.tests_dir, *ctx.extra_cp])
    return [
        opts.java,
        *opts.jvm_args,
        *([f"-Dlogback.configurationFile={logback.resolve().as_posix()}"] if logback else []),
        f"-javaagent:{lib / 'jacocoagent.jar'}=output=tcpserver,port=6300",
        "-cp", cp,
        "br.usp.each.saeg.jaguar.core.cli.JaguarRunner",
        "-p", ".", "-c", ctx.classes_dir, "-t", ctx.tests_dir,
        "-tf", str(tf), "-h", heuristic,
        "-ot", opts.output_type, "-o", out_base,
        "-l", opts.log_level,
    ]


def _file_events(path: Path, attempt: int) -> list[tuple[str, str, str]]:
    """(teste, classe, Passed|Failed) da tentativa `attempt` no arquivo do JaguarLogger."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    k = text.rfind(f"# sbfl attempt {attempt}\n")
    out = []
    for ln in text[k:].splitlines() if k >= 0 else []:
        m = TEST_RE.search(ln)
        if m:
            out.append((m.group(1), m.group(2), m.group(3)))
    return out


def _find_xml(project_dir: Path, tail: list[str], out_base: str) -> Path | None:
    for line in reversed(tail):
        m = OUT_RE.search(line.strip())
        if m:
            p = Path(m.group(1).strip())
            p = p if p.is_absolute() else project_dir / p
            if p.exists():
                return p
    p = project_dir / ".jaguar" / f"{out_base}.xml"
    return p if p.exists() else None


def run_jaguar(opts: JaguarOptions, ctx, out_dir: Path, heuristic: str | None = None) -> dict:
    heuristic = heuristic or opts.heuristic
    out_base = f"sbfl_{heuristic}"
    log = out_dir / "jaguar.log"
    test_log = out_dir / "jaguar-tests.log"       # JaguarLogger direto em arquivo (sobrevive a System.setOut)
    logback = out_dir / "logback-jflp.xml"
    write_logback(logback, test_log)
    tf = ctx.project_dir / ".sbfl_tests.txt"
    classes = list(ctx.test_classes)
    excluded: list[str] = []
    last_seen = None
    meta = {"attempts": 0, "excluded_classes": excluded, "analysis_exceptions": 0}
    stale = ctx.project_dir / ".jaguar" / f"{out_base}.xml"
    for attempt in range(opts.max_retries + 1):
        meta["attempts"] = attempt + 1
        if stale.exists():
            stale.unlink()
        remaining = [c for c in classes if c not in excluded]
        if not remaining:
            meta["status"] = "no_tests"
            return meta
        write_test_list(tf, remaining)
        with open(log, "a", encoding="utf-8") as lf:
            lf.write(f"# sbfl attempt {attempt + 1}\n")
        with open(test_log, "a", encoding="utf-8", newline="\n") as tl:
            tl.write(f"# sbfl attempt {attempt + 1}\n")
        r = run(build_cmd(opts, ctx, tf, out_base, heuristic, logback), cwd=ctx.project_dir,
                env=opts.env or None, timeout=opts.timeout_s, log_path=log,
                drop=NOISE,
                count={"analysis_exceptions": r"Exception during analysis"},
                collect={"test": r"(Test \S+?\([\w.$]+\) : (?:Passed|Failed))"})
        meta["analysis_exceptions"] += r["counts"]["analysis_exceptions"]
        meta["seconds"] = meta.get("seconds", 0) + r["seconds"]
        events = [TEST_RE.search(t) for t in r["collected"]["test"]]
        events = [(m.group(1), m.group(2), m.group(3)) for m in events if m]
        from_file = _file_events(test_log, attempt + 1)
        if len(from_file) > len(events):     # o stdout pode ter sido cortado por um teste; o arquivo não
            events = from_file
        failed = [f"{c}#{n}" for n, c, s in events if s == "Failed"]
        meta["failed_tests"] = failed[:200]
        xml = _find_xml(ctx.project_dir, r["tail"], out_base)
        if xml:
            # classes que não são testes de verdade (abstract/sem @Test) viram
            # `initializationError` "falho" e poluem o espectro: remove e repete.
            bad = sorted({c for n, c, s in events if n == "initializationError" and c in remaining})
            if bad and attempt < opts.max_retries:
                excluded += bad
                continue
            shutil.copy(xml, out_dir / f"jaguar_{heuristic}.xml")
            meta["status"] = "ok"
            return meta
        if r["timed_out"]:
            meta["status"] = "jaguar_timeout"
            return meta
        # crash: descobre o culpado pelo último teste que terminou
        if not events:
            culprit, last = remaining[0], None
        else:
            last = events[-1][1]
            base = last if last in remaining else last.split("$", 1)[0]
            if base not in remaining:
                meta["status"] = "jaguar_crash"
                meta["tail"] = "".join(r["tail"][-30:])
                return meta
            i = remaining.index(base)
            nxt = remaining[i + 1] if i + 1 < len(remaining) else None
            culprit = base if (base == last_seen or nxt is None) else nxt
            last = base
        last_seen = last
        excluded.append(culprit)
    meta["status"] = "jaguar_crash"
    meta["tail"] = "".join(r["tail"][-30:])
    return meta
