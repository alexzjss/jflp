"""Visão por bug de um lote: Maven, Jaguar 1 e Jaguar 2 lado a lado, veredito contra o catálogo do Bugs.jar,
concordância entre os dois Jaguar e CSV. Módulo puro (sem FastAPI): devolve dicts simples para os templates.

Funções públicas:
 - `lote_view(home, bid)`      tabela do lote (uma linha por bug, barata: só lê o que as execuções gravaram);
 - `lote_sig(home, bid)`       assinatura de estado dos jobs (re-renderizar só quando muda);
 - `bug_detail(home, bid, bug_key)` detalhe completo de um bug (Maven, Jaguar, concordância, catálogo);
 - `lote_resumos(home, bid)`   resumo do JIRA de cada bug (lê o git; chamar à parte);
 - `lote_csv(home, bid)`       CSV do lote (uma linha por bug);
 - `fonte_bugsjar(repo, ramo, cls)` e `metodo_da_linha(linhas, n)` código-fonte do ramo e método de uma linha.

Veredito (por Jaguar): posição esperada da melhor linha do defeito <= 10 "achou"; <= 50 ou alguma classe do
patch entre as classes do top 10 "parcial"; senão "nao_achou"; sem gabarito, sem ranking ou com erro "sem_dado".
A suspeição é sempre a gravada pelo Jaguar: nada aqui recalcula fórmulas."""
from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path

from . import compare
from . import queue as jobs_queue
from . import status as S
from .diffgt import TEST_PATH_RE, FaultLine, select_lines
from .errors import StageError
from .gitutil import git
from .outputs import LineScore, outer, parse_surefire_failures, ranking_rows
from .prepare import (BUGSJAR_DIR, _test_fqcn, bugsjar_label, bugsjar_patch_text,
                      bugsjar_summary)
from .store import load_run, run_dir

TOP_N = 10
MAIS_N = 50
KINDS = ("maven", "jaguar1", "jaguar2")
KIND_LABEL = {"maven": "Maven", "jaguar1": "Jaguar 1", "jaguar2": "Jaguar 2"}
KIND_CURTO = {"jaguar1": "J1", "jaguar2": "J2"}
JIRA_URL = "https://issues.apache.org/jira/browse/{id}"

# código -> (símbolo, rótulo); a ordem é da pior para a melhor
VEREDITOS = {"sem_dado": ("—", "sem dado"), "nao_achou": ("✗", "não achou"),
             "parcial": ("~", "parcial"), "achou": ("✓", "achou")}
_ORDEM = {c: i for i, c in enumerate(VEREDITOS)}


def veredito(code: str) -> dict:
    """{"code", "simbolo", "label"} de um código de veredito."""
    simbolo, label = VEREDITOS[code]
    return {"code": code, "simbolo": simbolo, "label": label}


def melhor_veredito(codes: list[str]) -> str:
    return max(codes, key=_ORDEM.__getitem__) if codes else "sem_dado"


# ------------------------------------------------------------------ catálogo e fonte no git

_listagens: dict[tuple[str, str], list[str]] = {}
_catalogos: dict[tuple[str, str], dict | None] = {}
_fontes: dict[tuple[str, str, str], tuple[str, list[str]] | None] = {}
_resumos: dict[tuple[str, str], dict] = {}
_pos_cache: dict[tuple, dict] = {}


def limpar_cache() -> None:
    """Esquece listagens, catálogos e fontes lidos do git (os ramos podem ter mudado)."""
    for c in (_listagens, _catalogos, _fontes, _resumos, _pos_cache):
        c.clear()


def _listagem(repo, ramo: str) -> list[str]:
    key = (str(repo), ramo)
    if key not in _listagens:
        try:
            out = git(repo, "ls-tree", "-r", "--name-only", ramo, check=False)
        except OSError:
            out = ""
        _listagens[key] = [x.strip() for x in out.splitlines() if x.strip()]
    return _listagens[key]


def _show(repo, ramo: str, caminho: str) -> str | None:
    try:
        r = git(repo, "show", f"{ramo}:{caminho}", check=False)
    except (OSError, StageError):
        return None
    return r or None


def fonte_bugsjar(repo, ramo: str, cls: str) -> tuple[str, list[str]] | None:
    """(caminho, linhas) do arquivo `.java` de produção da classe `cls` no ramo (`git show`, sem checkout).

    Classes internas (`a.B$I`) resolvem para o arquivo da classe externa. None se não achar."""
    cls = outer(cls)
    key = (str(repo), ramo, cls)
    if key in _fontes:
        return _fontes[key]
    sufixo = cls.replace(".", "/") + ".java"
    cand = [p for p in _listagem(repo, ramo)
            if (p == sufixo or p.endswith("/" + sufixo)) and not TEST_PATH_RE.search(p)]
    cand.sort(key=lambda p: (0 if "src/main/java/" in p else 1, len(p)))
    got = None
    if cand:
        text = _show(repo, ramo, cand[0])
        if text is not None:
            got = (cand[0], text.splitlines())
    _fontes[key] = got
    return got


