# Do bugs-dot-jar ao JFLP generalista

Este roteiro diz onde o JFLP Studio está e para onde vai. Itens marcados como **proposta** ainda não foram decididos; os tamanhos (P, M, G) são estimativas grosseiras de esforço, e **não há datas**.

## Direção

O MVP é **focado no bugs-dot-jar**: gerar, em escala e de forma retomável, os dados de cobertura **Maven + Jaguar 1 + Jaguar 2** de todos os bugs da coleção (1162 bugs, 8 projetos) e entregá-los numa planilha do Google. Depois que isso estiver sólido, a ferramenta passa a ser **generalista**: qualquer benchmark ou repositório Java, com o mesmo fluxo (sincronizar, rodar com vários workers, pausar e retomar, refazer o que falhou, exportar).

## Fase 1: MVP bugs-dot-jar

### Já feito

- [x] Cobertura do bugs-dot-jar: um lote especial com Maven, Jaguar 1 e Jaguar 2 para cada bug da pasta (`jflp cobertura sincronizar`, que só acrescenta o que falta e não duplica).
- [x] N workers em paralelo, ajustáveis no Painel sem reiniciar (`cobertura.workers`), sem nunca rodar dois jobs do mesmo bug ao mesmo tempo.
- [x] Pausar, continuar e parar; retomada automática depois de fechar o programa (jobs que ficaram pela metade voltam a pendentes).
- [x] Refazer falhados (`jflp cobertura refazer`, botão no Painel e na Tabela).
- [x] Importação dos resultados do pipeline PowerShell antigo (`jflp cobertura importar <pasta>`), com os bugs marcados como importados.
- [x] Perfis legados (JDK 7 + Maven 3.0.5, inclusive a versão offline) para os bugs Maven antigos (`cobertura.legado`, `cobertura.legado_offline`).
- [x] Planilha do Google atualizada sozinha por um app da web do Apps Script (`tools/planilha/Code.gs`): caixa de saída no SQLite, nova tentativa com espera crescente, linha atualizada no lugar, aba Resumo recalculada, reconciliação periódica.
- [x] Telas Painel e Tabela dos bugs, com detalhe de cada bug e filtro por projeto, situação e texto.
- [x] Guarda de espaço livre: a cobertura pausa abaixo de `cobertura.min_livre_gb` (padrão 5) e mostra o motivo; o usuário libera espaço e clica em Continuar.
- [x] Pasta de trabalho própria (`cobertura.work_dir`, padrão `E:\JFLP\cobertura`) e checkout apagado ao fim de cada bug.
- [x] Escolha das etapas que rodam (`cobertura.kinds`) e limite de tentativas dos Jaguar (`cobertura.max_tentativas_jaguar`, padrão 4).
- [x] Bugs sem `pom.xml` classificados como não executáveis, em vez de falha.
- [x] Suíte de testes `unittest` sem rede (Maven e Java falsos).

### Falta para declarar o MVP

Os números abaixo são **propostas**, para serem ajustados depois de ver os dados reais da primeira passada completa.

