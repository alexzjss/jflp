# JFLP Studio — UI para rodar Maven e Jaguar (2) puros, com segurança

Data: 2026-10-20 · Status: rascunho para aprovação

## 1. Intenção e porquê

O autor trabalha numa IC (PUB-USP, grupo SAEG/EACH) cujo objetivo é **gerar dados de
cobertura de linhas (fluxo de controle) com o Jaguar 2 para o maior número possível de
versões defeituosas** de benchmarks Java (Defects4J, Bugs.jar, ...). O indicador de sucesso
da IC é *quantas versões terminam com a coleta bem-sucedida*, e os dados brutos devem
poder ser disponibilizados a outros pesquisadores.

A dor real hoje: problemas de dependência, compatibilidade de JDK/Maven/plugins e erros
antes e durante a execução do Maven e, principalmente, do Jaguar. O artigo
*"Executing, troubleshooting and analysing the Jaguar2 SBFL tool with the Defects4J
benchmark"* (Bonin & Chaim, 2025) documenta exatamente esses problemas e as correções
manuais (animal-sniffer forçando Java 7, Surefire 2.x incompatível, compiler-plugin antigo
ou novo demais, testes fora de `src/test/java`, POM pai com versões incompatíveis, classes
duplicadas).

O que se quer: **rodar Maven puro e Jaguar puro de forma fácil, automática, segura e
reprodutível**, guardar os resultados brutos e poder exibi-los e compará-los, por meio de
uma interface gráfica que funcione em Windows e Arch Linux.

## 2. Objetivos

1. UI local (Windows e Arch Linux) para: escolher bug → preparar → rodar Maven puro → rodar
   Jaguar 2 (principal) ou Jaguar 1 (compatibilidade) → ver resultados → comparar.
2. Ambiente sem dor: detectar JDK/Maven/Git instalados; se faltar, **baixar versões fixas**
   para uma pasta da própria ferramenta, sem mexer no sistema (sem alterar PATH global,
   `~/.bashrc`, registro etc.).
3. Jaguar 2 via **POM gerado separado** (`jaguar2-pom.xml`), nunca alterando o `pom.xml`
   original, com correções conhecidas aplicadas automaticamente e revisáveis (diff +
   liga/desliga por correção) antes de rodar.
4. **Distinguir falha de teste de falha de infraestrutura.** Testes que falham por causa do
   bug são o resultado esperado: Maven e Jaguar devem rodar até o fim e reportar seus
   próprios resultados. Só falhas de infraestrutura (build, JDK, plugin, crash do Jaguar,
   ausência de saída) disparam diagnóstico/retry.
5. Em falha de infraestrutura: diagnóstico em português, botão "tentar de novo com a
   correção sugerida", **receitas por projeto/bug** reaproveitáveis, e **busca automática**
   limitada de combinações de versões.
6. Cada execução vira uma **pasta imutável** com: logs completos, saídas puras, ambiente
   exato e cópia do checkout.
7. Comparações: Maven × Jaguar (mesmo bug), **Jaguar 1 × Jaguar 2** (mesmo bug) e
   **resumo de lote** (taxa de sucesso por projeto — o indicador da IC).
8. Execução em lote básica: selecionar vários bugs, fila sequencial, retomável após fechar
   o app, painel de sucesso/falha.

## 3. Não-objetivos (v1)

- Cálculo próprio de fórmulas SBFL, EXAM, Top-N, agregação com pandas/matplotlib (o
  JFLP atual faz isso; sai do escopo e do código — ver §9).
- Benchmarks Bears e GitBug-Java na UI (adaptadores podem continuar existindo no código,
  mas não são testados nem expostos na v1).
- Gradle/Ant para o Jaguar 2 (o Jaguar 2 roda como listener do Surefire → exige Maven).
  Projetos sem `pom.xml` (ex.: alguns do Defects4J como Closure/Chart) aparecem como
  "não suportado pelo Jaguar 2" e podem usar o Jaguar 1.
- Ba-Dua (provider de fluxo de dados do Jaguar 2) — fora do escopo, como no artigo.
- Paralelismo no lote, multiusuário, login, servidor remoto.
- Comparar execuções variando JDK/Maven, ou duas execuções arbitrárias (não pedido).

