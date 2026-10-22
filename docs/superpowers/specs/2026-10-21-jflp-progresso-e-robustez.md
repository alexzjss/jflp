# JFLP Studio — progresso visual e robustez em repositórios reais (v0.4)

Data: 2026-10-21. Base: `feat/jflp-studio` depois de fila/cancelamento (c49c0f6), pasta sem POM (7d5f3be) e
`inside()` sem `resolve()` (3ec8ae1). Restrições de sempre: textos em português, subprocessos em lista, servidor
só em 127.0.0.1, nada alterado fora da pasta do JFLP, testes `unittest` sem rede.

## 0. O que a análise ao vivo encontrou (2026-10-21, Windows 11)

Testado de ponta a ponta: `jflp selftest` (ok); Executar com a raiz do bugs-dot-jar; Bugs.jar
`commons-math/bugs-dot-jar_MATH-1005_91d280b7` com Maven, Jaguar 2 e Jaguar 1; Fila (remover e cancelar).

| # | Achado | Gravidade |
|---|--------|-----------|
| 1 | App aberto de dentro de um app MSIX (terminal do Claude) grava em `AppData\Local\Packages\<app>\LocalCache\Local\jflp` (AppData redirecionado). `resolve()` dava "Execução não encontrada" — **corrigido** (`winpath.inside`). Falta avisar o usuário. | alta |
| 2 | Maven puro no commons-math: o POM do projeto injeta JaCoCo 0.6.2 → `FATAL ERROR in native method: processing of -javaagent failed` / "forked VM terminated". No Maven puro a cobertura não serve para nada. | alta |
| 3 | Busca automática repetiu a mesma configuração (JDK "padrão" = JDK 8 → tentativa 2 idêntica à 1). | média |
| 4 | Status final = última tentativa real (JDK 11: `Source option 5 is no longer supported` → `compilacao_falhou`), escondendo a tentativa que foi mais longe (a 1ª chegou aos testes). | média |
| 5 | Jaguar 1: XML certo (`demo.Calc:6` = 1.0), mas `tests` = 0 e status `ok` em vez de `ok_testes_falhando`; o log tem `Test testAbsNegative(demo.CalcTest) : Failed`. | média |
| 6 | Guia do Executar: `find_poms` lista submódulos (`accumulo/core`, `camel/camel-core`…) e corta em 30 — `maven`, `wicket`, `logging-log4j2`, `jackrabbit-oak` sumiram. | média |
| 7 | `jflp run <kind> <pasta>` não aceita pasta local ("Bugs locais não podem ser reconstruídos pela chave"). | baixa |
| 8 | Tela ao vivo = log cru; no 1º uso ~80% das linhas são `Downloading/Downloaded from central`. Sem etapa, tempo, testes, tentativas. | alta (pedido do usuário) |
| 9 | Fila/lote atualizam a cada 2 s trocando o HTML inteiro: um clique pode cair num botão que acabou de ser substituído. | baixa |

Funcionou bem: Jaguar 2 no MATH-1005 (`ok_testes_falhando`, 93 s, correções `compiler-java`+`skip-rat` automáticas,
41 testes/1 erro, linhas `MathArrays:816+` com o defeito em destaque); cancelar job em andamento (Java morto em
segundos, execução e `work/w` descartados); remover da fila; aviso Bugs.jar e "usar como bugsjar.root".

## 1. Progresso visual ao vivo (achado 8)

### 1.1 Estado estruturado (servidor)
Novo módulo `sbfl/progress.py`, puro e testável: `Progress` alimentado linha a linha (`feed(line)`), com `to_dict()`:
- `etapa`: `preparando` → `tentativa` → `gravando` → `fim`. Antes da 1ª `# jflp tentativa N` = preparando.
- `fase` da tentativa: marcadores existentes `# jflp fase: reactor|pom|maven|jaguar2|jaguar1` e, dentro do Maven,
  o objetivo corrente pela linha `--- <plugin>:<versão>:<goal> (<id>) @ <artefato> ---` (Maven 3.9 também
  `--- <plugin>:<versão>:<goal> (<id>) @ <artefato> ---` com prefixo `[INFO]`), mapeado para rótulos:
  resources→"copiando recursos", compiler:compile→"compilando", compiler:testCompile→"compilando testes",
  jacoco:prepare-agent→"preparando cobertura", surefire:test→"rodando testes", jacoco:report→"relatório de cobertura",
  outro→nome do goal.
- `modulo`: `Building <nome> <versão>` e, no reactor, `[n/m]` → `{nome, i, n}`.
- `downloads`: contagem e bytes de `Downloaded from <repo>: <url> (<n> kB at …)`; `baixando` = há `Downloading` sem par.
- `testes`: `Running <classe>` (classe atual), soma de `Tests run: X, Failures: Y, Errors: Z, Skipped: W` por classe
  (ignorar a linha de totais `Results :`/`Tests run:` final sem `-- in`/`Time elapsed` para não contar duas vezes),
  lista curta das classes com falha. Jaguar 1: `Test <metodo>(<classe>) : Passed|Failed` e `Test # N`.
- `compilacao`: `Compiling N source files` (último N).
- `tentativas`: `[{n, fase, status?}]` — status vem do novo marcador `# jflp tentativa N: <status>` (§2.4).
- `ultimo_erro`: última linha `[ERROR]` não vazia (sem as de ajuda `To see the full stack trace`, `Re-run Maven`,
  `[Help 1]`, `For more information`).