- [ ] **Passada completa.** Rodar os 1162 bugs até o fim com as três etapas, sem intervenção além de Continuar depois de pausa por disco. Critério: nenhum bug fica em "pendente" ou "rodando" ao final.
- [ ] **Qualidade mínima.** Proposta: pelo menos 70% dos bugs com Jaguar 2 `ok` ou `ok_testes_falhando`, pelo menos 85% com Maven concluído e pelo menos 60% com Jaguar 1 concluído. Todo bug fora disso precisa de um motivo registrado (falhou com diagnóstico ou não executável), nunca "sem informação".
- [ ] **Planilha fiel.** Depois da passada, o número de linhas da aba Bugs é igual ao número de bugs do lote (nenhuma duplicada, nenhuma faltando) e a aba Resumo bate com o Painel. Conferir também depois de "Reenviar tudo".
- [ ] **Retomada.** Fechar o programa (e, em outra rodada, reiniciar o PC) no meio da cobertura e conferir que ela retoma sozinha, sem perder execuções concluídas nem duplicar linhas na planilha.
- [ ] **Importação sem sobras.** Os ~140 bugs do pipeline PowerShell aparecem no app e na planilha como importados, e o Jaguar 2 deles roda depois.
- [ ] **Disco.** A passada completa termina sem encher o C: nem o E:, só com a pausa por espaço como rede de segurança. Registrar o consumo final de `runs/`, `m2/` e da pasta de trabalho.
- [ ] **Refazer converge.** Após `refazer`, a taxa de sucesso sobe ou fica igual; o que continua falhando tem motivo estável.
- [ ] **Importação pela interface.** Hoje só pela CLI; para o MVP basta documentar a ordem correta (importar antes de iniciar os workers), mas um botão no Ambiente evitaria o erro.
- [ ] **Roteiro de aceite escrito.** Uma lista curta e executável (comandos e telas) que qualquer pessoa do grupo repita numa máquina nova.

### Limitações conhecidas

- **Jaguar 1 em fila única.** O jar 0.0.6 não aceita escolher a porta: não existe a propriedade `jaguar.port` (medido), então dois jobs do Jaguar 1 em paralelo disputam a mesma porta. Por isso `jaguar1.port_aware` não é suportado por esse jar e o Jaguar 1 roda um de cada vez, mesmo com vários workers.
- **Jaguar 2 falha muito depois de várias tentativas.** Na medição do piloto, cerca de 58 de 94 execuções do Jaguar 2 falharam mesmo depois de 8 tentativas, por erro de compilação ou de projeto, e só uma foi salva nas últimas. Essas tentativas consumiam cerca de 78% do tempo, e por isso o limite virou 4 (`cobertura.max_tentativas_jaguar`). Esses bugs terminam como falha com diagnóstico, não como dado.
- **Perfis legados só no Maven.** O JDK 7 + Maven 3.0.5 só é tentado na etapa Maven dos bugs da cobertura; os Jaguar não têm perfil legado.
- **Importação só pela CLI** (`jflp cobertura importar`), e precisa rodar antes dos workers ou com a cobertura pausada.
- **Um único `m2` compartilhado** entre todos os workers. Se aparecer corrupção, a saída é separar um `m2` por worker (não implementado).
- **Checkouts apagados por bug.** Economiza disco, mas o "ver no código" de um bug da cobertura lê o ramo do bug no repositório, não uma cópia arquivada.
- **A guarda de disco não retoma sozinha.** Depois de liberar espaço, é preciso clicar em Continuar.
- **A planilha tem atraso.** O estado "rodando" e as mudanças feitas fora dos workers (importação, refazer) chegam à planilha pela reconciliação de 10 minutos.
- **Só uma cobertura por vez**, e só Windows na prática (o `jflp.sh` só teve a sintaxe conferida).
- **O app do Apps Script é público com token.** Quem tiver a URL e o token grava na planilha; o aviso "app não verificado" do Google aparece uma vez.
- **Reativar lote cancelado.** `refazer` reativa um lote que o usuário cancelou de propósito.

### Riscos

- **Tempo total.** Com o Jaguar 1 serializado e bugs lentos, a passada completa pode levar dias; o ritmo medido no piloto é o que decide. Mitigação: priorizar projetos, limitar tentativas, rodar com o computador sem dormir (o app já impede a suspensão).
- **Disco.** Os XMLs do Jaguar chegam a dezenas de GB. Mitigação: `JFLP_HOME` no E: (ver README, seção 8), guarda de espaço e checkout apagado por bug.
- **Rede e repositórios mortos.** Bugs antigos dependem de repositórios Maven que sumiram ou bloqueiam `http`; parte nunca será executável.
- **Cota do Apps Script.** Muitas linhas em pouco tempo podem esbarrar nos limites do Google; o envio já espera e tenta de novo, mas uma carga inicial grande pode demorar.
- **Corrupção do `m2` compartilhado** com vários workers escrevendo juntos.
- **Dependência de um jar do Jaguar 1 que não podemos alterar** (sem o código-fonte aqui, a porta não é configurável).
- **Perfis legados que não reproduzem o defeito:** o bug "roda" mas sem os testes falhando, e o dado não serve à pesquisa. Vale conferir por amostra.

