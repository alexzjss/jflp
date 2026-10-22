# JFLP Studio — fluxo único e resultado unificado (v0.5)

Data: 2026-10-22. Base: `feat/jflp-studio` (tela Rodar/Resultados da v0.4, ainda não commitada no início).
Decisões tomadas pelo usuário em perguntas de múltipla escolha; o que é "decisão do construtor" está marcado.

## Intenção

Usar o app é: **escolher a pasta do bugs-dot-jar → marcar bugs (de um ou mais projetos) → escolher
Maven / Jaguar 1 / Jaguar 2 → Rodar**, e na **mesma tela** ver, por bug, o que o Maven acusou, o que cada
Jaguar apontou (linhas e suspeição), se os dois Jaguar concordam e se isso bate com o defeito catalogado no
próprio repositório. Tudo que não serve a isso sai da interface.

Não-objetivos: calcular fórmulas SBFL/EXAM próprias (a suspeição é sempre a gravada pelo Jaguar); mexer na
CLI (receitas, exportar e lotes continuam na linha de comando); Defects4J na UI.

Restrições que continuam valendo: textos em português; subprocessos em lista; servidor só em 127.0.0.1;
nada alterado fora da pasta do JFLP; testes `unittest` sem rede; nunca comparar caminhos com `resolve()`
(AppData redirecionado do MSIX).

## 1. Interface: o que fica e o que sai

Barra lateral: **Rodar**, **Resultados**, **Ambiente**. Sem menu "Mais".

Removidos (rotas, templates e testes; a lógica de domínio em `queue`, `compare`, `recipes`, `dataset`
continua, pois a CLI e a nova tela usam): Painel (`/painel*`), Executar (`/executar*`), Bugs (`/bugs*`,
`/bug/{key}*`, revisão do POM na UI), Lotes (`/lotes`, telas antigas de `/lote/{bid}*`), Fila (`/fila`),
Comparar (`/comparar`), Receitas (`/receitas`), Exportar (`/exportar`), botão "Salvar receita" na página da
execução.

Ficam: `/rodar*`, `/resultados`, `/ambiente*`, `/bem-vindo*`, `/run/{rel}*` (detalhes, código, log,
arquivos), `/job/{id}/ao-vivo`, `/job/{id}/mini`, `/job/{id}/stream`, `/job/{id}/cancelar`,
`/fila/contagem` (selo da barra lateral), e a nova vista de lote (§3).

Helpers hoje em `routes_quick`/`routes_bugs` que a Rodar usa (`_pick_folder`, `_tk_available`, `_ramo`,
`is_bugsjar_repo_ou_ramo`, `add_recente`, `load_recentes`, `KINDS`, `kinds_validos`, …) migram para um
módulo comum (`sbfl/web/comum.py`, nome a critério do construtor) antes de apagar os arquivos.

## 2. Tela Rodar

Passos como hoje (pasta → projeto → bugs → ferramentas), com:

- **Vários projetos de uma vez**: na coleção, cada projeto é uma seção recolhível com seus bugs; a seleção
  vale entre projetos e o contador mostra "N bugs em M projetos". Um único POST cria um lote com todos.
- **Lista de bugs**: ID (ex.: `LANG-7`) + **resumo do JIRA** (`bugsjar_summary`; carregar sob demanda por
  projeto, pois lê o git), **busca** por ID/resumo, **marcar todos/nenhum (visíveis)**, **filtro por status
  anterior**: todos / nunca rodados / com falha / OK (usa `_ultimo_status`).
- **Ferramentas**: Maven + Jaguar 2 marcados por padrão; Jaguar 1 desmarcado.
- **Testes**: "relevantes do catálogo" por padrão; "todos" como alternativa.
- **Opções** (`<details>` fechado): busca automática de correções, ligada por padrão.
- **Rodar** não sai da página: o formulário recolhe numa linha ("12 bugs · Maven, Jaguar 2 · alterar") e
  abaixo aparece a vista do lote (§3), com `hx-push-url` para `/rodar?lote=N` (recarregar restaura).
- Pasta de projeto Maven comum (sem Bugs.jar) continua aceita: vira um lote de 1 "bug" sem gabarito.

## 3. Vista do lote (tabela por bug + detalhe)

Fragmento único usado em `/rodar?lote=N` e em `/lote/{bid}` (página cheia, aberta pela Resultados).

### 3.1 Tabela (uma linha por bug)

Colunas: **Bug** (ID + projeto + resumo curto) · **Maven** · **Jaguar 1** · **Jaguar 2** · **Gabarito**.
Colunas de ferramenta não usadas no lote somem.

- Enquanto roda: cada célula mostra o andamento do job (`/job/{id}/mini`, já existente) e "na fila".
  A tabela só é re-renderizada quando um job muda de estado (mesmo esquema `sig`/204 de `_executar_cartoes`).
