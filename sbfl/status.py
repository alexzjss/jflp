"""Status de uma execução do JFLP (strings exatas, gravadas em meta.json e mostradas na UI)."""
from __future__ import annotations

OK = "ok"
OK_TEST_FAIL = "ok_testes_falhando"

INFRA = (
    "checkout_falhou",
    "jdk_incompativel",
    "dependencia_nao_resolvida",
    "plugin_incompativel",
    "compilacao_falhou",
    "classes_duplicadas",
    "testes_nao_encontrados",
    "jaguar_sem_saida",
    "jaguar_crash",
    "timeout",
    "interrompido",
    "desconhecido",
)


def is_success(status: str) -> bool:
    return status in (OK, OK_TEST_FAIL)
