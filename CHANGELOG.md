# Changelog

Todas as mudanças relevantes deste projeto estão aqui. O formato segue o [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e as versões seguem o [Versionamento Semântico](https://semver.org/lang/pt-BR/).

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