## Fase 2: generalista

Tudo abaixo é **proposta**. Tamanhos: P (dias), M (uma a duas semanas), G (mais que isso). Sem datas.

O que já existe e serve de base: o preparo de bugs do Defects4J (via WSL), do Bugs.jar e de pastas locais ou repositórios git (`sbfl/prepare.py` e `sbfl/adapters/base.py`), a fila com N workers (`WorkerPool` em `sbfl/queue.py`), a derivação de situação por bug (`sbfl/cobertura.py`) e o envio para a planilha (`sbfl/planilha.py`).

| Item | O que é | Tamanho |
|---|---|---|
| Cobertura para qualquer adaptador | Generalizar `sincronizar` para enumerar os bugs de um adaptador qualquer (Defects4J, pasta local, git), não só a pasta do Bugs.jar | M |
| Mais de uma cobertura | Hoje existe uma por vez; permitir uma cobertura por benchmark, cada uma com seu lote, painel e tabela | M |
| Escolha do benchmark na tela | Escolher o benchmark e a pasta na tela Cobertura, em vez de depender de `bugsjar.root` | P |
| Planilha configurável por benchmark | Colunas, abas e resumo definidos por benchmark (o gabarito do Defects4J difere do catálogo do Bugs.jar), ou exportação para CSV/XLSX sem o Google | M |
| Exportação do dataset pela interface | Hoje `jflp export` só existe na CLI | P |
| Importação pela interface | Botão no Ambiente para `jflp cobertura importar`, com a trava contra rodar durante a execução | P |
| Jaguar 1 com porta configurável | Requer o código-fonte do Jaguar 1 (ou outro jar) que leia `-Djaguar.port`; depois liga o `jaguar1.port_aware` | G (depende de terceiros) |
| `m2` por worker (opcional) | Evitar corrupção do repositório compartilhado, ao custo de disco | M |
| Perfil legado também no Jaguar | Estender o JDK 7 / Maven 3.0.5 aos Jaguar, se a pesquisa mostrar que vale | M |
| Retomada da guarda de disco | Voltar sozinho quando o espaço volta ao normal | P |
| Instalador e atalho | Instalador do Windows (ou pacote) que dispense clonar o repositório e criar o `.venv` | M |
| Linux | Validar de fato o `jflp.sh` em Arch/Ubuntu e ajustar caminhos de disco (hoje o padrão `E:\...` é do Windows) | M |
| CI | Rodar a suíte `unittest` (cerca de 7 minutos) a cada PR | P |
| Adaptador de outros benchmarks | Um adaptador por benchmark (por exemplo BugSwarm, GitBug-Java), seguindo o contrato de `Adapter` | M por benchmark |
| Teste de aceitação ponta a ponta | Um bug real pequeno rodado em CI com JDK e Maven reais, além do `jflp selftest` | M |

## Como acompanhar

- **Painel** (tela Cobertura, página inicial): bugs concluídos, barra por situação, ritmo, tempo restante, workers, etapas a rodar, estado da planilha e aviso de disco.
- **Tabela** (Cobertura > Tabela): um bug por linha, com filtro por projeto, situação e texto, e o detalhe de cada bug.
- **Linha de comando:** `jflp cobertura status` mostra os bugs por situação e por projeto, o ritmo e o tempo restante.
- Mudanças entre versões: veja o [CHANGELOG](../CHANGELOG.md). Uso e configuração: [README](../README.md).
