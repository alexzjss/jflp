"""Progresso estruturado de uma execução, extraído do log linha a linha (Maven, Jaguar 1/2 e marcadores do JFLP).

Puro e barato: expressões regulares pré-compiladas e checagens `startswith` antes de qualquer regex.
Não é thread-safe por si; quem o compartilha (o `Worker`) o protege com a própria trava.
"""
from __future__ import annotations

import re
import time

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PREFIX = re.compile(r"^\[(INFO|ERROR|WARNING|WARN|DEBUG)\]\s?")
_GOAL = re.compile(r"^--- (\S+?) \(([^)]*)\)(?: @ (\S+))? ---")
_BUILDING = re.compile(r"^Building (.+?) (\S+)(?: \[(\d+)/(\d+)\])?\s*$")
_COMPILING = re.compile(r"^Compiling (\d+) source files?")
_SIZE = re.compile(r"\(([\d.,]+) (B|kB|KB|MB|GB)\b")
_TESTS_RUN = re.compile(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)")
_IN_CLASS = re.compile(r"(?: -- in | - in )(\S+)")
_J1_TEST = re.compile(r"Test (\S+) : (Passed|Failed)\s*$")
_J1_NUM = re.compile(r"Test # (\d+)")
_ATTEMPT = re.compile(r"^# jflp tentativa (\d+)(?: (ignorada): (.*)| ?: (\S+))?\s*$")
_PHASE = re.compile(r"^# jflp fase: (\S+)")
_UNITS = {"B": 1, "kB": 1000, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3}
_HELP = ("To see the full stack trace", "e-run Maven", "[Help ", "For more information",
         "After correcting the problems", "mvn <args> -rf", "Re-run with -X")

# linhas [ERROR] do Surefire que relatam testes falhando: é o bug (dado), não erro de infraestrutura
_TEST_REPORT = ("Tests run:", "There are test failures", "Failures:", "Errors:", "Failed tests:",
                "Tests in error:", "Please refer to", "<<< FAILURE", "<<< ERROR")

STEPS = ("preparar", "compilar", "testes", "analise", "gravar")   # ordem da linha do tempo
_STEP_ORDER = {s: i for i, s in enumerate(STEPS)}
SUCCESS = ("ok", "ok_testes_falhando")

PHASE_LABEL = {"reactor": "preparando o reactor", "pom": "gerando o POM", "maven": "executando o Maven",
               "jaguar2": "executando o Jaguar 2", "jaguar1": "executando o Jaguar 1"}
_GOAL_LABEL = {
    ("resources", "resources"): ("copiando recursos", None),
    ("resources", "testResources"): ("copiando recursos de teste", None),
    ("compiler", "compile"): ("compilando", "compilar"),
    ("compiler", "testCompile"): ("compilando testes", "compilar"),
    ("jacoco", "prepare-agent"): ("preparando cobertura", None),
    ("jacoco", "report"): ("relatório de cobertura", "analise"),
    ("surefire", "test"): ("rodando testes", "testes"),
}


def _plugin_name(artifact: str) -> str:
    """`maven-compiler-plugin` e `compiler` → `compiler`; `jacoco-maven-plugin` → `jacoco`."""
    if artifact.startswith("maven-"):
        artifact = artifact[6:]
    for suf in ("-maven-plugin", "-plugin"):
        if artifact.endswith(suf):
            return artifact[: -len(suf)]
    return artifact


def parse_goal(text: str) -> tuple[str, str] | None:
    """`plugin:versão:goal (id) @ art` (formas longa, curta ou com groupId) → (plugin, goal)."""
    m = _GOAL.match(text)
    if not m:
        return None
    parts = m.group(1).split(":")
    if len(parts) < 3:
        return None
    return _plugin_name(parts[-3]), parts[-1]


def _bytes(m: re.Match) -> int:
    try:
        return int(float(m.group(1).replace(",", ".")) * _UNITS[m.group(2)])
    except ValueError:
        return 0


