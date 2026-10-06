"""Verificações de ambiente usadas por `doctor` (funções puras, fáceis de testar)."""
from __future__ import annotations

import re

JAGUAR_LONG_OPTIONS = ["classesDir", "heuristic", "logLevel", "output", "outputType",
                       "projectDir", "testsDir", "testsListFile"]


def java_major(text: str) -> int | None:
    """`openjdk version "21.0.10"` -> 21; `java version "1.8.0_202"` -> 8."""
    m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2)) if major == 1 and m.group(2) else major


def maven_version(text: str) -> tuple | None:
    m = re.search(r"Apache Maven (\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in m.groups()) if m else None


def missing_jaguar_options(help_text: str) -> list[str]:
    return [o for o in JAGUAR_LONG_OPTIONS if o not in help_text]


def advice(java_text: str, mvn_text: str, help_text: str, work_path: str, free_gb: float | None) -> list[tuple]:
    """Lista de (nível, mensagem); nível: ok | warn | fail."""
    out = []
    jm = java_major(java_text)
    if jm is None:
        out.append(("fail", "não consegui descobrir a versão do Java"))
    elif jm > 8:
        out.append(("warn", f"Java {jm}: Jaguar/JaCoCo antigos costumam exigir Java 8 "
                            "(defina [java] java_home); projetos antigos também"))
    else:
        out.append(("ok", f"Java {jm}"))
    mv = maven_version(mvn_text)
    if mv is None:
        out.append(("fail", "Maven não encontrado ou saída inesperada de `mvn -v`"))
    elif mv >= (3, 8, 1):
        out.append(("warn", f"Maven {'.'.join(map(str, mv))} bloqueia repositórios http://; "
                            "snapshots antigos podem falhar (use Maven 3.6/3.8.0 ou um mirror)"))
    else:
        out.append(("ok", f"Maven {'.'.join(map(str, mv))}"))
    if help_text.strip():
        miss = missing_jaguar_options(help_text)
        if len(miss) == len(JAGUAR_LONG_OPTIONS):
            out.append(("warn", "saída de `JaguarRunner --help` inesperada; confira as flags manualmente"))
        elif miss:
            out.append(("fail", f"o JaguarRunner não lista as opções: {', '.join(miss)}"))
        else:
            out.append(("ok", "JaguarRunner aceita todas as opções usadas pelo pipeline"))
    else:
        out.append(("warn", "não consegui obter `JaguarRunner --help`"))
    if len(work_path) > 50:
        out.append(("warn", f"workdir com caminho longo ({len(work_path)} caracteres); no Windows "
                            "prefira algo curto como C:\\w"))
    if free_gb is not None and free_gb < 20:
        out.append(("warn", f"só {free_gb:.0f} GB livres no disco do workdir"))
    return out