- `inicio`/`decorrido` (s).
O `Worker` mantém um `Progress` do job atual (zerado em `_run_job`), alimentado em `_on_line`; `current()` inclui
`progress`. Leve: nada de regex pesado por linha; testes com logs reais recortados (Maven 3.6.3 e 3.9, Surefire
2.x e 3.x, reactor, Jaguar 1).

### 1.2 Tela ao vivo (`/job/{id}/ao-vivo`)
- SSE existente continua mandando as linhas; acrescentar um evento `estado` (JSON de `progress`) a cada ~1 s
  enquanto o job roda (e um final). Sem dependências novas; JS puro no template.
- Cabeçalho: bug, tipo, cronômetro (conta no cliente a partir de `inicio`), botão **Cancelar** (rota da fila).
- **Linha do tempo** horizontal: Preparar → Compilar → Testes → (Jaguar/Cobertura) → Gravar, com o passo atual
  animado (pulso/spinner CSS), passados marcados ✓, falha marcada ✗. No Maven puro sem Jaguar a etapa some.
- **Cartões**: Testes (números grandes: rodados / falhas / erros; classe atual; barra se houver estimativa —
  total da última execução do mesmo bug/tipo ou nº de testes relevantes), Downloads (n arquivos, MB, "baixando…"),
  Módulo do reactor (i/n com barra), Compilação (N arquivos).
- **Tentativas** como chips `#1 plugin_incompativel` com a cor/rótulo de status já usados no app; a atual pulsa.
- **Último erro** em destaque quando houver.
- **Log** num `<details>` (fechado por padrão), com caixa "ocultar downloads" marcada por padrão e rolagem automática.
- Ao terminar: resumo (status com cor, testes, tempo) e botões "Ver execução" / "Ver no código" em vez de
  redirecionar sozinho na hora (redireciona depois de 3 s com aviso "abrindo…", cancelável).
- Tema claro/escuro e largura de celular, seguindo `app.css`.

### 1.3 Mini progresso em outras telas
- Cartões da tela Executar (`_executar_cartoes.html`) e linha "Em andamento" da Fila: etapa + fase + testes
  (ex.: "Tentativa 2 · rodando testes · 128 testes, 1 falha · 01:42") e uma barra fina indeterminada.
- Atualização sem trocar botões: a parte que muda vai num elemento próprio (`hx-swap` só do miolo),
  corrigindo o achado 9 (os botões não são substituídos a cada 2 s).

## 2. Robustez (achados 1–7)

2.1 **Aviso de AppData redirecionado** (achado 1): `home.virtualized_root()` devolve o caminho real quando
`Path(home.root / 'x').resolve()` de um arquivo novo cai fora de `home.root` (ou quando `sys.executable`/ambiente
indicam pacote MSIX: `\WindowsApps\` ou `LOCALAPPDATA` redirecionado). Painel e `jflp doctor` mostram:
"O JFLP foi aberto de dentro de outro aplicativo (ex.: terminal do Claude) e o Windows grava os dados em <real>.
Abra pelo atalho JFLP Studio para usar a pasta normal." Só aviso; nada muda de lugar.

2.2 **Maven puro sem JaCoCo** (achado 2): o perfil `maven` acrescenta `-Djacoco.skip=true` (não mexe no POM).
O diagnóstico reconhece `processing of -javaagent failed` / `Error opening zip file or JAR manifest missing`
com `jacoco` no comando → sugestão `skip-jacoco` (para Jaguar 1, que usa o POM original, também).

2.3 **Sem tentativas repetidas** (achado 3): `autosearch` deduplica pela configuração *efetiva* — JDK/Maven
resolvidos (o "padrão" do perfil vira o número real antes de comparar).

2.4 **Status pela tentativa mais avançada** (achado 4): ordenar por fase alcançada
(checkout < pom < reactor < compilação < testes < jaguar/saída) e, empatando, pela mais recente; o run usa as
saídas/checkout dessa tentativa e lista as outras em `warnings` ("tentativa 4 (JDK 11): compilacao_falhou").
Como a cópia de trabalho de tentativas anteriores é apagada, guardar o `keep` da melhor até o fim (só uma cópia
extra). `execute` emite `# jflp tentativa N: <status>` ao fim de cada tentativa (usado pela tela ao vivo).

2.5 **Testes do Jaguar 1** (achado 5): ler `Test <m>(<C>) : Passed|Failed` do log do JaguarRunner para preencher
`tests` (`C#m` → passed/failed) → `ok_testes_falhando` quando houver falha, como Maven/Jaguar 2.

2.6 **Guia do Executar** (achado 6): `find_poms` não desce em pasta que já tem `pom.xml` (os filhos são módulos dela);
mostra só projetos; limite 50 com aviso "e mais N". Na raiz do Bugs.jar, lista os repositórios.

2.7 **CLI com pasta local** (achado 7): `jflp run <kind> <pasta|URL@commit> [--module M]` usa `local_bug`.

## 3. Aceitação
1. Suíte completa verde.
2. Ao vivo: Jaguar 2 no MATH-1005 mostra etapas, fase "rodando testes", 41 testes/1 erro, chips de tentativa,
   log recolhido; selftest com Maven + Jaguar 2 + Jaguar 1 em lote.
3. Maven puro no MATH-1005 passa da fase de testes (sem JaCoCo) ou, se falhar, com status da tentativa mais avançada.
4. Jaguar 1 do selftest: `ok_testes_falhando`, 4 testes, 1 falha.
5. Executar com a raiz do bugs-dot-jar lista os 8 repositórios.