def catalogo_bugsjar(repo, ramo: str) -> dict | None:
    """Patch do catálogo lido do git: `bugsjar_patch_text` + `diff` + `results`. None sem `developer-patch.diff`."""
    key = (str(repo), ramo)
    if key in _catalogos:
        return _catalogos[key]
    diff = _show(repo, ramo, f"{BUGSJAR_DIR}/developer-patch.diff")
    got = None
    if diff is not None:
        results = _show(repo, ramo, f"{BUGSJAR_DIR}/test-results.txt")
        files = _listagem(repo, ramo)
        poms = {p[:-len("pom.xml")].rstrip("/") or "." for p in files if p == "pom.xml" or p.endswith("/pom.xml")}

        def module_of(path: str) -> str:
            d = path.rsplit("/", 1)[0] if "/" in path else "."
            while d not in poms and d != ".":
                d = d.rsplit("/", 1)[0] if "/" in d else "."
            return d

        def find_tests(simples: set[str]) -> set[str]:
            return {c for p in files if p.endswith(".java") and TEST_PATH_RE.search(p)
                    and p.rsplit("/", 1)[-1][:-5] in simples for c in [_test_fqcn(p)] if c}

        got = bugsjar_patch_text(diff, results, module_of=module_of, find_tests=find_tests)
        got.update(diff=diff, results=results)
    _catalogos[key] = got
    return got


def resumo_bugsjar(repo, ramo: str) -> dict:
    """{"resumo", "descricao"} do `bug-report.yml` do ramo (vazios se não houver)."""
    key = (str(repo), ramo)
    if key not in _resumos:
        text = _show(repo, ramo, f"{BUGSJAR_DIR}/bug-report.yml") or ""
        m = re.search(r"^Description:[ \t]*[|>][-+]?[ \t]*\n((?:[ \t]+.*\n?|\n)*)", text, re.M)
        desc = "\n".join(x[2:] if x.startswith("  ") else x for x in m.group(1).splitlines()).strip() if m else ""
        _resumos[key] = {"resumo": bugsjar_summary(repo, ramo) if text else "", "descricao": desc[:2000]}
    return _resumos[key]


def jira_url(bug_id: str) -> str:
    """Link do JIRA do Apache montado do ID (o `bug-report.yml` do Bugs.jar não traz URL)."""
    return JIRA_URL.format(id=bug_id)


# ------------------------------------------------------------------ método que contém uma linha

_PALAVRAS = {"if", "for", "while", "switch", "catch", "synchronized", "try", "else", "do", "return", "new",
             "throw", "assert", "super", "this"}
_CLASSE = re.compile(r"(?:^|[\s)>])(?:class|interface|enum|record|@interface)\s+([A-Za-z_$][\w$]*)")
_NEW = re.compile(r"\bnew\s+[\w.$<>?,\s]+$")
_THROWS = re.compile(r"\)\s*throws\s+[\w.$<>,\s]+$")


def _ultimo_grupo(h: str) -> int | None:
    """Índice do `(` que casa com o `)` final de `h`."""
    prof = 0
    for i in range(len(h) - 1, -1, -1):
        if h[i] == ")":
            prof += 1
        elif h[i] == "(":
            prof -= 1
            if prof == 0:
                return i
    return None


def _classificar(h: str, pai: str | None) -> tuple[str, str]:
    """(tipo, nome) do bloco aberto por `{` dado o cabeçalho `h` e o tipo do bloco pai."""
    h = " ".join(h.split())
    if "->" in h and h.endswith("->"):
        return "outro", ""
    m = _CLASSE.search(h)
    if m and not h.endswith(")"):
        return "classe", m.group(1)
    h2 = _THROWS.sub(")", h)
    if h2.endswith(")"):
        i = _ultimo_grupo(h2)
        if i is not None:
            prefixo = h2[:i].rstrip()
            if _NEW.search(prefixo):
                return "classe", "<anônima>"
            if pai in (None, "classe"):
                m2 = re.search(r"([A-Za-z_$][\w$]*)$", prefixo)
                if m2 and m2.group(1) not in _PALAVRAS:
                    return "metodo", m2.group(1)
    if pai in (None, "classe"):
        if h == "static":
            return "metodo", "<static>"
        if h == "":
            return "metodo", "<init>"
    return "outro", ""


