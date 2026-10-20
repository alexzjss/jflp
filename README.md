# JFLP Studio

Ferramenta local (Windows e Arch Linux) para rodar **Maven puro**, **Jaguar 2** (principal) e **Jaguar 1** (compatibilidade) sobre versões defeituosas de projetos Java, guardar os **resultados brutos** de cada execução e compará-los. Serve para gerar, em escala, dados de cobertura de linhas (espectros) com o Jaguar 2 a partir de benchmarks como Defects4J e Bugs.jar.

O que ela faz:

- roda o Maven e o Jaguar 2 "limpos": o Jaguar 2 usa um POM gerado (`jaguar2-pom.xml`) ao lado do original, que nunca é alterado;
- aplica automaticamente, e mostra para revisão, as correções conhecidas para projetos legados (animal-sniffer, compiler, Surefire antigo, testes fora de `src/test/java`, ...);
- distingue **falha de teste** (é o bug, é dado) de **falha de infraestrutura** (build, JDK, plugin, Jaguar), diagnostica a segunda em português e tenta a busca automática de correções;
- grava cada execução numa pasta imutável (logs completos, saídas puras, ambiente exato, checkout);
- compara Maven × Jaguar e Jaguar 1 × Jaguar 2 para o mesmo bug, e resume lotes (taxa de sucesso por projeto);
- roda lotes em fila sequencial, retomável após fechar o programa;
- exporta um dataset (saídas puras do Jaguar 2 + `manifest.csv/json`) pronto para publicar.

A ferramenta **não calcula** fórmulas SBFL nem EXAM: o score de cada linha é o que o próprio Jaguar grava.

Licença: veja [LICENSE](LICENSE).

## Índice