class Progress:
    """Estado do progresso de um job; `feed(linha)` atualiza, `to_dict()` serializa (JSON puro)."""

    def __init__(self, kind: str = "", clock=time.time):
        self.kind = kind
        self._clock = clock
        self.inicio = clock()
        self._fim: float | None = None
        self.etapa = "preparando"
        self.fase = "preparando"
        self.passo = "preparar"
        self.falhou = False
        self.n_tentativa = 0
        self.tentativas: list[dict] = []
        self.modulo: dict | None = None
        self.downloads = {"n": 0, "bytes": 0, "baixando": False}
        self.compilacao: int | None = None
        self.ultimo_erro: str | None = None
        self.build: str | None = None          # SUCCESS | FAILURE da última construção do Maven
        self._reset_attempt()

    def _reset_attempt(self) -> None:
        self.testes = {"rodados": 0, "falhas": 0, "erros": 0, "ignorados": 0, "classe": None,
                       "classes_falhas": [], "passaram": 0, "falharam": 0}
        self.compilacao = None
        self.modulo = None
        self.ultimo_erro = None
        self.passo = "preparar"
        self.fase = "preparando"
        self.falhou = False

    # ---------------------------------------------------------------- entrada
    def feed(self, line) -> None:
        s = str(line)
        if "\r" in s:
            s = s.rstrip("\r\n").rsplit("\r", 1)[-1]
        s = s.strip()
        if not s:
            return
        if "\x1b" in s:
            s = _ANSI.sub("", s).strip()
        if s.startswith("#"):
            if s.startswith("# jflp"):
                self._marker(s)
            return
        level = None
        if s.startswith("["):
            m = _PREFIX.match(s)
            if m:
                level, s = m.group(1), s[m.end():]
        if level == "ERROR":
            self._error(s)
        if s.startswith("Downloading from"):
            self.downloads["baixando"] = True
            return
        if s.startswith("Downloaded from"):
            self.downloads["n"] += 1
            self.downloads["baixando"] = False
            m = _SIZE.search(s)
            if m:
                self.downloads["bytes"] += _bytes(m)
            return
        self.downloads["baixando"] = False
        if s.startswith("---"):
            self._goal(s)
        elif s.startswith("Running "):
            self._running(s[8:].strip())
        elif "Tests run:" in s:
            self._tests_run(s)
        elif s.startswith("Building "):
            self._building(s)
        elif s.startswith("Compiling "):
            m = _COMPILING.match(s)
            if m:
                self.compilacao = int(m.group(1))
        elif s.startswith("BUILD "):
            self.build = s[6:].strip().split(" ", 1)[0]
        elif " JaguarLogger - " in s:
            self._jaguar1(s.split(" JaguarLogger - ", 1)[1])

    # ---------------------------------------------------------------- partes
    def _advance(self, passo: str) -> None:
        if _STEP_ORDER[passo] > _STEP_ORDER[self.passo]:
            self.passo = passo

    def _marker(self, s: str) -> None:
        m = _ATTEMPT.match(s)
        if m:
            n = int(m.group(1))
            if m.group(2):                                  # ignorada: ferramenta ausente
                self._attempt(n)["status"] = "ignorada"
                self._attempt(n)["motivo"] = (m.group(3) or "").strip()
                self.falhou = True
            elif m.group(4):                                # fim da tentativa, com status
                a = self._attempt(n)
                a["status"] = m.group(4)
                self.falhou = m.group(4) not in SUCCESS
                if not self.falhou:
                    self.etapa, self.passo, self.fase = "gravando", "gravar", "gravando o resultado"
            else:                                           # início da tentativa
                self.n_tentativa = n
                self.etapa = "tentativa"
                self._reset_attempt()
                self._attempt(n)
            return
        m = _PHASE.match(s)
        if m:
            fase = m.group(1)
            self.fase = PHASE_LABEL.get(fase, fase)
            if self.tentativas:
                self.tentativas[-1]["fase"] = fase
            if fase in ("reactor", "pom"):
                self._advance("preparar")

    def _attempt(self, n: int) -> dict:
        for a in self.tentativas:
            if a["n"] == n:
                return a
        a = {"n": n, "fase": None, "status": None}
        self.tentativas.append(a)
        return a

    def _goal(self, s: str) -> None:
        g = parse_goal(s)
        if g is None:
            return
        plugin, goal = g
        label, passo = _GOAL_LABEL.get(g, (None, None))
        if label is None:
            label = f"{plugin}:{goal}"
            if "jaguar" in plugin.lower():
                passo = "analise"
        self.fase = label
        if passo:
            self._advance(passo)

    def _running(self, cls: str) -> None:
        self.testes["classe"] = cls
        self._advance("testes")

    def _tests_run(self, s: str) -> None:
        if "Time elapsed" not in s:      # sem tempo é a linha de totais (`Results :`): contaria em dobro
            return
        m = _TESTS_RUN.search(s)
        if not m:
            return
        run, fail, err, skip = (int(x) for x in m.groups())
        t = self.testes
        t["rodados"] += run
        t["falhas"] += fail
        t["erros"] += err
        t["ignorados"] += skip
        if fail or err:
            mc = _IN_CLASS.search(s)
            cls = mc.group(1) if mc else t["classe"]
            if cls and cls not in t["classes_falhas"]:
                t["classes_falhas"].append(cls)
        self._advance("testes")

    def _building(self, s: str) -> None:
        if s.startswith(("Building jar:", "Building war:", "Building ear:")):
            return
        m = _BUILDING.match(s)
        if not m:
            return
        nome, _versao, i, n = m.groups()
        self.modulo = {"nome": nome, "i": int(i) if i else None, "n": int(n) if n else None}

    def _jaguar1(self, s: str) -> None:
        if s.startswith("Test "):
            m = _J1_TEST.search(s)
            if m:
                t = self.testes
                t["rodados"] += 1
                if m.group(2) == "Failed":
                    t["falharam"] += 1
                    t["falhas"] += 1
                    cls = m.group(1).rsplit("(", 1)[-1].rstrip(")")
                    if cls and cls not in t["classes_falhas"]:
                        t["classes_falhas"].append(cls)
                else:
                    t["passaram"] += 1
                t["classe"] = m.group(1).rsplit("(", 1)[-1].rstrip(")") or t["classe"]
                self.fase = "rodando testes"
                self._advance("testes")
        elif s.startswith("Rank calculation"):
            self.fase = "calculando o ranking"
            self._advance("analise")
        elif s.startswith("Jaguar has finished"):
            self.fase = "Jaguar concluído"
            self._advance("analise")

    def _error(self, s: str) -> None:
        if not s or any(h in s for h in _HELP) or any(h in s for h in _TEST_REPORT):
            return
        if s[:1].isspace():     # linha de detalhe recuada (ex.: "  FooTest.x:12 ..." do resumo do Surefire)
            return
        self.ultimo_erro = s[:300]

    # ---------------------------------------------------------------- saída
    def finish(self, status: str | None = None) -> None:
        """Marca o fim do job (congela o cronômetro)."""
        self.etapa = "fim"
        self._fim = self._clock()
        if status:
            self.falhou = status not in SUCCESS

    @property
    def steps(self) -> list[str]:
        """Etapas da linha do tempo: a análise só existe em Jaguar 1/2."""
        return [s for s in STEPS if s != "analise" or self.kind in ("jaguar1", "jaguar2")]

    def to_dict(self) -> dict:
        now = self._fim if self._fim is not None else self._clock()
        t = dict(self.testes)
        t["classes_falhas"] = list(t["classes_falhas"][-5:])
        return {
            "etapa": self.etapa, "tentativa": self.n_tentativa, "fase": self.fase, "passo": self.passo,
            "passos": self.steps, "falhou": self.falhou, "kind": self.kind,
            "modulo": dict(self.modulo) if self.modulo else None,
            "downloads": dict(self.downloads), "testes": t, "compilacao": self.compilacao,
            "tentativas": [dict(a) for a in self.tentativas], "ultimo_erro": self.ultimo_erro,
            "build": self.build, "inicio": self.inicio, "decorrido": round(max(now - self.inicio, 0), 1),
        }


