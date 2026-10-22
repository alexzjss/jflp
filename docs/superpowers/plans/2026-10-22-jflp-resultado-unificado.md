# Plano — fluxo único e resultado unificado (v0.5)

Spec: `docs/superpowers/specs/2026-10-22-jflp-resultado-unificado-design.md`.
Execução: tarefas sequenciais (mexem nos mesmos arquivos), cada uma por um subagente; revisão do Opus entre
elas. Testes: `.venv/Scripts/python.exe -m unittest discover -s tests` (≈3 min). Cada tarefa termina com a
suíte verde e um commit próprio (mensagem em português, estilo `feat:`/`fix:`/`refactor:`).

| # | Tarefa | Modelo |
|---|--------|--------|
| 1 | Base: corrigir os 8 testes que falham (3 do Painel → página inicial agora é Rodar; 5 de WSL em `test_detect`), commitar o trabalho pendente da v0.4 | Sonnet |
| 2 | `sbfl/bugview.py` (§3.2–3.4 e §6): resumo Maven, ranking Jaguar, posições, vereditos, concordância, catálogo (resumo/JIRA, método do defeito, trecho do patch, testes esperados), fonte via `git show` para Bugs.jar, CSV; com testes | Sonnet |
| 3 | UI: Rodar (multi-projeto, resumo/busca/filtros, padrões, Opções, resultado na mesma tela), vista do lote (tabela + detalhe + CSV), `/lote/{bid}`, Resultados (lista de lotes), "ver no código" com fallback `git show`; com testes | Sonnet |
| 4 | Remoção (§1): mover helpers para módulo comum, apagar rotas/templates/testes das telas removidas, enxugar a barra lateral e a página da execução | Haiku |
| 5 | Ambiente enxuto (§5) + README (seção 2 e índice) refletindo o fluxo novo | Haiku |
| 6 | Revisão final + validação real: 1–2 bugs pequenos do bugs-dot-jar com Maven + J1 + J2 pela UI | Opus |