def _blocos_metodos(linhas: list[str]) -> list[tuple[int, int, str]]:
    """[(linha_inicial, linha_final, nome)] de cada método/construtor/inicializador do fonte Java.

    Varredura caractere a caractere ignorando comentários, strings, textos entre aspas triplas e caracteres;
    um `{` é classificado pelo cabeçalho acumulado desde o último `;`, `{` ou `}`."""
    texto = "\n".join(linhas)
    n, i, ln = len(texto), 0, 1
    pilha: list[tuple[str, str, int]] = []
    spans: list[tuple[int, int, str]] = []
    h: list[str] = []
    hini: int | None = None

    def zera():
        nonlocal hini
        h.clear()
        hini = None

    while i < n:
        c = texto[i]
        d = texto[i:i + 2]
        if d == "//":
            j = texto.find("\n", i)
            i = n if j < 0 else j
            continue
        if d == "/*":
            j = texto.find("*/", i + 2)
            j = n if j < 0 else j + 2
            ln += texto.count("\n", i, j)
            i = j
            h.append(" ")
            continue
        if c == '"' or c == "'":
            aspas = '"""' if texto.startswith('"""', i) else c
            j = i + len(aspas)
            while j < n and not texto.startswith(aspas, j):
                j += 2 if texto[j] == "\\" else 1
                if aspas == c and j <= n and texto[j - 1:j] == "\n":
                    break      # literal quebrado: não engole o arquivo inteiro
            j = min(j + len(aspas), n)
            ln += texto.count("\n", i, j)
            i = j
            if hini is None:
                hini = ln
            h.append('""')
            continue
        if c == "{":
            tipo, nome = _classificar("".join(h), pilha[-1][0] if pilha else None)
            pilha.append((tipo, nome, hini if hini is not None else ln))
            zera()
        elif c == "}":
            if pilha:
                tipo, nome, ini = pilha.pop()
                if tipo == "metodo":
                    spans.append((ini, ln, nome))
            zera()
        elif c == ";":
            zera()
        else:
            if c == "\n":
                ln += 1
                h.append(" ")
            else:
                if hini is None and not c.isspace():
                    hini = ln
                h.append(c)
        i += 1
    return spans


def metodo_da_linha(linhas: list[str], n: int) -> str | None:
    """Nome do método (ou construtor) que contém a linha `n` (1-based) do fonte; o mais interno vence
    (método de classe anônima ou interna). None fora de qualquer método."""
    dentro = [s for s in _blocos_metodos(linhas) if s[0] <= n <= s[1]]
    return min(dentro, key=lambda s: s[1] - s[0])[2] if dentro else None


# ------------------------------------------------------------------ veredito e posições

def _chave(cls: str, line: int) -> tuple[str, int]:
    return (outer(cls), line)


def _pos_texto(p: dict) -> str:
    esperada = f"{p['expected']:g}"
    return f"pos. {esperada}"


def _faixa_texto(p: dict) -> str:
    if p["tied"] <= 1:
        return f"posição {p['best']} de {p['total']}"
    return f"posições {p['best']}–{p['worst']} de {p['total']} ({p['tied']} empatadas)"


def posicoes_defeito(scores: list[LineScore], faults: list[FaultLine]) -> dict:
    """Posições das linhas do defeito (classes internas valem pela mais suspeita) e a melhor delas.

    {"total", "linhas": [{cls, line, best, worst, tied, expected}], "melhor": linha|None, "nao_coberto"}."""
    achadas: dict[tuple[str, int], dict] = {}
    for r in compare.fault_positions(scores, faults)["lines"]:
        k = _chave(r["cls"], r["line"])
        if k not in achadas or r["rank_best"] < achadas[k]["best"]:
            achadas[k] = {"cls": k[0], "line": k[1], "best": r["rank_best"], "worst": r["rank_worst"],
                          "tied": r["tied"], "expected": r["expected"], "total": len(scores)}
    linhas = sorted(achadas.values(), key=lambda r: (r["expected"], r["cls"], r["line"]))
    melhor = dict(linhas[0]) if linhas else None
    if melhor:
        melhor["texto"] = _pos_texto(melhor)
        melhor["faixa"] = _faixa_texto(melhor)
    return {"total": len(scores), "linhas": linhas, "melhor": melhor,
            "nao_coberto": len({_chave(f.cls, f.line) for f in faults}) - len(linhas)}


def veredito_jaguar(scores: list[LineScore] | None, faults: list[FaultLine], top_n: int = TOP_N) -> tuple[str, dict | None]:
    """(código do veredito, posições) de um ranking do Jaguar contra as linhas do defeito (spec §3.3)."""
    faults = select_lines(list(faults))
    if scores is None or not faults:
        return "sem_dado", None
    pos = posicoes_defeito(scores, faults)
    melhor = pos["melhor"]
    if melhor and melhor["expected"] <= top_n:
        return "achou", pos
    if melhor and melhor["expected"] <= MAIS_N:
        return "parcial", pos
    classes_topo = {outer(c) for c, _l, _s, _r in ranking_rows(scores)[:top_n]}
    if classes_topo & {outer(f.cls) for f in faults}:
        return "parcial", pos
    return "nao_achou", pos


