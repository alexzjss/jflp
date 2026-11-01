# JFLP Studio — cobertura completa do bugs-dot-jar (v0.6)

Data: 2026-10-30. Base: `feat/jflp-studio`.
Decisões do usuário estão escritas como tal; as marcadas **(decisão minha)** foram tomadas por mim durante a conversa e podem ser revistas.

## Intenção

O usuário faz uma pesquisa sobre localização de defeitos e precisa de uma **cobertura de testes do bugs-dot-jar inteiro**
(1.158 bugs em 8 projetos), com o resultado de **Maven, Jaguar 1 e Jaguar 2 para cada bug** numa **planilha do Google**
que ele compartilha. Hoje isso está dividido em dois mundos que não se falam:

- o **JFLP Studio** (este repo): roda Maven/J1/J2, mas o usuário escolhe bugs à mão, em lotes, com **um worker sequencial**,
  e o resultado sai num CSV por lote;
- um **pipeline PowerShell avulso** em `E:\Documentos\GitHub\bugs-dot-jar\relatorios_cobertura\_tools\`
  (`run_bug2.ps1`, `run_trial.ps1`, `make_xlsx.py`), criado na sessão "Maven e Jaguar test coverage": roda só Maven + Jaguar 1,
  com 6 workers em worktrees, e gera um `.xlsx` à parte. ~140 bugs executados (59 do commons-math com dados bons).

A v0.6 junta tudo no app: **aponta-se a pasta do bugs-dot-jar uma vez e o app cobre todos os bugs sozinho**, sabe o que já
rodou e o que falta, deixa pausar/parar/continuar/refazer, e manda cada resultado para a planilha do Google automaticamente.

### Objetivos
1. Modo **Cobertura**: todos os bugs de todos os projetos da pasta, cada um com Maven + Jaguar 1 + Jaguar 2.
2. Estado persistente por bug, visível no app (pendente, rodando, OK, parcial, falhou, não executável) com motivo.
3. **Pausar** (termina o bug atual e para), **Parar** (interrompe já e devolve o bug à fila), **Continuar** (retoma de onde
   parou, inclusive após fechar o app ou reiniciar o PC), **Refazer** falhados (todos, por projeto, por seleção, por bug).
4. **Planilha do Google atualizada sozinha** a cada bug concluído, linha atualizada no lugar (sem duplicar).
5. **Importar** os resultados já produzidos pelo pipeline PowerShell.
6. Fazer rodar o máximo possível de bugs, inclusive os legados (camel/wicket antigos), com esforço médio.
7. Paralelismo (vários workers) para caber em dias, não semanas.

### Não-objetivos
- Linux/rodar em duas máquinas na mesma cobertura (o usuário quer só Windows, deixado ligado).
- Calcular fórmulas SBFL/EXAM próprias: a suspeição é sempre a gravada pelo Jaguar.
- Defects4J na interface.
- Conta de serviço / projeto no Google Cloud.

### Anti-objetivos (seria fracasso mesmo "funcionando")
- Perder progresso ao fechar o app, ao travar um Maven ou ao reiniciar o PC.
- Planilha com linhas duplicadas, desatualizada em relação ao app, ou que para de receber dados em silêncio.
- Encher o disco C: (já aconteceu: C: chegou a 0 GB). Trabalho pesado vai para `E:`.
- Gastar horas num bug travado: todo passo tem timeout.
- Misturar resultado importado com resultado do app sem dar para distinguir.

## Restrições que continuam valendo
Textos em português; subprocessos em lista; servidor só em 127.0.0.1; nada alterado fora da pasta do JFLP (e da pasta de
trabalho configurada); testes `unittest` sem rede; **nunca comparar caminhos com `resolve()`** (AppData redirecionado do
MSIX quando o processo é lançado pelo Claude); repositórios do bugs-dot-jar não podem ficar alterados (checkout por
worktree descartável, nunca `reset --hard`/`clean -fdx` no clone do usuário).

## 1. Interface

Barra lateral: **Cobertura** (nova página inicial), **Resultados**, **Ambiente**. Esboços aprovados (opções A e C) em
https://claude.ai/artifact/PQjS4fyrUdwJsY5RdWggV4 — o usuário escolheu **"A + tabela C"**.

A tela **Rodar** (escolha manual de bugs) sai da barra lateral **(decisão minha)**: o "refazer este bug" e a seleção em massa
da tabela cobrem o uso manual. As rotas de detalhe de execução (`/run/...`, código, log) ficam, porque a tabela linka para elas.

### 1.1 Painel (opção A) — `/cobertura`
- Cabeçalho: pasta do bugs-dot-jar, nº de projetos, ferramentas. Selo de estado (Rodando / Pausando… / Pausado / Parado).
- Botões: **Pausar após o bug atual** ↔ **Continuar**, **Parar agora**, **Refazer falhados (N)**.
- Progresso geral: "231 de 1.158 concluídos (20%)", barra segmentada por situação com legenda e contagem, ritmo (bugs/h),
  tempo restante estimado, nº de workers.
- Estado da planilha: "sincronizada há 12 s · N linhas · abrir planilha", ou em vermelho "planilha sem enviar há X min ·
  N pendentes · motivo" quando o envio falha.
- Tabela **por projeto**: barra de progresso, OK, falhou, não executável, pendentes, link "ver" (abre a tabela C filtrada).
- **Rodando agora**: um item por worker com o bug, há quanto tempo, e em que etapa está (Maven / Jaguar 1 / Jaguar 2).
- **Últimos concluídos**: bug, posições J1/J2 ou motivo, veredito.
- Atualização ao vivo (o app já usa htmx + SSE em `/job/{id}/stream`; o mecanismo fica a critério do construtor).

### 1.2 Tabela (opção C) — `/cobertura/bugs`
- Faixa de progresso compacta + Pausar/Parar.
- Filtros por situação com contagem (Todos, Pendentes, Rodando, OK, Parcial, Falharam, Não executáveis), seletor de projeto,
  busca por ID ou motivo. Filtros na URL (recarregar mantém).
- Seleção em massa: **Refazer selecionados**, **Marcar como não executável**, **Pôr no início da fila**.
- Colunas: Bug (+ projeto), Situação, Maven (testes · falhas), Jaguar 1 (posição do patch ou erro), Jaguar 2 (idem),
  J1×J2 (linhas em comum no top 10), Motivo/perfil, ação "refazer". Paginação.
- Clique no bug abre o detalhe que já existe hoje (Maven, cada Jaguar, concordância, gabarito do catálogo).
- Resultado importado é identificado como tal (ex.: "importado" no Motivo/perfil).

## 2. Modelo de execução

### 2.1 A cobertura
Uma **cobertura** é ligada a uma pasta raiz do bugs-dot-jar (hoje `E:\Documentos\GitHub\bugs-dot-jar`, já é a
`bugsjar.root`). Ao abrir/atualizar, o app enumera todos os ramos `origin/bugs-dot-jar_*` de cada projeto e garante uma
entrada por bug (bugs novos entram como pendentes; nada é apagado). Existe uma cobertura só por vez **(decisão minha)**.

Para cada bug guardam-se três **etapas** (Maven, Jaguar 1, Jaguar 2), cada uma com estado próprio
(pendente / rodando / ok / falhou / não executável), a execução (`runs/...`) que a produziu, tentativas e motivo.
A **situação do bug** é derivada:
- **OK**: as três etapas terminaram com dado (Maven rodou testes; cada Jaguar gerou ranking);
- **Parcial**: ao menos uma etapa com dado e alguma falhou/não executável;
- **Falhou**: nenhuma etapa com dado e ainda há caminho a tentar;
- **Não executável**: todos os perfis tentados falharam (ou o usuário marcou);
- **Rodando** / **Pendente**.

O veredito contra o patch (✓ achou / ~ parcial / ✗ não achou / — sem dado) continua a regra que já existe no app (v0.5)
e é uma coluna à parte da situação.

### 2.2 Fila, workers, pausa
- **N workers** em paralelo; padrão 4, configurável em `config.toml` e na tela Ambiente **(decisão minha;** a máquina tem
  12 núcleos lógicos; a sessão anterior usou 6).
- Cada worker pega um **bug inteiro** e roda as etapas pendentes dele em sequência (Maven → J1 → J2), num **diretório de
  trabalho próprio** (worktree descartável do ramo do bug), apagado ao fim.
- Ordem: **alternando projetos** (round-robin), para a planilha ter amostra de todos os projetos cedo **(decisão minha)**;
  "Pôr no início da fila" fura essa ordem.
- **Pausar**: nenhum worker pega bug novo; os atuais terminam. **Parar**: mata a árvore de processos de cada worker, e o bug
  volta a pendente (etapas já concluídas dele ficam). **Continuar**: volta a distribuir.
- Ao abrir o app, execuções que ficaram pela metade viram pendentes, e a cobertura **retoma sozinha se estava rodando quando
  o app fechou** (se estava pausada, continua pausada) **(decisão minha)**.
- O app deve impedir o Windows de dormir enquanto a cobertura roda (já existe `sbfl/power.py`).
- Tudo que é pesado (worktrees, `target/`, temporários do Maven) fica numa **pasta de trabalho em `E:`**, configurável;
  padrão `E:\JFLP\cobertura` nesta máquina. O `m2/` e os `runs/` também devem poder morar em `E:` (hoje `JFLP_HOME` fica em
  `%LOCALAPPDATA%\jflp`, no C:).
- Timeouts por etapa (os que o app já tem para Maven/J1/J2 continuam valendo).

### 2.3 Refazer
- **Refazer falhados** (painel): recoloca na fila as etapas com falha de todos os bugs **Falhou** e **Parcial**; não mexe em
  **Não executável** nem em etapas OK.
- Por seleção / por bug (tabela): refaz as etapas não-OK; um "refazer tudo" do bug (incluindo etapas OK) fica a critério do
  construtor.
- Refazer gera nova execução; a planilha mostra sempre a mais recente; o histórico fica no app.

### 2.4 Bugs legados (esforço médio)
O usuário quer que se tente fazer rodar os bugs legados **se eles ainda trouxerem dado útil para a pesquisa**; caso contrário,
esforço médio basta. Por bug e etapa, o app tenta em sequência, automaticamente:
1. **perfil moderno** (o que o app já escolhe hoje para cada ferramenta);
2. **perfil legado**: JDK 7 + Maven 3.0.5 (Zulu 7 e Maven 3.0.5 portáteis, baixados pela tela Ambiente para a pasta do JFLP);
3. **legado offline**: dependências resolvidas antes com JDK 8 (TLS moderno) no `m2` próprio e depois o build legado
   em `-o` no JDK 7. Era a saída mais promissora que ficou em aberto na sessão anterior.

Se os três falham, a etapa fica **Não executável** com o motivo diagnosticado. O construtor deve medir num piloto se os bugs
legados recuperados de fato **reproduzem a falha do bug** (testes falhando como no `test-results.txt`); se a maioria não
reproduzir, basta o passo 1 e 2 e marcar o resto, sem insistir.

Correções descobertas no pipeline PowerShell que o app precisa ter (onde já não tiver):
- `settings.xml` que redireciona repositórios `http://` para o Maven Central via HTTPS (Maven ≥ 3.8.1 bloqueia http);
  **não** desativar o bloqueio de http;