## 4. Anti-objetivos (seria fracasso mesmo "funcionando")

- Tratar testes falhando (o bug!) como erro e "consertar"/excluir testes para ficar verde.
  O Jaguar 2 precisa justamente dos testes que falham.
- Alterar o `pom.xml` original, o `~/.m2` do usuário, variáveis de ambiente globais ou
  instalar coisas no sistema sem o usuário ver.
- Resultados que não dá para reproduzir: toda execução deve registrar o comando, o POM
  usado e as versões exatas.
- Esconder a saída bruta: a UI resume, mas o log e os arquivos originais estão sempre a um
  clique.
- Busca automática que roda indefinidamente ou apaga resultados anteriores.

## 5. Restrições

- Usuário único (o autor), máquina local. Windows 11 e Arch Linux.
- Interface em português.
- Núcleo em Python 3.11+ (reaproveita o pacote `sbfl` existente).
- Funciona offline depois que ferramentas/dependências foram baixadas uma vez.
- Defects4J exige Perl/Linux: no Windows, usar **WSL automaticamente** para os comandos
  `defects4j` (checkout/export); Maven/Jaguar rodam no Windows nativo sobre o checkout.
- Caminhos curtos no Windows (limite de 260 caracteres): workdir padrão curto.

## 6. Decisões tomadas pelo usuário

| Tema | Decisão |
|---|---|
| Foco | Jaguar 2 é o principal; Jaguar 1 é compatibilidade e testes iniciais |
| Usuários | Só o autor |
| JDK/Maven | Detectar o instalado; se faltar, baixar versões fixas para pasta da ferramenta |
| Benchmarks v1 | Defects4J, Bugs.jar, pasta/repo local (pasta no disco ou URL Git + commit) |
| Defects4J no Windows | Via WSL automático |
| Adaptação do POM | Automática + revisão (diff, liga/desliga cada correção) |
| Em falhas de infra | Diagnóstico PT, retry com correção, receitas por projeto, busca automática |
| Falhas de teste | Não são erro: deixar Maven/Jaguar rodarem e mostrar o resultado deles |
| Comparações | Maven × Jaguar; Jaguar 1 × Jaguar 2; resumo de lote |
| Lote | Sim, básico (sequencial, retomável) |
| `~/.m2` | Repositório Maven próprio da ferramenta, compartilhado entre execuções |
| Artefatos | Logs completos, saídas puras, ambiente exato **e** checkout inteiro |
| Idioma | Português |
| Tecnologia da UI | Deixada ao builder (ver §8.1 — proposta) |
| CLI/código atual | Manter só o necessário (ver §9) |
| Escopo de testes | Padrão = **só testes relevantes**; suíte inteira é opção por execução/lote (ver §8.4a) |
| Dados para publicar | Pastas de execução + botão "exportar dataset" com layout fixo e manifest (ver §8.11) |

### 6.1 Decisões do builder (tomadas na revisão — conferir)

- **Multi-módulo (Jaguar 2)**: primeiro roda `mvn install -DskipTests` no reactor com os POMs
  **originais** (para o `m2/` próprio), depois gera `jaguar2-pom.xml` **só no módulo que
  contém os testes do bug** e roda `mvn -f <módulo>/jaguar2-pom.xml clean test`. O
  `<parent>` continua resolvendo pelo `pom.xml` original do pai (via `relativePath`
  padrão), e os módulos irmãos vêm do `m2/`. Assim nenhum POM do pai/irmãos é reescrito.
  O módulo é o mesmo conceito que `sbfl/build.py` já usa (`module`).
- **Scores**: nenhuma fórmula é calculada pela ferramenta. O score por linha é o que o
  próprio Jaguar grava (Jaguar 1: `suspicious-value` no XML, como `compare.py` já lê;
  Jaguar 2: o score dos relatórios CSV/XML, que o artigo mostra). O parser do XML de
  `report.py` migra para o módulo de comparação; o resto de `report.py` sai. Se o Jaguar 1
  reportar em granularidade diferente de linha, a comparação J1×J2 casa por
  (classe, linha) e lista o que não casar.