def veredito_maven(falhas: list[str], esperadas: list[str] | None) -> str:
    """Só Maven: classes de teste com falha contra as do catálogo (iguais ✓, interseção ~, nenhuma ✗)."""
    if not esperadas:
        return "sem_dado"
    obtidas, esp = {outer(t.split("#")[0]) for t in falhas}, {outer(c) for c in esperadas}
    if obtidas == esp:
        return "achou"
    return "parcial" if obtidas & esp else "nao_achou"


def esperado_maven(falhas: list[str], esperadas: list[str] | None) -> dict:
    """"Falhou o esperado?" no nível de classe: {"estado": iguais|diferentes|sem_catalogo, a_mais, faltando, texto}."""
    obtidas = sorted({outer(t.split("#")[0]) for t in falhas})
    if not esperadas:
        return {"estado": "sem_catalogo", "a_mais": [], "faltando": [], "obtidas": obtidas, "esperadas": [],
                "texto": "sem catálogo"}
    esp = sorted({outer(c) for c in esperadas})
    a_mais, faltando = sorted(set(obtidas) - set(esp)), sorted(set(esp) - set(obtidas))
    partes = []
    if a_mais:
        partes.append("a mais: " + ", ".join(c.rsplit(".", 1)[-1] for c in a_mais))
    if faltando:
        partes.append("faltando: " + ", ".join(c.rsplit(".", 1)[-1] for c in faltando))
    return {"estado": "diferentes" if partes else "iguais", "a_mais": a_mais, "faltando": faltando,
            "obtidas": obtidas, "esperadas": esp, "texto": "; ".join(partes) or "iguais"}


# ------------------------------------------------------------------ lote: agrupamento por bug

def _partes(bug_key: str) -> tuple[str, str, str]:
    p = (bug_key.split("/", 2) + ["", "", ""])[:3]
    return p[0], p[1], p[2]


def _agrupar(jobs: list[dict]) -> list[dict]:
    """Jobs agrupados por `bug_key`, na ordem em que aparecem: [{key, jobs: {kind: job}, extra}]."""
    grupos: dict[str, dict] = {}
    for j in jobs:
        g = grupos.setdefault(j["bug_key"], {"key": j["bug_key"], "jobs": {}, "extra": j.get("extra") or {}})
        g["jobs"][j["kind"]] = j
    return list(grupos.values())


def lote_sig(home, bid: int, jobs: list[dict] | None = None) -> str:
    """Assinatura (id:estado de cada job): muda só quando um job muda de estado."""
    jobs = jobs if jobs is not None else jobs_queue.batch_jobs(home, bid)
    return hashlib.blake2s("|".join(f"{j['id']}:{j['state']}" for j in jobs).encode(), digest_size=4).hexdigest()


def _repo_ramo(g: dict) -> tuple[str | None, str | None]:
    """(repositório, ramo) de um bug do Bugs.jar; (None, None) nos outros."""
    ex = g["extra"]
    if g["key"].startswith("bugsjar/") and ex.get("repo"):
        return ex["repo"], ex.get("buggy") or _partes(g["key"])[2]
    return None, None


def _ident(g: dict) -> dict:
    bench, projeto, ramo = _partes(g["key"])
    return {"key": g["key"], "benchmark": bench, "project": projeto, "branch": ramo,
            "id": bugsjar_label(ramo) if bench == "bugsjar" else ramo}


# ------------------------------------------------------------------ análise de um bug (execuções + gabarito)