- `-Djacoco.skip=true -DargLine=` no Maven puro (JaCoCo 0.6.x antigo derruba a JVM no JDK 8u504);
- Jaguar 1 em paralelo: a porta 6300 do JaCoCo é fixa no cliente do Jaguar 1; cada worker precisa de porta própria
  (a sessão anterior alterou o Jaguar 1 para ler `-Djaguar.port`). Jaguar 2 roda dentro do Surefire e precisa ser conferido
  quanto a colisões entre workers;
- classpath do Jaguar 1 em multimódulo: incluir `target/classes` dos módulos irmãos e o `dependency:build-classpath`;
- wicket antigo sem `pom.xml` na raiz → **Não executável** ("sem pom.xml").

## 3. Importação dos resultados existentes
O usuário pediu para importar o que o pipeline PowerShell já produziu, em vez de refazer.
- Fonte: `E:\Documentos\GitHub\bugs-dot-jar\relatorios_cobertura\<projeto>\<bug>\summary.json` + `control-flow*.xml`
  (Jaguar 1) + `maven.log`. Há dois formatos de `summary.json` (pipeline antigo do commons-math e `run_bug2.ps1`);
  `make_xlsx.py` dessa pasta já sabe ler os dois e calcular a posição do patch quando falta.
- Ignorar lixo conhecido: a entrada `relatorios_cobertura/commons-math/origin/...` do piloto que falhou.
- Os importados entram com Maven e Jaguar 1 preenchidos e **Jaguar 2 pendente**: a cobertura roda só o J2 neles
  **(decisão minha)**. Etapas importadas que falharam ficam como falhou (o "Refazer falhados" refaz pelo app).