1. [Instalação](#1-instalação)
2. [Primeira execução](#2-primeira-execução)
3. [Defects4J no Windows (WSL)](#3-defects4j-no-windows-wsl)
4. [Configurações (`config.toml`)](#4-configurações-configtoml)
5. [Como o Jaguar 2 é executado](#5-como-o-jaguar-2-é-executado)
6. [Status e diagnóstico](#6-status-e-diagnóstico)
7. [Busca automática e receitas](#7-busca-automática-e-receitas)
8. [Layout da pasta de execução](#8-layout-da-pasta-de-execução)
9. [Layout do dataset exportado](#9-layout-do-dataset-exportado)
10. [Referência da linha de comando](#10-referência-da-linha-de-comando)
11. [Solução de problemas](#11-solução-de-problemas)
12. [Desenvolvimento](#12-desenvolvimento)

## 1. Instalação

Requisitos mínimos: **Python 3.11 ou mais novo** e **git**. JDK e Maven podem ser baixados pela própria ferramenta (tela Ambiente); eles ficam na pasta do JFLP e nada é alterado no sistema (PATH, registro, `~/.bashrc`). O Jaguar 2 vem do Maven Central como dependência do POM gerado.

Os lançadores criam um `.venv` ao lado do script na primeira execução (com o Python ≥ 3.11 encontrado) e rodam `pip install -e .[full]`; se o extra falhar, instalam sem ele. O extra `full` traz `zstandard` (checkout arquivado em `.tar.zst`, senão `.zip`) e `pywebview` (janela própria). Sem argumentos, o lançador executa `jflp ui`. Se uma instalação falhar pela metade ou as dependências mudarem (por exemplo, depois de um `git pull`), apague a pasta `.venv` e rode o lançador de novo para reinstalar tudo.

**Windows** (PowerShell ou cmd):

```powershell
git clone <url-do-repositorio> jflp
cd jflp
.\jflp.ps1            # abre a interface (jflp ui)
.\jflp.ps1 doctor     # qualquer comando da CLI
jflp.cmd selftest     # mesmo lançador, para o cmd ou duplo clique
```

O `jflp.ps1` procura `py -3` (e confere `sys.version_info >= 3.11`), senão `python`. Se o PowerShell bloquear scripts, use `jflp.cmd` (ele chama o PowerShell com `-ExecutionPolicy Bypass`).

**Arch Linux:**

```bash
sudo pacman -S python git
git clone <url-do-repositorio> jflp
cd jflp
./jflp.sh             # abre a interface (jflp ui)
./jflp.sh doctor
```

> O `jflp.sh` foi escrito junto com o resto, mas só teve a sintaxe conferida (`bash -n`); a execução em Arch ainda não foi verificada.

**Pasta do JFLP** (`tools/`, `m2/`, `work/`, `runs/`, `recipes/`, `datasets/`, `jflp.db`, `config.toml`):

- Windows: `%LOCALAPPDATA%\jflp`
- Linux: `~/.jflp`
- para mudar: variável de ambiente `JFLP_HOME`. No Windows prefira um caminho **curto** (ex.: `C:\jflp`) por causa do limite de 260 caracteres.

## 2. Primeira execução

```text
.\jflp.ps1 ui        # ou ./jflp.sh ui
```

O servidor sobe em `127.0.0.1` (porta livre, ou `--port`) e abre o navegador; `--no-browser` não abre, `--window` usa uma janela própria (pywebview). A barra lateral tem: Painel, Ambiente, Bugs, Lotes, Comparar, Receitas e Exportar.

1. **Ambiente.** Mostra JDKs, Mavens, Git, WSL/Defects4J e o perfil resolvido de cada tipo de execução. Os botões baixam, com confirmação de URL e tamanho, o **JDK 11** e o **Maven 3.9.9** (perfil do Jaguar 2). Também há o *pre-fetch* do Jaguar 2 para o `m2/` próprio, para depois rodar offline. Para o Jaguar 1 são necessários JDK 8 e Maven 3.6.3, e a `lib/` do Jaguar 1 (baixada pela tela ou apontada em `jaguar1.lib`).
2. **Bugs.** Escolha a origem:
   - **Pasta local**: um projeto no disco (é *copiada*; o original nunca é tocado) ou `URL@commit` (clone raso);
   - **Bugs.jar**: configure `bugsjar.root` e `bugsjar.projects` e rode `jflp fetch` (ou use a tela); os bugs são os branches `bugs-dot-jar_*`;
   - **Defects4J**: veja a seção 3.
3. **Revisão do POM** (Jaguar 2). Mostra o diff `pom.xml` → `jaguar2-pom.xml`, a lista de correções com liga/desliga e o motivo de cada uma, e a receita aplicada, se houver. Em lotes a revisão é pulada (valem as correções detectadas e as receitas).
4. **Rodar.** O log aparece ao vivo. Se a execução falhar por infraestrutura, a tela mostra o diagnóstico, o trecho do log e o botão "tentar de novo com a correção sugerida".
5. **Resultado.** Resumo dos testes (passou/falhou/erro/ignorado), tabela do relatório do Jaguar (classe, linha, cef/cnf/cep/cnp, score) com as linhas do defeito destacadas quando há *ground truth*, saídas puras, log completo e botão "abrir pasta".

Para validar a instalação inteira (Maven, Jaguar 2 e, se existir, Jaguar 1) com um projeto de exemplo embutido:

```text
.\jflp.ps1 selftest
```

Ele roda um bug pequeno e confere que `jaguar2.csv` contém a linha defeituosa. Sai com código 0 se tudo passou.

## 3. Defects4J no Windows (WSL)

O Defects4J exige Perl/Linux. No Windows a ferramenta chama os comandos `defects4j` por `wsl -d <distro> -- bash -lc ...` e converte caminhos para `/mnt/<drive>/...`; Maven e Jaguar rodam no Windows nativo sobre o checkout. É preciso:

1. WSL instalado (`wsl --install`) com uma distribuição Linux.
2. O Defects4J instalado **dentro** dessa distribuição (clone, `cpanm --installdeps .`, `./init.sh`; veja a documentação do Defects4J). O comando `defects4j` precisa estar no PATH do shell de login, ou use `defects4j.home`.
3. As configurações (opcionais):
   - `defects4j.home`: pasta de instalação **dentro do WSL** (ex.: `/home/usuario/defects4j`); usada quando `defects4j` não está no PATH;
   - `defects4j.wsl_distro`: nome da distribuição (`wsl -l -q`); vazio = a primeira que tiver o Defects4J.

No Linux, o `defects4j` vem do PATH ou de `defects4j.home` (pasta nativa). A tela Ambiente e `jflp doctor` mostram o estado ("WSL/Defects4J"). Projetos do Defects4J sem `pom.xml` (ex.: Closure, Chart) aparecem como "não suportados pelo Jaguar 2"; use o Jaguar 1.

## 4. Configurações (`config.toml`)

Fica em `<pasta do JFLP>/config.toml` e é criado com os padrões na primeira vez; a tela Ambiente também grava nele. Chaves ausentes usam o padrão abaixo.

| Seção.chave | Padrão | Significado |
|---|---|---|
| `tools.jdks` | `{}` | JDKs manuais: `{ "11" = "C:/Java/jdk-11" }` |
| `tools.mavens` | `{}` | Mavens manuais: `{ "3.9.9" = "C:/tools/maven" }` |
| `jaguar2.version` | `0.0.2` | versão dos artefatos `br.usp.each.saeg:jaguar2-*` |
| `jaguar2.jacoco` | `0.8.7` | `jacoco-maven-plugin` do POM gerado |
| `jaguar2.surefire` | `3.5.3` | `maven-surefire-plugin` do POM gerado |
| `jaguar2.compiler` | `3.8.0` | `maven-compiler-plugin` usado pela correção `compiler-java` |
| `jaguar2.junit` | `4.12` | JUnit adicionado pela correção `junit4` |
| `jaguar1.lib` | `""` | pasta `lib/` do Jaguar 1 (com `jacocoagent.jar`); vazio = `tools/jaguar1/lib` |
| `jaguar1.timeout_s` | `3600` | tempo limite do Jaguar 1 |
| `jaguar1.max_retries` | `6` | recuperações de crash da JVM do Jaguar 1 |
| `jaguar1.jvm_args` | `[]` | argumentos extras da JVM do Jaguar 1 |
| `run.test_scope` | `relevant` | `relevant` (só testes relevantes) ou `all` (suíte inteira) |
| `run.timeout_s` | `7200` | tempo limite de cada processo Maven |
| `run.archive_checkout` | `true` | arquiva o checkout inteiro na pasta da execução (pode ocupar muito disco) |
| `run.max_attempts` | `6` | máximo de tentativas por execução (inclui a primeira) |
| `run.maven_opts` | `""` | opções extras de Maven |
| `run.skip_flags` | lista abaixo | flags `-D...skip=true` passadas ao Maven |
| `bugsjar.root` | `""` | pasta onde ficam os clones do Bugs.jar (um por projeto) |
| `bugsjar.projects` | `[]` | projetos do Bugs.jar usados por `fetch` e pela listagem |
| `bugsjar.url_template` | `https://github.com/bugs-dot-jar/{project}.git` | URL de clone |
| `defects4j.home` | `""` | instalação do Defects4J (dentro do WSL no Windows) |
| `defects4j.wsl_distro` | `""` | distribuição WSL (seção 3) |
| `defects4j.projects` | Lang, Math, Time, Chart, Closure, Mockito, Jsoup | projetos listados na tela Bugs |
| `ui.port` | `0` | porta da interface (0 = automática) |

`run.skip_flags` padrão: `-Drat.skip=true -Drat.numUnapprovedLicenses=999999 -Drat.ignoreErrors=true -Denforcer.skip=true -Dcheckstyle.skip=true -Dlicense.skip=true -Dmaven.javadoc.skip=true -Danimal.sniffer.skip=true -Dfindbugs.skip=true` (desligam verificações de qualidade que atrapalham checkouts antigos).

Exemplo:

```toml
[bugsjar]
root = 'D:\bugsjar'
projects = ["commons-math", "maven"]

[defects4j]
home = "/home/usuario/defects4j"
wsl_distro = "Ubuntu"

[run]
max_attempts = 4
```

O repositório Maven usado pelas execuções é o `m2/` da própria ferramenta (nunca o `~/.m2`).

## 5. Como o Jaguar 2 é executado

O Jaguar 2 roda como *listener* do Surefire, então exige **Maven** (Gradle/Ant não são suportados) e testes **JUnit 4**. Perfil de ambiente: **JDK 11 + Maven ≥ 3.9.9**. (Jaguar 1: JDK 8 + Maven 3.6.3; Maven puro: JDK escolhido pelo nível declarado no `pom.xml`, com override.)

Cada execução parte de uma cópia limpa do checkout (`pristine/` no workdir), então nada vaza entre execuções (`target/`, `jaguar2-pom.xml`).

1. **Reactor (só multi-módulo).** Se o bug está num submódulo, roda antes `mvn install -DskipTests` no reactor com os POMs **originais**, para popular o `m2/` próprio. Depois o `jaguar2-pom.xml` é gerado **só no módulo que contém os testes**, e o `<parent>` continua resolvendo pelo `pom.xml` original do pai. Nenhum POM do pai ou dos módulos irmãos é reescrito.
2. **Geração do `jaguar2-pom.xml`** no diretório do módulo, ao lado do `pom.xml` (para que caminhos relativos e `<parent>` resolvam igual). O `pom.xml` original nunca é modificado. O que é injetado **sempre**:
   - dependências `br.usp.each.saeg:jaguar2-junit4`, `jaguar2-jacoco-provider`, `jaguar2-core`, `jaguar2-xml-exporter` e `jaguar2-csv-exporter` (escopo `test`);
   - `jacoco-maven-plugin` com o goal `prepare-agent` (reaproveitando a execução já herdada do pai, se houver);
   - `maven-surefire-plugin` com a propriedade `listener = br.usp.each.saeg.jaguar2.junit.JaguarJUnitRunListener` (somada a um listener já existente, sem duplicar) e `argLine` preservando `@{argLine}` para não perder o agente do JaCoCo.
3. **Correções** (ligadas pela detecção, ajustáveis na tela de revisão, no CLI com `--fix ID` / `--no-fix ID`):

   | id | Título | Quando é detectada |
   |---|---|---|
   | `skip-animal-sniffer` | Ignorar o animal-sniffer | `animal-sniffer` aparece em algum POM da cadeia (módulo, pais) |
   | `compiler-java` | Compilar com o JDK atual | o projeto declara Java < 8 (ou nada); fixa o `maven-compiler-plugin` (`jaguar2.compiler`) com source/target do JDK escolhido e UTF-8 |
   | `source-dirs` | Ajustar pastas de código-fonte | fontes fora de `src/main/java` ou `src/test/java` (ex.: `src/test`); aponta `sourceDirectory`/`testSourceDirectory` sem mover arquivos |
   | `junit4` | Adicionar JUnit 4 | não há `junit:junit` na cadeia de POMs |
   | `override-version` | Forçar versões | nunca automática; redeclara versões de dependências/plugins (`params`: `deps`, `plugins`); usada pelo diagnóstico e pela busca |
   | `exclude-duplicates` | Excluir classes duplicadas | nunca automática; exclui arquivos duplicados da compilação, só por sugestão do diagnóstico |
   | `skip-rat` | Ignorar o Apache RAT | `apache-rat-plugin` aparece em algum POM da cadeia; define `skip` e `numUnapprovedLicenses=999999` no POM gerado |
   | `unblock-http` | Liberar repositórios http | nunca automática (correção de execução, não do POM); passa `-s <home>/tools/jflp-settings-unblock.xml` a todo `mvn`, cujo mirror anula o bloqueio do Maven ≥ 3.8.1 |

   A ferramenta **detecta e mostra** classes duplicadas; não apaga código por conta própria.
4. **Comando:** `mvn -B -Dmaven.repo.local=<m2> -f jaguar2-pom.xml clean test -Djaguar2.classesDirs=<módulo>/target/classes -Dmaven.test.failure.ignore=true <skip_flags> [-Dtest=...]`.
   - `-Djaguar2.classesDirs` é obrigatório: o Jaguar 2 só analisa as classes listadas nessa propriedade; sem ela o relatório sai vazio.
   - A JVM do Surefire roda com `-Duser.language=en -Duser.country=US` (via `JAVA_TOOL_OPTIONS`, só nessa chamada): sem isso o CSV sai com vírgula decimal em máquinas pt-BR e as colunas quebram.
   - Escopo de testes: padrão **só testes relevantes** (Defects4J: `tests.relevant`; Bugs.jar: testes ligados ao fix; local: suíte inteira). Vão em `-Dtest=` ou, se a lista for longa, num `includesFile` do Surefire. `--all-tests` / `run.test_scope = "all"` roda tudo.
5. **Saídas:** `target/jaguar2.csv`, `jaguar2.xml`, `jacoco.exec` e `surefire-reports/`.
6. **Sucesso** = existe relatório do Jaguar 2 legível. Testes falhando são esperados.

## 6. Status e diagnóstico

**Princípio: testes que falham são dados, não erros.** O Jaguar 2 precisa justamente dos testes que falham. Nenhum teste é excluído ou "consertado" para ficar verde, e uma execução cujo build fechou e produziu relatórios nunca dispara retry por causa de testes falhando.

| Status | Significado | Retry/busca? |
|---|---|---|
| `ok` | infraestrutura OK e todos os testes passaram | não |
| `ok_testes_falhando` | infraestrutura OK; há testes falhando (o esperado num bug) | não |
| `checkout_falhou` | não foi possível obter/preparar o bug | não |
| `jdk_incompativel` | JDK incompatível com o projeto ou com o JaCoCo, ou JDK pedido ausente | sim |
| `dependencia_nao_resolvida` | dependência/plugin não baixado (inclui repositório `http` bloqueado) | sim |
| `plugin_incompativel` | animal-sniffer, Surefire ou compiler incompatível; JVM de teste caiu | sim |
| `compilacao_falhou` | erro de compilação | sim |
| `classes_duplicadas` | classes duplicadas entre diretórios de fontes | sim (com confirmação) |
| `testes_nao_encontrados` | nenhum teste executado | sim |
| `jaguar_sem_saida` | o build terminou mas não há relatório do Jaguar (ou ele é ilegível) | sim |
| `jaguar_crash` | a JVM do Jaguar 1 caiu | sim |
| `timeout` | excedeu o tempo limite | não |
| `interrompido` | a execução foi interrompida antes de terminar (recuperada na abertura) | não |
| `desconhecido` | falha não reconhecida; veja o log | sim |

As regras de diagnóstico (regex sobre o log, só em linhas de erro quando necessário) mapeiam o sintoma para causa em português, o trecho do log que disparou a regra e correções sugeridas. Para o Jaguar 2, se nenhum teste falhou e o relatório está vazio, a execução continua `ok` mas com o aviso "defeito não reproduzido".

## 7. Busca automática e receitas

**Busca automática** (só para falhas de infraestrutura; nunca altera testes nem código-fonte). Depois da 1ª tentativa falhar, tenta, nesta ordem e sem repetir configurações:

1. as sugestões do diagnóstico;
2. (Jaguar 2) Surefire `3.5.3` → `3.2.5` → `3.0.0-M5`;
3. (Jaguar 2) compiler `3.8.0` → `3.1` → `3.11.0`;
4. JDK `11` → `17` → `8`.

Para no primeiro sucesso e respeita `run.max_attempts` (padrão 6, contando a primeira). Cada tentativa é gravada em `attempts/<n>/`. Desligue com `--no-search` (CLI) ou na tela.

**Receitas** guardam o que funcionou e são reaplicadas nas execuções seguintes. A tela de execução oferece "salvar como receita" (todos os bugs, faixa/glob ou só este bug). Ficam em `<pasta do JFLP>/recipes/<benchmark>/<projeto>.toml`, em TOML legível e versionável (podem ser publicadas junto com os dados):

```toml
[[recipe]]
match = "*"                       # "*" = projeto todo; um id exato; um glob ("MNG-5*"); ou uma lista
kind = "jaguar2"                  # maven | jaguar2 | jaguar1 | "*"
fixes = ["skip-animal-sniffer"]   # correções ligadas
params = { override-version = { plugins = { "org.apache.maven.plugins:maven-surefire-plugin" = "3.2.5" } } }
jdk = 11                          # opcional
maven = "3.6.3"                   # opcional
note = "animal-sniffer exige Java 7"
```

Prioridade: **bug (id exato) > glob > projeto (`*`) > padrão**; em empate vale a mais recente do arquivo. Opções dadas na execução (`--fix`, `--no-fix`, `--jdk`, `--maven`) somam-se à receita; `--no-fix` vence a receita. `jflp recipes` lista; `jflp recipes <benchmark>/<projeto>` mostra o arquivo.

## 8. Layout da pasta de execução

Cada execução é uma pasta nova (nunca sobrescrita). Ela é gravada em `.<nome>.partial/`, renomeada ao final e seus arquivos viram somente leitura. A pasta é a fonte da verdade; o índice (`jflp.db`) pode ser reconstruído com `jflp reindex`. Execuções que ficaram pela metade viram `interrompido` quando a fila (interface ou `jflp batch run`) é iniciada.

```text
runs/<benchmark>/<projeto>/<bug>/<AAAAMMDD-HHMMSS>-<tipo>/
  run.json            # status, tipo, tempos, resumo dos testes, diagnóstico, receita, ground truth, tentativas
  env.json            # SO, Python, Git, JDK/Maven exatos, commit do checkout, versão do jflp
  command.txt         # comando exato da última tentativa
  pom/                # jaguar2-pom.xml usado e jaguar2-pom.diff (Jaguar 2)
  attempts/
    0-prepare.log     # log da preparação do bug
    <n>/log.txt.gz    # log completo da tentativa n
    <n>/attempt.json  # configuração, diagnóstico, comando, ambiente e status da tentativa
  outputs/            # saídas puras da última tentativa real
    surefire-reports/ jaguar2.csv jaguar2.xml jacoco.exec   # Maven e Jaguar 2
    jaguar1.xml jaguar1.log.gz                              # Jaguar 1
  checkout.tar.zst    # checkout inteiro (com target/); checkout.zip sem zstandard
```

`jaguar2.csv` **não tem linha de cabeçalho**: colunas `classe,linha,cef,cnf,cep,cnp,susp` (só linhas com `susp > 0`). Para apagar uma execução à mão, remova antes o atributo somente leitura dos arquivos.

## 9. Layout do dataset exportado

`jflp export` (ou a tela Exportar) pega, para cada bug da seleção, a **última execução Jaguar 2 bem-sucedida** (`ok` ou `ok_testes_falhando`) e monta, em `datasets/<nome>/` (o nome não pode já existir):

```text
README.md             # o que é, layout e colunas
manifest.csv
manifest.json         # o mesmo, com schema_version (atual: 1)
<benchmark>/<projeto>/<bug>/
  jaguar2.csv  jaguar2.xml  surefire-reports/  env.json
```

O `jaguar2.csv` exportado também **não tem linha de cabeçalho** (colunas `classe,linha,cef,cnf,cep,cnp,susp`).

Colunas do manifest: `benchmark, project, bug, status, test_scope, n_tests, n_failed, jdk, maven, jaguar2_version, recipe, path`. Bugs sem execução bem-sucedida entram com o status da última tentativa e `path` vazio, para a taxa de sucesso ficar explícita; além dos status da seção 6, o manifest usa `sem_execucao`, `cancelado` e `desconhecido` (job que terminou com erro, sem execução gravada). `jaguar2_version` vem da configuração no momento da exportação.

## 10. Referência da linha de comando

Toda ação da interface tem um comando equivalente. Use `jflp <comando> --help` para ver o uso. Em lançadores: `.\jflp.ps1 <comando>` / `./jflp.sh <comando>`.

Um **bug** é identificado por `benchmark/projeto/id` (`d4j/Jsoup/93`, `bugsjar/maven/bugs-dot-jar_MNG-5742_6ab41ee8`) ou por `local:<pasta>` / `local:<URL>@<commit>`.

| Comando | O que faz |
|---|---|
| `jflp ui [--port N] [--no-browser] [--window]` | abre a interface web |
| `jflp doctor` | JDKs, Mavens, git, WSL/Defects4J e o perfil resolvido de cada tipo; sai com 1 se faltar ferramenta |
| `jflp tools list` | ferramentas encontradas (JDK, Maven, Jaguar 1) |
| `jflp tools get {jdk\|maven\|jaguar1} [valor] [--yes]` | baixa uma ferramenta (`valor` = versão do JDK ou do Maven); pergunta antes, salvo `--yes` |
| `jflp fetch [projeto]` | clona/atualiza os projetos do Bugs.jar (`bugsjar.root`, `bugsjar.projects`) |
| `jflp bugs {d4j\|bugsjar} [projeto]` | lista os bugs de um benchmark |
| `jflp prepare <bug>` | faz o checkout e mostra módulo e linhas defeituosas conhecidas |
| `jflp run {maven\|jaguar2\|jaguar1} <bug> [opções]` | executa; sai com 0 se o status for `ok`/`ok_testes_falhando` |
| `jflp runs [bug]` | lista execuções gravadas |
| `jflp compare <execução-A> <execução-B>` | compara duas execuções (caminhos relativos a `runs/`, como em `jflp runs`) |
| `jflp batch create <nome> <tipos> <bug>...` | cria um lote; `tipos` separados por vírgula (`maven,jaguar2`) |
| `jflp batch run` | roda a fila em primeiro plano até esvaziar (Ctrl+C espera o job atual) |
| `jflp batch status <id>` | situação e resumo por projeto |
| `jflp export <nome> [bug...] [--batch ID]` | exporta o dataset (informe `--batch` **ou** bugs) |
| `jflp recipes [benchmark/projeto]` | lista as receitas, ou mostra a de um projeto |
| `jflp reindex` | reconstrói o índice varrendo `runs/` |
| `jflp selftest` | valida o ambiente com um projeto de exemplo |

Opções de `jflp run`: `--all-tests` (suíte inteira), `--fix ID` e `--no-fix ID` (repetíveis), `--jdk N`, `--maven VERSÃO`, `--no-search` (sem busca automática), `--no-archive` (não arquiva o checkout).

Exemplo completo:

```text
jflp doctor
jflp tools get jdk 11
jflp tools get maven 3.9.9
jflp run jaguar2 local:D:\projetos\meu-projeto
jflp runs
jflp batch create teste maven,jaguar2 bugsjar/maven/bugs-dot-jar_MNG-5742_6ab41ee8
jflp batch run
jflp export meu-dataset --batch 1
```

Variável de ambiente: `JFLP_HOME` muda a pasta do JFLP.

## 11. Solução de problemas

- **"JDK N não encontrado"** (`jdk_incompativel` com causa de ferramenta ausente): baixe o JDK na tela Ambiente (`jflp tools get jdk 11`) ou aponte-o em `tools.jdks`. O Jaguar 2 usa JDK 11; o Jaguar 1, JDK 8.
- **Maven bloqueia repositório `http`** ("Blocked mirror for repositories", `maven-default-http-blocker`): projetos antigos usam repositórios `http://`, que o Maven ≥ 3.8.1 bloqueia. O diagnóstico sugere primeiro a correção **`unblock-http`** (funciona também com o Maven ≥ 3.9.9 que o Jaguar 2 exige) e depois o **Maven 3.6.3** (`--maven 3.6.3`, ou receita com `maven = "3.6.3"`). Baixe-o em `jflp tools get maven 3.6.3`.
- **Apache RAT** ("Too many files with unapproved license"): o checkout do Bugs.jar não leva mais a pasta `.bugs-dot-jar` para a cópia de trabalho e os flags `-Drat.*` estão em `run.skip_flags`; se ainda falhar, a correção `skip-rat` desliga o plugin no POM gerado.
- **animal-sniffer** ("Undefined reference", build quebra por API Java): a correção `skip-animal-sniffer` é detectada e aplicada sozinha. Se foi desligada na revisão, o diagnóstico a sugere de volta.
- **Surefire 2.x** ("maven-surefire-plugin:2.x", "The forked VM terminated"): o Jaguar 2 precisa do Surefire 3. O POM gerado fixa `jaguar2.surefire`; se ainda falhar, a busca tenta 3.2.5 e 3.0.0-M5.
- **Testes fora de `src/test/java`** (ex.: `src/test/org/...`; "Nenhum teste foi executado" ou "package ... does not exist"): a correção `source-dirs` aponta `testSourceDirectory` para a pasta real, sem mover arquivos.
- **Jaguar 2 com relatório vazio**: se nenhum teste falhou, o defeito **não foi reproduzido** (aviso na execução; confira o escopo `relevant` e tente `--all-tests`). Se há testes falhando e o CSV está vazio, confirme que a execução usou `-Djaguar2.classesDirs` (veja `command.txt`) e que o módulo certo foi escolhido.
- **Projeto sem `pom.xml` ou sem JUnit 4**: o Jaguar 2 não se aplica (aparece como "não suportado"); use o Jaguar 1.
- **CSV com colunas quebradas / vírgula decimal**: a ferramenta já força o locale en-US na JVM do Surefire; ao rodar o Maven por fora, passe `JAVA_TOOL_OPTIONS="-Duser.language=en -Duser.country=US"`.
- **WSL / Defects4J ausente** ("WSL/Defects4J: ..." em `doctor`): siga a seção 3 e confira `defects4j.home` e `defects4j.wsl_distro`. Sem WSL, use Bugs.jar ou projetos locais.
- **Windows e caminhos longos** (erros de checkout ou de arquivo não encontrado com caminhos > 260 caracteres): use um `JFLP_HOME` curto (`C:\jflp`), habilite `git config --global core.longpaths true` e, se necessário, os caminhos longos do Windows (`LongPathsEnabled=1` no registro, pede administrador).
- **Pouco espaço em disco**: o checkout arquivado (`run.archive_checkout`) e os `work/` são os maiores consumidores; use `--no-archive` ou desligue a opção.
- **Falha ao instalar com `[full]`** (pywebview/zstandard sem wheel): o lançador instala sem o extra; `--window` não funcionará, e o checkout sai em `.zip`.
- **A interface não abre**: rode `jflp ui --no-browser` e abra o endereço mostrado; a porta fixa é `--port` ou `ui.port`.

## 12. Desenvolvimento

```text
python -m venv .venv
.venv\Scripts\activate          # Linux: source .venv/bin/activate
pip install -e .[dev]           # [dev] traz httpx, usado nos testes da interface
python -m unittest discover -s tests -v
```

Os testes usam `unittest` (não pytest) e Maven/Java falsos; a integração real é validada por `jflp selftest` e pelos casos de aceitação. O pacote é `sbfl/` (comando `jflp` → `sbfl.cli:main`); a interface web (FastAPI + Jinja2 + htmx vendorizado, sem CDN) está em `sbfl/web/`. Dependências de execução: `fastapi`, `uvicorn`, `jinja2`, `python-multipart`; extra `full`: `zstandard`, `pywebview`.

Segurança: o servidor escuta só em `127.0.0.1` e nenhum comando roda com shell (listas de argumentos). Downloads só de fontes fixas (Adoptium, Apache, Maven Central, GitHub saeg) e sempre com confirmação.