class _Bug:
    """Carrega sob demanda as execuções, o gabarito e as posições de um bug (cada coisa lida uma vez)."""

    def __init__(self, home, g: dict):
        self.home, self.g = home, g
        self.repo, self.ramo = _repo_ramo(g)
        self._runs: dict[str, dict | None] = {}
        self._scores: dict[str, list[LineScore] | None] = {}
        self._truth: list[FaultLine] | None = None
        self._cat = False

    def job(self, kind: str) -> dict | None:
        return self.g["jobs"].get(kind)

    def run(self, kind: str) -> dict | None:
        if kind not in self._runs:
            j = self.job(kind)
            run = None
            if j and j["state"] == "done" and j.get("run_rel"):
                try:
                    run = load_run(self.home, j["run_rel"])
                except (OSError, ValueError):
                    run = None
            self._runs[kind] = run
        return self._runs[kind]

    def status(self, kind: str) -> str | None:
        run = self.run(kind)
        return S.effective(run.get("status") or "", run.get("tests")) if run else None

    def scores(self, kind: str) -> list[LineScore] | None:
        if kind not in self._scores:
            sc = None
            if self.run(kind) is not None and S.is_success(self.status(kind) or ""):
                try:
                    sc = compare.run_scores(self.home, self.job(kind)["run_rel"])
                except (OSError, ValueError):
                    sc = None
            self._scores[kind] = sc
        return self._scores[kind]

    def catalogo(self) -> dict | None:
        if not self._cat:
            self._cat = True
            self._catalogo = catalogo_bugsjar(self.repo, self.ramo) if self.repo else None
        return self._catalogo

    def truth(self) -> list[FaultLine]:
        """Linhas do defeito: `truth` de qualquer execução do bug; senão o patch do catálogo no git."""
        if self._truth is None:
            t: list[FaultLine] = []
            for kind in KINDS:
                run = self.run(kind)
                if run and run.get("truth"):
                    t = [FaultLine(**x) for x in run["truth"]]
                    break
            if not t and (cat := self.catalogo()):
                t = list(cat["faults"])
            self._truth = t
        return self._truth

    def falhas_maven(self) -> list[dict]:
        run = self.run("maven")
        return falhas_da_execucao(run, run_dir(self.home, self.job("maven")["run_rel"])) if run else []

    def posicao(self, kind: str) -> tuple[str, dict | None]:
        """(veredito, posições) do Jaguar `kind`, em cache por execução e gabarito (execuções não mudam)."""
        sc = self.scores(kind)
        if sc is None:
            return "sem_dado", None
        faults = tuple(self.truth())
        key = (str(self.home.runs), self.job(kind)["run_rel"], faults)
        if key not in _pos_cache:
            code, pos = veredito_jaguar(sc, list(faults))
            _pos_cache[key] = {"code": code, "pos": pos}
        c = _pos_cache[key]
        return c["code"], c["pos"]


def falhas_da_execucao(run: dict, d: Path) -> list[dict]:
    """Falhas gravadas na execução (`test`, `cls`, `metodo`, `tipo`, `message`, `frame`, `frame_text`); nas
    antigas, sem `failures`, refaz a partir dos relatórios do surefire guardados."""
    saved = run.get("failures")
    if isinstance(saved, list):
        items = [x for x in saved if isinstance(x, dict)]
    else:
        reports = d / "outputs" / "surefire-reports"
        try:
            items = parse_surefire_failures(reports) if reports.is_dir() else []
        except OSError:
            items = []
    out = []
    for x in items:
        fr = x.get("frame") if isinstance(x.get("frame"), dict) else None
        teste = x.get("test") or "?"
        cls, _, metodo = teste.partition("#")
        out.append({"test": teste, "cls": cls, "short": cls.rsplit(".", 1)[-1], "metodo": metodo,
                    "tipo": x.get("type"), "message": x.get("message"), "frame": fr,
                    "frame_text": frame_texto(fr)})
    return out


def frame_texto(f: dict | None) -> str:
    """"org.x.Foo.bar (Foo.java:12)" de um quadro da pilha."""
    if not f:
        return ""
    onde = f"{f.get('file') or '?'}" + (f":{f['line']}" if f.get("line") else "")
    return f"{f.get('cls')}.{f.get('method')} ({onde})"


def _celula(b: _Bug, kind: str) -> dict:
    """Resumo de um job para a tabela do lote."""
    j = b.job(kind)
    cel = {"kind": kind, "nome": KIND_LABEL[kind], "job_id": j["id"], "state": j["state"], "rel": j.get("run_rel"),
           "status": None, "label": "", "tom": "", "error": j.get("error"), "texto": "", "falhas": None,
           "veredito": None, "pos": None}
    if j["state"] in ("pending", "running"):
        cel.update(texto="na fila" if j["state"] == "pending" else "rodando")
        return cel
    if j["state"] == "cancelled":
        cel.update(texto="cancelado", tom="alerta", label="cancelado")
        return cel
    st = b.status(kind)
    if st is None:      # job concluído sem execução gravada (erro antes de gravar)
        cel.update(texto="erro", tom="erro", label=j.get("error") or "erro")
        return cel
    cel.update(status=st, label=S.label(st), tom=S.tom(st))
    if kind == "maven":
        if S.is_success(st) or st == S.SEM_TESTE_FALHANDO:
            n = len(b.falhas_maven())
            cel["falhas"] = n
            cel["texto"] = "nenhuma falha" if n == 0 else f"{n} falha" + ("s" if n > 1 else "")
        else:
            cel["texto"] = cel["label"]
        return cel
    code, pos = b.posicao(kind)
    cel["veredito"] = veredito(code)
    cel["pos"] = pos["melhor"] if pos else None
    if not S.is_success(st):
        cel["texto"] = cel["label"]
    elif cel["pos"]:
        cel["texto"] = cel["pos"]["texto"]
    else:
        cel["texto"] = "defeito fora do ranking" if b.truth() else "sem gabarito"
    return cel


def _veredito_geral(b: _Bug) -> dict:
    """Melhor veredito entre os Jaguar; só Maven, classes com falha × catálogo."""
    jag = [k for k in ("jaguar1", "jaguar2") if b.job(k)]
    if jag:
        return veredito(melhor_veredito([b.posicao(k)[0] for k in jag]))
    if b.job("maven") and b.status("maven") is not None:
        cat = b.catalogo()
        return veredito(veredito_maven([f["test"] for f in b.falhas_maven()], cat["tests"] if cat else None))
    return veredito("sem_dado")


