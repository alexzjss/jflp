# Changelog

Todas as mudanças relevantes deste projeto estão aqui. O formato segue o [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e as versões seguem o [Versionamento Semântico](https://semver.org/lang/pt-BR/).

## [Não lançado]

### Adicionado

- **Correções para famílias de falha do Jaguar 2**, sugeridas pelo diagnóstico e aplicadas pela busca automática: `javac7` (compila com o javac do JDK 7 e mantém Maven/Surefire no JDK 8; Wicket, Camel, Flink), `asm-shadow` (ASM antigo do `cglib-full` sobrepondo o do JaCoCo; log4j2), `scr-jdk7` (reactor no JDK 7 + Maven 3.6.3 para o `maven-scr-plugin`; Oak), `rat-flags` (RAT 0.10), `exec-skip` (`exec-maven-plugin` rodando `.sh` no Windows) e `sortpom-warn` (sortpom reprovando o `jaguar2-pom.xml`; Accumulo). Ver README, seção 7.
- **Flags extras do reactor** (`params["reactor_flags"]` e `params[<correção>]["reactor_flags"]`): vão para o `mvn install -pl M -am` e para o `mvn -f jaguar2-pom.xml`, depois do `run.skip_flags`.
- O diagnóstico passa a reconhecer: ASM antigo na JVM do Surefire (`jaguar_sem_saida` com causa), `jdk.tools`/`lib/tools.jar` (pede JDK 8), pacotes Java EE ausentes no JDK 11 e `Source option 5 is no longer supported` (agora `jdk_incompativel`, antes escondido atrás do `COMPILATION ERROR` genérico).

### Corrigido

- **Linux:** `make_writable` e o `_rmtree` tiravam o bit de execução das pastas (`chmod 0o600`), deixando-as inacessíveis: o export do dataset perdia os arquivos e o cancelamento de um job deixava a execução parcial no disco. Agora só acrescentam permissões.
- **Linux:** o botão "Escolher pasta…" desaparecia quando o `libtk` não estava instalado; agora há fallback para `zenity`/`kdialog`.
- `redirected_path` passa a tratar o caminho como caminho do Windows em qualquer sistema; o teste de navegador do Windows só roda no Windows; `[dev]` inclui `httpx2` (exigido pelo Starlette recente).
- Um JDK 7 configurado em `[tools] jdks` não é mais usado para sondar a versão dos Mavens (o Maven 3.9 não roda em Java 7 e o Maven era dado como ausente).

## [0.3.0] - 2026-10-31

### Adicionado

- **Cobertura do bugs-dot-jar:** um lote especial que roda Maven, Jaguar 1 e Jaguar 2 em todos os bugs da pasta do Bugs.jar (`jflp cobertura sincronizar`, `status`, `refazer`), sem duplicar ao sincronizar de novo.
- **Vários workers em paralelo** (`WorkerPool`), ajustáveis ao vivo no Painel, sem dois jobs do mesmo bug ao mesmo tempo. Na linha de comando: `jflp batch run --workers N`.
- **Pausar, continuar e parar** a cobertura; retomada automática depois de fechar o programa.
- **Perfis legados** (JDK 7 + Maven 3.0.5, inclusive a versão offline) para os bugs Maven antigos, com mirror HTTPS dos repositórios `http`.
- **Importação** dos resultados do pipeline PowerShell antigo (`jflp cobertura importar <pasta>`).
- **Planilha do Google** atualizada sozinha por um app da web do Apps Script (`tools/planilha/Code.gs`), com caixa de saída no SQLite, nova tentativa com espera crescente e reconciliação; `jflp cobertura planilha [--reenviar]`.
- **Telas Painel e Tabela** da cobertura, com detalhe de cada bug, e a configuração da planilha na tela Ambiente.
- **Guarda de espaço livre:** a cobertura pausa abaixo de `cobertura.min_livre_gb` (padrão 5) e mostra o motivo.
- **Pasta de trabalho configurável** (`cobertura.work_dir`, padrão `E:\JFLP\cobertura` quando existe o disco E:).
- **Escolha das etapas** que a cobertura roda (`cobertura.kinds`, "Etapas a rodar" no Painel) e do limite de tentativas dos Jaguar (`cobertura.max_tentativas_jaguar`, padrão 4).
- Desenho e plano da cobertura em `docs/superpowers/` e este roteiro em `docs/ROADMAP.md`.

### Alterado

- A página inicial passa a ser a **Cobertura**. Na barra lateral, a Cobertura toma o lugar do link "Rodar" (a tela de execução avulsa continua em `/rodar`); Resultados e Ambiente seguem.
- Menos tentativas nos Jaguar da cobertura (de 8 para 4): na medição, a maior parte do tempo ia para bugs que não se recuperavam.
- O Jaguar 1 da cobertura roda um job por vez por padrão (`jaguar1.port_aware = false`), porque o jar 0.0.6 não aceita escolher a porta.
- O checkout de cada bug é apagado ao fim do bug, e o banco usa WAL, para a cobertura caber no disco.
- O README foi reorganizado em torno do fluxo novo.

### Corrigido

- Jaguar 1 roda só os testes escolhidos (o `JaguarRunner` ignora `-tf`).
- Bugs sem `pom.xml` viram "não executável" em vez de falha; bug com etapa pendente não conta como parcial.
- Dois cliques em "Começar cobertura" não criam dois lotes.
- Cancelar e interromper no pool chegam ao worker que está com o job; o fim de um job que falha ao gravar é tentado de novo.
- A importação não sobrescreve job em execução e não deixa execução órfã.
- Painel: uma leitura por atualização, selos e chips por situação, aviso de disco e de linhas com erro, refazer atualiza a planilha.
- Planilha: reenvio quando o cabeçalho muda, token gerado mesmo sem URL, proteção contra fórmulas nas células.
- Pré-carregamento offline na raiz do reactor, com aviso quando falha.

## [0.2.0]

Versão anterior: Maven, Jaguar 2 e Jaguar 1 sobre bugs do Bugs.jar, Defects4J e projetos locais, com resultados brutos por execução, busca automática de correções, lotes e exportação do dataset.
