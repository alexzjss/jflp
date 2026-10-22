"""Status de uma execução do JFLP (strings exatas, gravadas em meta.json e mostradas na UI)."""
from __future__ import annotations

OK = "ok"
OK_TEST_FAIL = "ok_testes_falhando"
SEM_TESTE_FALHANDO = "sem_teste_falhando"   # rodou, mas nenhum teste falhou: o SBFL não tem o que apontar
CHECKOUT_FALHOU = "checkout_falhou"
PROJETO_INVALIDO = "projeto_invalido"   # sem pom.xml na pasta ou módulo inexistente: nenhuma correção resolve
JDK_INCOMPATIVEL = "jdk_incompativel"
DEPENDENCIA_NAO_RESOLVIDA = "dependencia_nao_resolvida"
PLUGIN_INCOMPATIVEL = "plugin_incompativel"
COMPILACAO_FALHOU = "compilacao_falhou"
CLASSES_DUPLICADAS = "classes_duplicadas"
TESTES_NAO_ENCONTRADOS = "testes_nao_encontrados"
JAGUAR_SEM_SAIDA = "jaguar_sem_saida"
JAGUAR_CRASH = "jaguar_crash"
TIMEOUT = "timeout"
INTERROMPIDO = "interrompido"
DESCONHECIDO = "desconhecido"

INFRA = (
    CHECKOUT_FALHOU,
    PROJETO_INVALIDO,
    JDK_INCOMPATIVEL,
    DEPENDENCIA_NAO_RESOLVIDA,
    PLUGIN_INCOMPATIVEL,
    COMPILACAO_FALHOU,
    CLASSES_DUPLICADAS,
    TESTES_NAO_ENCONTRADOS,
    JAGUAR_SEM_SAIDA,
    JAGUAR_CRASH,
    TIMEOUT,
    INTERROMPIDO,
    DESCONHECIDO,
)


CAUSA_SEM_TESTE_FALHANDO = ("Nenhum teste falhou: o SBFL precisa de ao menos um teste falhando para apontar linhas. "
                            "Confira se está no commit com o bug e no módulo certo.")

LABEL = {
    OK: "concluída", OK_TEST_FAIL: "concluída, com testes falhando",
    SEM_TESTE_FALHANDO: "concluída, mas nenhum teste falhou",
    CHECKOUT_FALHOU: "não foi possível obter o código", PROJETO_INVALIDO: "projeto inválido",
    JDK_INCOMPATIVEL: "JDK incompatível", DEPENDENCIA_NAO_RESOLVIDA: "dependência não resolvida",
    PLUGIN_INCOMPATIVEL: "plugin incompatível", COMPILACAO_FALHOU: "erro de compilação",
    CLASSES_DUPLICADAS: "classes duplicadas", TESTES_NAO_ENCONTRADOS: "testes não encontrados",
    JAGUAR_SEM_SAIDA: "o Jaguar não gerou resultado", JAGUAR_CRASH: "o Jaguar caiu", TIMEOUT: "tempo esgotado",
    INTERROMPIDO: "interrompida", DESCONHECIDO: "falha não reconhecida",
}


# `sem_teste_falhando` não é falha de infraestrutura (INFRA) nem sucesso: rodou, mas não há o que localizar.


def is_success(status: str) -> bool:
    return status in (OK, OK_TEST_FAIL)


AVISO = (SEM_TESTE_FALHANDO,)   # não é sucesso nem erro: aparece em âmbar (`alerta`)


def tom(status: str) -> str:
    """Estilo visual de um status: `ok` (sucesso), `alerta` (aviso, âmbar) ou `erro` (vermelho)."""
    if is_success(status):
        return "ok"
    return "alerta" if status in AVISO else "erro"


def label(status: str) -> str:
    return LABEL.get(status, status or "")


def effective(status: str, tests: dict | None) -> str:
    """Status para mostrar: execuções antigas gravaram `ok` sem nenhum teste falhando, ou contaram errado."""
    t = tests or {}
    failing = (t.get("failed") or 0) or (t.get("error") or 0)
    if status == OK and (t.get("total") or 0) > 0 and not failing:
        return SEM_TESTE_FALHANDO
    if status == SEM_TESTE_FALHANDO and failing:     # contagem refeita a partir do XML do Jaguar
        return OK_TEST_FAIL
    return status