def _pos_resumo(b: _Bug) -> str:
    """"J2 pos. 1 · J1 pos. 3" (melhor primeiro)."""
    itens = []
    for k in ("jaguar2", "jaguar1"):
        if b.job(k):
            pos = b.posicao(k)[1]
            if pos and pos["melhor"]:
                itens.append((pos["melhor"]["expected"], f"{KIND_CURTO[k]} {pos['melhor']['texto']}"))
    return " · ".join(t for _e, t in sorted(itens))


def lote_view(home, bid: int, top_n: int = TOP_N) -> dict:
    """Tabela do lote: uma linha por bug (na ordem do lote) com a célula de cada ferramenta e o veredito.

    {"bid", "kinds", "bugs": [{key, id, project, branch, kinds, celulas:{kind: célula}, veredito, posicoes}],
     "contagem": {código: n}, "total_jobs", "feitos", "pendente", "sig"}. Não lê o git por bug, exceto o
    patch do catálogo quando nenhuma execução trouxe o gabarito (cache por repositório e ramo)."""
    jobs = jobs_queue.batch_jobs(home, bid)
    kinds = [k for k in KINDS if any(j["kind"] == k for j in jobs)]
    bugs, contagem = [], {c: 0 for c in reversed(list(VEREDITOS))}
    for g in _agrupar(jobs):
        b = _Bug(home, g)
        celulas = {k: _celula(b, k) for k in KINDS if b.job(k)}
        ver = _veredito_geral(b)
        contagem[ver["code"]] += 1
        bugs.append({**_ident(g), "kinds": list(celulas), "celulas": celulas, "veredito": ver,
                     "posicoes": _pos_resumo(b), "catalogo": bool(b.repo)})
    return {"bid": bid, "kinds": kinds, "bugs": bugs, "contagem": contagem, "total_jobs": len(jobs),
            "feitos": sum(1 for j in jobs if j["state"] not in ("pending", "running")),
            "pendente": any(j["state"] in ("pending", "running") for j in jobs),
            "sig": lote_sig(home, bid, jobs)}


def lote_resumos(home, bid: int) -> dict[str, dict]:
    """{bug_key: {"resumo", "descricao", "jira"}} dos bugs do Bugs.jar do lote (lê o git; com cache)."""
    out = {}
    for g in _agrupar(jobs_queue.batch_jobs(home, bid)):
        repo, ramo = _repo_ramo(g)
        if repo:
            out[g["key"]] = {**resumo_bugsjar(repo, ramo), "jira": jira_url(_ident(g)["id"])}
    return out


# ------------------------------------------------------------------ detalhe do bug

def _linhas_ranking(scores: list[LineScore], faltas: set, top_n: int, mais_n: int,
                    outro_top: set[tuple[str, int]]) -> tuple[list[dict], list[dict]]:
    todas = ranking_rows(scores)[:max(top_n, mais_n)]
    rows = [{"rank": r, "cls": c, "short": c.rsplit(".", 1)[-1], "line": ln, "susp": s,
             "defeito": _chave(c, ln) in faltas, "comum": _chave(c, ln) in outro_top} for c, ln, s, r in todas]
    return rows[:top_n], rows[top_n:]


def _top_chaves(scores: list[LineScore] | None, top_n: int) -> set[tuple[str, int]]:
    return {_chave(c, ln) for c, ln, _s, _r in ranking_rows(scores)[:top_n]} if scores else set()


def _bloco_maven(b: _Bug) -> dict | None:
    j = b.job("maven")
    if not j:
        return None
    cel = _celula(b, "maven")
    falhas = b.falhas_maven() if b.run("maven") else []
    cat = b.catalogo()
    esp = esperado_maven([f["test"] for f in falhas], cat["tests"] if cat else None) \
        if b.run("maven") and b.status("maven") is not None else None
    diag = (b.run("maven") or {}).get("diagnosis") or {}
    return {**cel, "falhas_lista": falhas, "n_falhas": len(falhas), "esperado": esp,
            "causa": diag.get("cause") or "" if not S.is_success(cel["status"] or "ok") else "",
            "veredito": veredito(veredito_maven([f["test"] for f in falhas], cat["tests"] if cat else None))
            if esp else veredito("sem_dado")}