- **Checkout limpo por execução**: cada execução começa de um checkout **intocado**. Após
  a preparação, guarda-se uma cópia limpa do checkout no workdir (`pristine/`); cada
  execução trabalha numa cópia dela. Nada vaza entre execuções (`target/`,
  `jaguar2-pom.xml`).
- **Dados antigos**: sem migração. A nova pasta da ferramenta começa vazia; os
  `work/`/`results/` antigos não são tocados nem apagados. Importação fica para depois.

## 7. Conceitos

- **Bug**: (benchmark, projeto, id). Ex.: `d4j/Jsoup/93`, `bugsjar/maven/MNG-1234`,
  `local/meu-projeto/<commit>`.
- **Preparação**: checkout da versão defeituosa (buggy) num workdir.
- **Execução (run)**: uma ação sobre um bug preparado. Tipos:
  - `maven` — `mvn clean test` puro com o POM original (testes podem falhar).
  - `jaguar2` — `mvn -f jaguar2-pom.xml clean test` com o POM gerado.
  - `jaguar1` — `JaguarRunner` com o agente JaCoCo em tcpserver (código atual em
    `sbfl/jaguar.py`, incluindo o laço de recuperação de crash).
- **Tentativa (attempt)**: uma execução pode ter várias tentativas (retry/busca automática);
  cada tentativa é guardada separadamente dentro da execução.
- **Correção (fix)**: uma transformação nomeada e determinística aplicada ao POM gerado ou
  ao ambiente (ex.: `skip-animal-sniffer`, `surefire=3.5.3`, `compiler-release=11`,
  `jdk=8`, `test-source-dir=src/test`).
- **Receita**: conjunto de correções + overrides que funcionou para um projeto (e
  opcionalmente faixa de bugs). Reaplicada automaticamente nas próximas execuções.

## 8. O como

### 8.1 Arquitetura e tecnologia da UI (proposta do builder — escolha delegada)

- **Núcleo** (`sbfl/` reorganizado): biblioteca Python sem UI — ambiente, preparação,
  execução, armazenamento, diagnóstico, comparação. Toda ação da UI é também um comando de
  CLI (headless), útil para depurar e para scripts.
- **UI**: app web local — servidor **FastAPI + Uvicorn** em `127.0.0.1` (porta livre
  aleatória, sem expor na rede), páginas HTML server-side (Jinja2) com **htmx** e um pouco de
  JS, *vendorizados* (sem CDN, funciona offline). Logs ao vivo via Server-Sent Events.
  Abre no navegador padrão; opcionalmente numa janela própria com `pywebview`.
  Motivo: roda igual em Windows e Arch, logs em tempo real fáceis, sem build de frontend,
  e reaproveita Python.
- **Processo de trabalho**: um worker em thread de fundo executa a fila de jobs (um por
  vez). O estado da fila é persistido em disco (SQLite em `data/jflp.db`) para retomar após
  fechar o app. A UI só lê estado e envia comandos.
- **Empacotamento** (usuário único): `pyproject.toml` com entry point `jflp`; `jflp ui`
  sobe a interface. Scripts de conveniência: `jflp.ps1`/`jflp.cmd` (Windows) e `jflp.sh`
  (Arch) que criam o venv e instalam dependências na primeira vez. PKGBUILD/instalador
  `.exe` ficam para depois.

### 8.2 Gerenciador de ferramentas (toolchain)