- Importado fica marcado como tal no app e na planilha. A importação é idempotente (rodar de novo não duplica).
- Representação no armazenamento (pasta `runs/` própria, tabela à parte, …) a critério do construtor, desde que
  `jflp reindex` não perca nem duplique os importados.

## 4. Planilha do Google
Decisão do usuário: **Google Sheets**, via **Apps Script publicado como app da web** (sem Google Cloud). O usuário já criou a
planilha vazia.

- O repositório traz o script (`tools/planilha/Code.gs` ou similar) e um passo a passo em português no README:
  Extensões → Apps Script → colar → Implantar → App da web (executar como: eu; acesso: qualquer pessoa com o link) →
  copiar URL → colar na tela Ambiente do JFLP.
- O script recebe POST JSON e faz **upsert por chave (projeto + bug)** na aba **Bugs**, cria abas/cabeçalhos se não
  existirem, e mantém a aba **Resumo** (totais por projeto: rodados, OK, parcial, falhou, não executável; achou/parcial/
  não achou por Jaguar), de preferência com fórmulas.
- Um **token** compartilhado (gerado pelo app, colado no script como propriedade) protege o endpoint contra escrita de
  terceiros que descubram a URL **(decisão minha)**.
- O app guarda uma **caixa de saída persistente** (no SQLite): cada bug concluído/alterado vira um item; um envio em segundo
  plano manda em lotes, com nova tentativa e espera crescente quando falha (sem internet, cota do Apps Script). Nada se
  perde se o app fechar. Botão **Reenviar tudo** na tela Ambiente (sincronização completa).
