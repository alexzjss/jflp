"""Status de uma execução do JFLP (strings exatas, gravadas em meta.json e mostradas na UI)."""
from __future__ import annotations

OK = "ok"
OK_TEST_FAIL = "ok_testes_falhando"
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


def is_success(status: str) -> bool:
    return status in (OK, OK_TEST_FAIL)