def _bloco_jaguar(b: _Bug, kind: str, top_n: int, mais_n: int, outro: str | None) -> dict | None:
    j = b.job(kind)
    if not j:
        return None
    cel = _celula(b, kind)
    sc = b.scores(kind)
    code, pos = b.posicao(kind)
    diag = (b.run(kind) or {}).get("diagnosis") or {}
    causa = ""
    if b.status(kind) is not None and not S.is_success(b.status(kind)):
        causa = diag.get("cause") or (S.CAUSA_SEM_TESTE_FALHANDO if b.status(kind) == S.SEM_TESTE_FALHANDO else "")
    faltas = {_chave(f.cls, f.line) for f in select_lines(b.truth())}
    top, mais = ([], [])
    if sc:
        top, mais = _linhas_ranking(sc, faltas, top_n, mais_n, _top_chaves(b.scores(outro), top_n) if outro else set())
    return {**cel, "veredito": veredito(code), "posicao": pos, "total": len(sc) if sc is not None else None,
            "causa": causa, "top": top, "mais": mais, "tem_mais": bool(mais)}


def _concordancia(b: _Bug, top_n: int) -> dict | None:
    s1, s2 = b.scores("jaguar1"), b.scores("jaguar2")
    if s1 is None or s2 is None:
        return None
    r1, r2 = ranking_rows(s1)[:top_n], ranking_rows(s2)[:top_n]
    d1 = {_chave(c, ln): (s, r) for c, ln, s, r in r1}
    d2 = {_chave(c, ln): (s, r) for c, ln, s, r in r2}
    comuns = [{"cls": k[0], "short": k[0].rsplit(".", 1)[-1], "line": k[1], "susp1": d1[k][0], "susp2": d2[k][0],
               "dif": abs(d1[k][0] - d2[k][0]), "rank1": d1[k][1], "rank2": d2[k][1]}
              for k in d1 if k in d2]
    comuns.sort(key=lambda x: (x["rank2"], x["rank1"], x["cls"], x["line"]))
    topo1, topo2 = (outer(r1[0][0]) if r1 else None), (outer(r2[0][0]) if r2 else None)
    pos = {k: (b.posicao(k)[1] or {}).get("melhor") for k in ("jaguar1", "jaguar2")}
    e1, e2 = (pos["jaguar1"] or {}).get("expected"), (pos["jaguar2"] or {}).get("expected")
    if e1 is None and e2 is None:
        antes = None
    elif e2 is None or (e1 is not None and e1 < e2):
        antes = "jaguar1"
    elif e1 is None or e2 < e1:
        antes = "jaguar2"
    else:
        antes = "empate"
    return {"top_n": top_n, "comuns": comuns, "n_comuns": len(comuns), "de": min(top_n, len(r1), len(r2)) or top_n,
            "texto_comuns": f"{len(comuns)} de {min(top_n, len(r1), len(r2)) or top_n}",
            "mesma_classe_topo": bool(topo1) and topo1 == topo2, "classe_topo": {"jaguar1": topo1, "jaguar2": topo2},
            "pos": {k: (v["texto"] if v else None) for k, v in pos.items()},
            "expected": {"jaguar1": e1, "jaguar2": e2}, "achou_antes": antes}


def _trecho_patch(diff: str, limite: int = 300) -> list[dict]:
    """Linhas do `developer-patch.diff` só dos arquivos de produção, marcadas: arquivo, hunk, rem, add, ctx."""
    out, ativo = [], False
    for ln in diff.splitlines():
        if ln.startswith("diff --git"):
            m = re.search(r" b/(\S+)$", ln)
            ativo = bool(m) and m.group(1).endswith(".java") and not TEST_PATH_RE.search(m.group(1))
            if ativo:
                out.append({"tipo": "arquivo", "texto": m.group(1)})
            continue
        if not ativo or ln.startswith(("index ", "--- ", "+++ ", "new file", "deleted file")):
            continue
        tipo = "hunk" if ln.startswith("@@") else "rem" if ln.startswith("-") else "add" if ln.startswith("+") else "ctx"
        out.append({"tipo": tipo, "texto": ln})
        if len(out) >= limite:
            break
    return out


def _bloco_catalogo(b: _Bug) -> dict | None:
    if not b.repo:
        return None
    ident = _ident(b.g)
    info = resumo_bugsjar(b.repo, b.ramo)
    cat = b.catalogo()
    metodos: dict[tuple[str, str], list[int]] = {}
    linhas = []
    for f in select_lines(b.truth()):
        fonte = fonte_bugsjar(b.repo, b.ramo, f.cls)
        m = metodo_da_linha(fonte[1], f.line) if fonte else None
        if m:
            metodos.setdefault((f.cls, m), []).append(f.line)
        posj = {}
        for k in ("jaguar1", "jaguar2"):
            if b.job(k):
                p = b.posicao(k)[1]
                r = next((x for x in (p["linhas"] if p else []) if x["cls"] == f.cls and x["line"] == f.line), None)
                posj[k] = {**r, "texto": _pos_texto(r), "faixa": _faixa_texto(r)} if r else None
        linhas.append({"cls": f.cls, "short": f.cls.rsplit(".", 1)[-1], "line": f.line, "kind": f.kind, "metodo": m,
                       "pos": posj})
    return {"id": ident["id"], "resumo": info["resumo"], "descricao": info["descricao"], "jira": jira_url(ident["id"]),
            "metodos": [{"cls": c, "short": c.rsplit(".", 1)[-1], "metodo": m, "linhas": ls}
                        for (c, m), ls in metodos.items()],
            "patch": _trecho_patch(cat["diff"]) if cat else [], "testes": cat["tests"] if cat else [],
            "modulo": cat["module"] if cat else None, "linhas": linhas, "fonte": bool(b.ramo)}