- O estado do envio aparece no painel (§1.1) — falha nunca é silenciosa.
- Testes sem rede: o cliente HTTP é injetável/falso nos testes.

Colunas da aba **Bugs** (proposta minha, aceita pelo usuário como ponto de partida; ajustar é barato):

| Grupo | Colunas |
|---|---|
| Identificação | Projeto · Bug (JIRA) · Resumo · Link JIRA |
| Estado | Situação · Última execução · Tentativas · Origem (app/importado) |
| Maven | Testes (total / falhas / erros) · Testes que falharam · Bateu com o catálogo? |
| Jaguar 1 | Status · Posição da 1ª linha do patch · Classe:linha do topo |
| Jaguar 2 | Status · Posição da 1ª linha do patch · Classe:linha do topo |
| Comparação | Veredito J1 · Veredito J2 · Concordância J1×J2 (linhas em comum no top 10) |
| Diagnóstico | Motivo da falha (português) · Perfil usado (moderno/legado/offline) |

Os 1.158 bugs aparecem na planilha desde o início, os não rodados como "pendente" **(decisão minha)**, para o leitor ver o
tamanho do universo.

## 4a. Respostas à revisão do construtor (decisões minhas, salvo indicação)
- **Testes (decisão do usuário)**: a cobertura roda a **suíte inteira** de cada bug em Maven, Jaguar 1 e Jaguar 2 (não os
  "relevantes do catálogo"), no mesmo critério dos resultados importados.