- Terminado: Maven → "2 falhas" / "nenhuma falha" / rótulo do status de erro (cor do app);
  Jaguar → "pos. 3" (posição esperada da melhor linha do defeito; tooltip com a faixa de empate) ou o
  rótulo do erro; Gabarito → veredito (§3.3).
- Cabeçalho: contagem por veredito (✓ 7 · ~ 2 · ✗ 1 · — 2), botão **Baixar CSV**, botão **Cancelar lote**
  enquanto houver pendentes.
- Clicar na linha expande o detalhe embaixo dela (carregado sob demanda, `hx-get`).

### 3.2 Detalhe do bug

Três colunas (Maven | Jaguar 1 | Jaguar 2; só as que existirem) e um bloco de Gabarito e de Concordância.

**Maven**: testes que falharam (`Classe#metodo`), tipo e mensagem da exceção; **linha do erro**: primeiro
frame em código de produção (`first_project_frame`, já gravado em `failures[].frame`) com link para o
código; **falhou o esperado?** comparando, no nível de classe, as classes de teste com falha contra as do
catálogo (`bugsjar_patch(...)["tests"]`): "iguais" / "a mais: …" / "faltando: …".

**Cada Jaguar**: top-10 linhas (classe curta, linha, suspeição) com "ver mais" até 50; a linha do patch
ganha ★; linhas que também estão no top-N do outro Jaguar ficam destacadas; link "ver no código" por linha;
status/diagnóstico quando falhou (link para `/run/{rel}`).

**Concordância J1 × J2** (só com os dois): linhas em comum no top-10 ("7 de 10"); mesma classe no topo
(sim/não, quais); posição do defeito em cada um (J1 pos. 3 × J2 pos. 1, quem achou antes); tabela das
linhas comuns com susp J1, susp J2 e diferença.

**Gabarito (catálogo do repositório)**: resumo do bug e link do JIRA (`bug-report.yml`; o link é montado do
ID quando o arquivo não traz URL — decisão do construtor); **método do defeito** (método que contém as
linhas do patch, achado no fonte da versão com defeito); linhas do defeito com a posição em cada Jaguar;
**trecho do `developer-patch.diff`** (recolhível) com as linhas removidas/alteradas destacadas; testes que
falham no catálogo.

Fonte para "ver no código" e método do defeito em bugs do Bugs.jar: como a Rodar grava sem o checkout
arquivado (`archive_checkout: False`), o código é lido do ramo do bug no próprio repositório
(`git show <ramo>:<caminho>`) quando a execução não tem `checkout.*`.

### 3.3 Veredito (por Jaguar e geral)

Por Jaguar, usando a **posição esperada** (meio da faixa de empate) da melhor linha do patch:
- **✓ achou**: posição ≤ 10;
- **~ parcial**: posição ≤ 50, ou alguma classe do patch entre as classes das 10 linhas do topo;
- **✗ não achou**: o resto (inclui defeito fora do ranking);
- **—**: sem gabarito, sem execução Jaguar ou execução com erro.

Geral (coluna Gabarito): o melhor veredito entre os Jaguar do lote, com as posições ao lado
("✓ J2 pos. 1 · J1 pos. 3"). Só Maven: ✓ se as classes com falha batem com o catálogo, ~ se há interseção,
✗ se nenhuma, — sem catálogo.

### 3.4 CSV

`/lote/{bid}/resultado.csv`, uma linha por bug: projeto, bug, ramo, resumo, status Maven, nº falhas,
falhou o esperado, status J1/J2, posição J1/J2 (esperada, melhor, pior), veredito J1/J2/geral, linhas em
comum no top-10, mesma classe no topo, método do defeito.

## 4. Resultados

Lista dos lotes (mais recentes primeiro): nome, data, ferramentas, nº de bugs, contagem de vereditos,
estado (rodando/terminado/cancelado), excluir. Abrir → `/lote/{bid}` com a vista do §3. Sai a lista de
"execuções recentes" e o estado da fila (o andamento está na própria vista do lote).

## 5. Ambiente

Mantido, com o visual enxugado: um cartão por perfil (Maven puro, Jaguar 1, Jaguar 2) com ✓/✗ e o botão
do que falta; detalhes técnicos (JDKs, Mavens encontrados, WSL/Defects4J) num `<details>` "Detalhes".

## 6. Onde mora a lógica

Novo módulo puro `sbfl/bugview.py` (sem FastAPI) que, dado o `home`, o `settings` e os jobs de um lote,
monta por bug: resumo Maven, ranking de cada Jaguar, posições do defeito, vereditos, concordância e dados
do catálogo; mais a função do CSV. As rotas só chamam e renderizam. Testável com execuções gravadas
falsas (como os testes atuais de `compare`).

## Deixado ao construtor

Nomes de módulos/rotas internas, CSS (seguir `app.css`), heurística do método que contém uma linha
(varredura de chaves com regex de assinatura basta), cache leve dos resumos do JIRA por sessão.