def bug_detail(home, bid: int, bug_key: str, top_n: int = TOP_N, more_n: int = MAIS_N) -> dict | None:
    """Detalhe de um bug do lote (spec §3.2); None se o lote não tem esse bug.

    {"bug": {key,id,project,branch,repo}, "kinds", "veredito", "maven": bloco|None,
     "jaguar": {kind: bloco}, "concordancia": dict|None, "catalogo": dict|None, "top_n", "more_n"}.
    Bloco Jaguar: célula (status, label, tom, rel, job_id...) + veredito, posicao (melhor/lista), causa,
    `top` (linhas com rank, cls, short, line, susp, defeito, comum), `mais` (até more_n no total), tem_mais."""
    g = next((x for x in _agrupar(jobs_queue.batch_jobs(home, bid)) if x["key"] == bug_key), None)
    if g is None:
        return None
    b = _Bug(home, g)
    jaguar = {k: _bloco_jaguar(b, k, top_n, more_n, "jaguar2" if k == "jaguar1" else "jaguar1")
              for k in ("jaguar1", "jaguar2") if b.job(k)}
    return {"bug": {**_ident(g), "repo": b.repo}, "kinds": [k for k in KINDS if b.job(k)],
            "veredito": _veredito_geral(b), "maven": _bloco_maven(b), "jaguar": jaguar,
            "concordancia": _concordancia(b, top_n) if len(jaguar) == 2 else None,
            "catalogo": _bloco_catalogo(b), "top_n": top_n, "more_n": more_n}


# ------------------------------------------------------------------ CSV

CSV_COLUNAS = ["projeto", "bug", "ramo", "resumo", "status_maven", "falhas_maven", "falhou_o_esperado",
               "status_j1", "status_j2", "pos_esperada_j1", "pos_melhor_j1", "pos_pior_j1",
               "pos_esperada_j2", "pos_melhor_j2", "pos_pior_j2", "veredito_j1", "veredito_j2", "veredito_geral",
               "linhas_comuns_top10", "mesma_classe_topo", "metodo_defeito"]


def lote_csv(home, bid: int) -> str:
    """CSV do lote, uma linha por bug (colunas em `CSV_COLUNAS`; vazio onde a ferramenta não rodou)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUNAS)
    for g in _agrupar(jobs_queue.batch_jobs(home, bid)):
        b = _Bug(home, g)
        ident = _ident(g)
        st = {k: (b.status(k) if b.job(k) and b.run(k) else "") for k in KINDS}
        stlabel = {k: S.label(v) if v else ("" if not b.job(k) else _estado_job(b, k)) for k, v in st.items()}
        falhas = esperado = ""
        if b.job("maven") and st["maven"] is not None and b.run("maven"):
            lista = b.falhas_maven()
            falhas = len(lista)
            cat = b.catalogo()
            esperado = esperado_maven([f["test"] for f in lista], cat["tests"] if cat else None)["texto"]
        row = [ident["project"], ident["id"], ident["branch"],
               resumo_bugsjar(b.repo, b.ramo)["resumo"] if b.repo else "", stlabel["maven"], falhas, esperado,
               stlabel["jaguar1"], stlabel["jaguar2"]]
        vers = {}
        for k in ("jaguar1", "jaguar2"):
            if b.job(k):
                code, pos = b.posicao(k)
                m = pos["melhor"] if pos else None
                row += [f"{m['expected']:g}", m["best"], m["worst"]] if m else ["", "", ""]
                vers[k] = VEREDITOS[code][1]
            else:
                row += ["", "", ""]
                vers[k] = ""
        row += [vers["jaguar1"], vers["jaguar2"], _veredito_geral(b)["label"]]
        conc = _concordancia(b, TOP_N)
        row += [conc["n_comuns"] if conc else "", ("sim" if conc["mesma_classe_topo"] else "não") if conc else ""]
        metodos = _bloco_catalogo(b)["metodos"] if b.repo else []
        row.append("; ".join(f"{m['short']}.{m['metodo']}" for m in metodos))
        w.writerow(row)
    return buf.getvalue()


def _estado_job(b: _Bug, kind: str) -> str:
    j = b.job(kind)
    return {"pending": "na fila", "running": "rodando", "cancelled": "cancelado"}.get(j["state"], j.get("error") or "erro")