- **Universo**: 1.158 = soma dos ramos `origin/bugs-dot-jar_*` (accumulo 98, camel 147, commons-math 147, flink 70,
  jackrabbit-oak 278, logging-log4j2 81, maven 48, wicket 289), conferido em 2026-10-30. Wicket sem `pom.xml` conta no total
  e fica Não executável.
- **Apps Script**: implantar "executar como: eu · acesso: qualquer pessoa"; token vai no corpo JSON; `LockService` no upsert;
  o cliente segue o redirecionamento 302 do `script.google.com` (POST → GET em `googleusercontent.com`) e confere a resposta
  JSON `{ok:true}`. Os testes automáticos usam cliente falso; o teste contra a planilha real é manual (piloto).
- **Isolamento entre workers**: já embutido (porta JaCoCo/Jaguar, diretórios de saída e worktree por worker), sem spike prévio.
- **m2**: um só, compartilhado; chave de configuração para `m2` por worker desde o início (desligada).
- **Legado offline**: o piloto não bloqueia o resto. Se menos da metade dos bugs legados recuperados reproduzir a falha do
  catálogo, o passo 3 é desligado por configuração e os bugs viram Não executável.
- **Refazer importado**: gera execução nova do app; a importada fica no histórico; vale a mais recente.
- **Lotes antigos**: não entram na cobertura; a tela Resultados continua mostrando-os.
- **Pasta de trabalho**: chave nova `cobertura.work_dir` (padrão `E:\JFLP\cobertura` quando existir `E:`); o resto do
  `JFLP_HOME` não é movido automaticamente (a variável `JFLP_HOME` já permite apontar para `E:`).
- **Maven sem falha** (defeito não reproduzido): a etapa é OK (rodou testes); "Bateu com o catálogo?" = "não reproduziu".

## 5. O que fica para o construtor
- Esquema SQLite (tabelas da cobertura e da caixa de saída) e migração a partir do `jflp.db` atual.
- Como o worker único atual (`sbfl/queue.py`, `WorkerLock`) vira N workers; reaproveitar o máximo de `execute.py`,
  `run_maven.py`, `run_jaguar1.py`, `run_jaguar2.py`, `diagnose.py`, `autosearch.py`.
- Concorrência no `m2` compartilhado entre workers (um `m2` só costuma funcionar; se der corrupção, separar).
- O que fazer com os lotes/Resultados atuais: a tela Resultados pode passar a listar só execuções, ou continuar como está.
- CLI: comandos equivalentes (`jflp cobertura iniciar|pausar|continuar|status|importar|sincronizar`) são desejáveis, não
  obrigatórios.
- Estimativa de tempo restante (média móvel é suficiente).

## 6. Como saber que deu certo
- Piloto: cobertura com 4 workers por ~1 h em amostra de todos os projetos → planilha recebe as linhas, painel coerente,
  pausar/parar/continuar/fechar-e-reabrir funcionam sem perder nem duplicar.
- Importação: os ~140 bugs do pipeline PowerShell aparecem no app e na planilha, marcados como importados.
- Os testes `unittest` existentes continuam passando; testes novos cobrem estado da cobertura, derivação da situação,
  pausa/retomada, upsert/caixa de saída (cliente falso) e importação (fixtures pequenas).
- Implementação: o usuário pediu que o código seja escrito por **Sonnet e Haiku** (subagentes), não pelo modelo principal.