def fmt_tempo(seconds: float) -> str:
    """`mm:ss` (ou `h:mm:ss`)."""
    s = int(max(seconds or 0, 0))
    h, resto = divmod(s, 3600)
    m, s = divmod(resto, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _plural(n: int, um: str, varios: str) -> str:
    return f"{n} {um if n == 1 else varios}"


def linha(d: dict | None) -> str:
    """Resumo de uma linha do progresso: `Tentativa 2 · rodando testes · 128 testes, 1 falha · 01:42`."""
    if not d:
        return "Em andamento"
    etapa = d.get("etapa")
    if etapa == "preparando":
        partes = ["Preparando"]
    elif etapa == "gravando":
        partes = ["Gravando o resultado"]
    elif etapa == "fim":
        partes = ["Concluindo"]
    else:
        partes = [f"Tentativa {d.get('tentativa') or 1}", d.get("fase") or "executando"]
    t = d.get("testes") or {}
    if t.get("rodados"):
        txt = _plural(t["rodados"], "teste", "testes")
        ruins = (t.get("falhas") or 0) + (t.get("erros") or 0)
        if ruins:
            txt += ", " + _plural(ruins, "falha", "falhas")
        partes.append(txt)
    partes.append(fmt_tempo(d.get("decorrido") or 0))
    return " · ".join(partes)