Pasta da ferramenta (padrão `~/.jflp/` no Linux, `%LOCALAPPDATA%\jflp\` no Windows):
`tools/` (JDKs, Mavens, Jaguar 1), `m2/` (repositório Maven compartilhado), `work/`
(checkouts), `runs/` (resultados), `recipes/`, `jflp.db`, `config.toml`.

- **Detecção**: procura JDKs instalados (PATH, `JAVA_HOME`, `/usr/lib/jvm/*` no Arch,
  `C:\Program Files\{Java,Eclipse Adoptium,Zulu,...}\*` e registro no Windows), lê a
  versão (`java -version`). Idem Maven (`mvn -v`) e Git.
- **Download** (com confirmação na UI mostrando URL e tamanho): Eclipse Temurin JDK 8, 11
  e 17 via API do Adoptium; Apache Maven 3.6.3 e 3.9.9+ do repositório oficial Apache
  (com verificação SHA-512). Extrai em `tools/` com nome versionado. Nunca altera PATH
  global: o ambiente é montado só para o subprocesso.
- **Jaguar 2**: não precisa download manual — vem do Maven Central como dependência do POM
  gerado (versão padrão `0.0.2`, configurável). O gerenciador faz um *pre-fetch* opcional
  para o `m2/` próprio (para funcionar offline depois).
- **Jaguar 1**: aceitar uma pasta `lib/` existente (como hoje) **ou** baixar/compilar a
  partir de `github.com/saeg/jaguar` (o builder define o procedimento — ver §11).
- **Defects4J**: no Linux usa a instalação apontada; no Windows detecta WSL + a instalação
  do Defects4J dentro do WSL. Se ausente, a UI mostra um assistente com os passos (não
  instala Perl/cpanm automaticamente na v1).
- Página **"Ambiente"** = `doctor` atual em formato visual: cada item OK/aviso/erro, com
  botão "baixar"/"apontar pasta" e explicação em português.
- **Perfis de ambiente**: Jaguar 2 → JDK 11 + Maven 3.9.9 (padrão do artigo);
  Jaguar 1 → JDK 8 + Maven 3.6.3; Maven puro → JDK escolhido pelo nível do `pom.xml`
  (lógica atual de `sbfl/jdk.py`), com override manual.

### 8.3 Preparação de bugs

- **Defects4J**: `defects4j checkout -p P -v Nb -w <work>` (via WSL no Windows, com
  conversão de caminhos `/mnt/<drive>/...`). Lista de bugs via `defects4j bids`.
- **Bugs.jar**: adaptador atual (git worktree a partir do clone por projeto; `fetch` clona
  o que falta).
- **Local**: (a) pasta já no disco (é **copiada** para o workdir; o original nunca é
  tocado), ou (b) URL Git + commit (clone raso no workdir).
- O checkout fica no workdir enquanto houver execuções pendentes para aquele bug; ao final
  de cada execução, o checkout (com `target/`) é arquivado na pasta da execução (§8.6).

### 8.4 Execução Maven pura

`mvn -B clean test -Dmaven.test.failure.ignore=true -DfailIfNoTests=false
-Dmaven.repo.local=<m2 próprio> [-f <pom>] [flags de skip configuráveis]`.

- Timeout configurável, kill de árvore de processos (reaproveita `sbfl/proc.py`).
- Sucesso de infraestrutura = build compilou e existem relatórios do Surefire. O resultado
  dos testes (passou/falhou/erro/skip) é **dado**, não status de erro.
- Saídas guardadas: `target/surefire-reports/` inteiro, log completo.

### 8.4a Escopo de testes

Padrão: **só testes relevantes**, aplicado igualmente a Maven puro, Jaguar 2 e Jaguar 1
(para que as comparações sejam justas).

- Defects4J: `defects4j export -p tests.relevant` (classes).
- Bugs.jar: classes de teste ligadas ao fix (escopo `changed` atual do adaptador).
- Local: sem informação de relevância → suíte inteira, a menos que o usuário informe uma
  lista de classes.
- Maven/Jaguar 2: passado ao Surefire via `-Dtest=<classes separadas por vírgula>
  -Dsurefire.failIfNoSpecifiedTests=false`; listas muito longas vão num arquivo de
  includes do Surefire em vez da linha de comando (limite de linha no Windows).
- Jaguar 1: a lista vai no `-tf` (como hoje).
- O escopo usado (e a lista de classes) é registrado em `run.json`. "Suíte inteira" é uma
  opção por execução e por lote.

### 8.5 Execução Jaguar 2 (principal)

1. Lê o `pom.xml` original e gera `jaguar2-pom.xml` **ao lado** dele (mesmo diretório, para
   que caminhos relativos e `<parent>` resolvam igual), aplicando as correções ativas:
   - **Base (sempre)**: dependências `br.usp.each.saeg:jaguar2-junit4`,
     `jaguar2-jacoco-provider`, `jaguar2-core`, `jaguar2-xml-exporter` e
     `jaguar2-csv-exporter` (escopo `test`; ambos os exportadores por padrão);
     `jacoco-maven-plugin` 0.8.7 com `prepare-agent`; `maven-surefire-plugin` 3.5.3
     configurado com o listener `JaguarJUnitRunListener` do módulo `jaguar2-junit4` (FQN
     exato a confirmar no jar — §11), preservando `@{argLine}` para não perder o agente do
     JaCoCo quando o POM já define `argLine`.
   - **Correções conhecidas (do artigo), ligáveis**: pular animal-sniffer (via plugin com
     `<skip>true</skip>` no POM filho, cobrindo o caso herdado do pai); fixar
     `maven-compiler-plugin` 3.8.0 com source/target 11 e encoding UTF-8; versões
     alternativas de compiler (rollback para família Jackson); apontar
     `<testSourceDirectory>` para o diretório real dos testes (ex.: `src/test`) **em vez de
     mover arquivos**; redeclarar no filho dependência/plugin herdado com versão
     incompatível; JUnit 4.12 se ausente.
   - Detecção automática de quais correções se aplicam (ex.: animal-sniffer presente no POM
     efetivo; testes em `src/test/org/...`; Surefire < 3; projeto sem JUnit 4 →
     aviso "Jaguar 2 só roda testes JUnit 4").
2. **Tela de revisão**: diff `pom.xml` → `jaguar2-pom.xml`, lista de correções com
   liga/desliga e motivo de cada uma, receita aplicada (se houver). Botão "Rodar".
   No lote, a revisão é pulada (usa o que foi detectado + receitas).
3. Roda `mvn -B -f jaguar2-pom.xml clean test -Dmaven.test.failure.ignore=true ...` com o
   perfil de ambiente do Jaguar 2.
4. Coleta as saídas: relatório XML/CSV do Jaguar 2 (local exato a confirmar — §11),
   `jacoco.exec`, `surefire-reports/`.
5. Sucesso = existe relatório do Jaguar 2 não vazio. Testes falhando são esperados e
   exibidos como dado.
6. **Classes duplicadas** (problema do artigo §5.3): a ferramenta só **detecta e mostra**
   as duplicatas; nunca apaga código automaticamente. Uma correção opcional pode excluir o
   arquivo duplicado da compilação via configuração do compiler, só com confirmação.

### 8.6 Armazenamento imutável de execuções

```
runs/<benchmark>/<projeto>/<bug>/<AAAAMMDD-HHMMSS>-<tipo>/
  run.json            # status, tipo, timestamps, duração, resumo dos testes, attempts
  env.json            # SO, versões exatas de JDK/Maven/Git/plugins, Jaguar, commit do bug
  command.txt         # comando exato + variáveis de ambiente relevantes (sem segredos)
  pom/                # jaguar2-pom.xml usado, pom.diff, correções e receita aplicadas
  attempts/<n>/       # por tentativa: log.txt.gz, correções, diagnóstico
  outputs/            # saídas puras: jaguar2 xml/csv, jacoco.exec, surefire-reports/, jaguar1 xml
  checkout.tar.zst    # cópia do checkout inteiro (com target/), compactado
```

- Escrita atômica: grava em `.<nome>.partial/` e renomeia ao final; depois marca os
  arquivos como somente-leitura. Execuções nunca são sobrescritas — rodar de novo cria uma
  nova pasta.
- O índice das execuções (para listar/filtrar rápido) fica no SQLite, mas a **fonte da
  verdade é a pasta** (o índice pode ser reconstruído varrendo `runs/`).
- Checkout inteiro: ligado por padrão (decisão do usuário), com aviso de uso de disco na
  UI e opção de desligar por execução/lote. Compactação zstd se disponível, senão zip.
- Botão "abrir pasta" e "exportar" (zip da execução) em cada execução.

### 8.7 Diagnóstico, retry, receitas e busca automática

- **Classificação de status** (por tentativa): `ok`, `ok_testes_falhando` (sucesso de
  infra, com falhas de teste), e falhas de infra: `checkout_falhou`, `jdk_incompativel`,
  `dependencia_nao_resolvida`, `plugin_incompativel` (surefire/compiler/animal-sniffer/
  enforcer), `compilacao_falhou`, `classes_duplicadas`, `testes_nao_encontrados`,
  `jaguar_sem_saida`, `jaguar_crash`, `timeout`, `desconhecido`.
- **Diagnóstico**: regras (regex sobre o log + inspeção do POM efetivo) que mapeiam
  sintomas → causa provável → correção sugerida, com texto em português e o trecho do log
  que disparou a regra. Reaproveita/expande `sbfl/triage.py` e `sbfl/envcheck.py`.
- **Retry com correção**: botão que cria uma nova tentativa com a correção sugerida ligada.
- **Receitas**: ao terminar uma execução com sucesso após ajustes, a UI oferece "salvar
  como receita para <projeto> (todos os bugs | faixa | só este bug)". Arquivos TOML em
  `recipes/<benchmark>/<projeto>.toml`, legíveis e versionáveis (podem ser publicados junto
  com os dados da IC). Prioridade: bug > faixa > projeto > padrão.
- **Busca automática**: só para falhas de infra; percorre uma lista **ordenada e limitada**
  de variações (ex.: correções sugeridas pelo diagnóstico primeiro; depois versões de
  Surefire 3.5.3 → 3.2.5 → 3.0.0-M5; compiler 3.8.0 → 3.1 → 3.11.0; JDK 11 → 17 → 8),
  máximo configurável de tentativas (padrão 6), parando no primeiro sucesso. Cada tentativa
  fica registrada. Nunca altera testes nem código-fonte.

### 8.8 Exibir resultados

- **Tela do bug**: execuções do bug (linha do tempo), status, duração.
- **Tela da execução**: resumo dos testes (passou/falhou/erro/skip, lista de falhas com
  mensagem), log ao vivo/completo com busca, saídas puras navegáveis; para Jaguar: tabela
  do relatório (classe, linha, cef/cep/cnf/cnp, score) ordenável/filtrável, e as linhas do
  defeito destacadas (ground truth do diff buggy×fixed, quando disponível).
- **Tela de ambiente** (§8.2) e **tela de receitas**.

### 8.9 Comparações

- **Maven × Jaguar** (mesmo bug): reaproveita `sbfl/compare.py` — testes só num lado,
  resultado diferente entre os dois, contagens; posição das linhas do defeito no relatório
  do Jaguar.
- **Jaguar 1 × Jaguar 2** (mesmo bug): testes executados em cada um; linhas cobertas só num
  lado; diferença de cef/cep/cnf/cnp e de score por linha; posição das linhas do defeito em
  cada um.
- **Resumo de lote**: por benchmark/projeto, nº de bugs tentados, `ok`,
  `ok_testes_falhando`, falhas por categoria, taxa de sucesso (o indicador da IC), tempo
  total; exporta CSV.

### 8.10 Lote

Selecionar bugs (por projeto, faixa, lista, "só os que ainda não têm execução ok"),
escolher tipo(s) de execução, enfileirar. Fila sequencial persistida; ao reabrir o app,
pergunta se retoma. Pausar/cancelar. Mantém a máquina acordada durante o lote (reaproveita
`sbfl/power.py`). Painel com progresso, ETA e contagem por status.

### 8.11 Exportar dataset

Botão/comando "exportar dataset" que, para uma seleção (benchmark/projetos/lote), pega a
**última execução Jaguar 2 com status ok/ok_testes_falhando** de cada bug e monta:

```
dataset/<nome>/
  README.md            # o que é, versões de ferramentas, como foi gerado, schema
  manifest.csv         # benchmark, projeto, bug, status, escopo de testes, nº testes,
                       # nº falhando, JDK, Maven, versão Jaguar 2, receita, caminho dos arquivos
  manifest.json        # o mesmo, estruturado
  <benchmark>/<projeto>/<bug>/  # saídas puras do Jaguar 2 (csv/xml) + surefire-reports + env.json
```

Bugs sem execução ok entram no manifest com o status da última tentativa (para a taxa de
sucesso ficar explícita). Schema versionado (`schema_version` no manifest).

### 8.12 Segurança

- Servidor só em `127.0.0.1`, com token aleatório na URL de abertura (evita outra página do
  navegador disparar comandos locais).
- Nada roda com shell: comandos como listas de argumentos.
- Downloads só de fontes fixas (Adoptium, Apache, Maven Central, GitHub saeg), com checksum
  quando a fonte publica; cada download pede confirmação mostrando URL e tamanho.
- Nunca escreve fora da pasta da ferramenta, exceto o `jaguar2-pom.xml` dentro do checkout
  (que já é uma cópia no workdir).

## 9. O que muda no código atual ("manter só o necessário")

Escolha do builder, a confirmar na revisão:

- **Fica** (reaproveitado pelo núcleo): `proc.py`, `jdk.py`, `build.py` (parte Maven),
  `jaguar.py` (Jaguar 1), `compare.py`, `classfile.py`, `gitutil.py`, `envcheck.py`,
  `envinfo.py`, `triage.py`, `power.py`, `selftest.py` + `selftest_data/`, `diffgt.py`
  (ground truth para destacar linhas do defeito), adaptadores `defects4j.py`, `bugsjar.py`,
  `manifest.py`/`gitpair.py` (base do "repo local").
- **Sai**: `report.py` (fórmulas/EXAM — exceto o parser do XML do Jaguar, que migra para o
  módulo de comparação), `aggregate.py`, `campaign.py` (substituído pelo lote novo),
  `truth.py` se não usado, comandos `analyze`/`aggregate`/`run` antigos, dependências
  pandas/matplotlib. `bears.py`/`gitbugjava.py` ficam fora da UI (podem ser removidos).
- A CLI passa a refletir as ações da UI: `jflp ui`, `jflp doctor`, `jflp tools`,
  `jflp prepare`, `jflp run maven|jaguar2|jaguar1`, `jflp compare`, `jflp batch`,
  `jflp selftest`.
- Testes existentes (`tests/test_core.py`) são mantidos para o que fica e adaptados.

## 10. Deixado para o builder

- Estrutura interna de módulos, nomes de classes, schema do SQLite.
- Layout visual das páginas (sóbrio, funcional, claro/escuro).
- Formato exato das regras de diagnóstico e das receitas (desde que TOML legível).
- Escolha zstd vs zip e como lidar com ausência de `zstandard`.
- Como manipular XML de POM preservando comentários/formatação o suficiente para o diff
  ser legível.

## 11. Pontos a verificar no início da implementação (fatos técnicos, não decisões)

- FQN exato do listener (`JaguarJUnitRunListener`) e configuração do Surefire esperada pelo
  Jaguar 2 0.0.2 (propriedade `listener`), local e nome dos arquivos de saída (XML/CSV) —
  verificar nos jars do Maven Central / `github.com/saeg/jaguar2` e reproduzir o Jsoup 93b
  do artigo como teste de aceitação.
- Procedimento para obter o Jaguar 1 (`github.com/saeg/jaguar`) já compilado com `lib/`.
- Comportamento do Defects4J via WSL escrevendo em `/mnt/<drive>/` (permissões, fim de
  linha, desempenho) e quais projetos do D4J têm `pom.xml` no checkout.

## 12. Critérios de aceitação

1. Em Windows e Arch, numa máquina sem JDK 11/Maven 3.9: a tela de Ambiente baixa e
   configura o necessário, sem alterar o sistema.
2. Jsoup 93b (Defects4J) roda com Jaguar 2 pela UI e produz o relatório, sem editar POM à
   mão — reproduzindo o artigo.
3. Um bug do Bugs.jar roda com Maven puro, Jaguar 1 e Jaguar 2, e as comparações Maven ×
   Jaguar e Jaguar 1 × Jaguar 2 aparecem.
4. Um bug com testes falhando termina como `ok_testes_falhando`, com as falhas listadas, e
   sem nenhum retry.
5. Uma falha de infra conhecida (ex.: animal-sniffer) é diagnosticada em português e
   resolvida pelo retry/busca automática; a correção pode ser salva como receita e é
   reaplicada no bug seguinte do mesmo projeto.
6. Um lote de ≥10 bugs roda sequencialmente, sobrevive a fechar/reabrir o app e mostra o
   resumo de taxa de sucesso exportável em CSV.
7. Toda execução tem a pasta imutável completa (§8.6) e nunca é sobrescrita.
8. Um módulo de projeto multi-módulo do Bugs.jar roda com Jaguar 2 sem reescrever POMs do
   pai/irmãos.
9. "Exportar dataset" gera a pasta com `manifest.csv`/`.json` e README para um lote.
