# Plano: desempenho do Jaguar 2 na cobertura (2026-11-04)

## Medição (lote 9 "Cobertura bugs-dot-jar", 94 bugs Jaguar 2 feitos)

- 17,8 h de relógio, 13,1 h dentro do Maven, 3,0 h em tentativas que deram dado (36 bugs `ok_testes_falhando`).
- **4,7 h fora do Maven**: `working_copy()` (copytree do pristine inteiro) roda **a cada tentativa**.
  Camel: 79% do tempo (cada tentativa ≈ 150 s de relógio para 30 s de Maven); Wicket 51%.
- **~10 h em tentativas que repetem a mesma falha**:
  - falha na fase `reactor` (`mvn install -pl M -am` com os POMs originais) e as tentativas seguintes só
    mudam correções do `jaguar2-pom.xml` / versão do Surefire, que o reactor nunca usa
    (CAMEL-3394: 6 de 7 tentativas com JDK 11 no reactor);
  - build que nem chegou aos testes e a tentativa seguinte só troca a versão do Surefire.
- Nos bugs que dão certo (MATH-1045: 14 min de Maven, 5,4 min de testes) o restante é o próprio Jaguar 2
  (listener por teste + reanálise dos .class por teste). **Fora de escopo**: o Jaguar 2 não é alterado.

## Famílias de falha (1ª tentativa, evidência nos logs em E:\JFLP\home\runs)

| Família | Bugs (amostra) | Fase | Erro |
|---|---|---|---|
| ASM antigo | LOG4J2-1025/1046/1058/1061/1062/1067 | jaguar2 | `class org.jacoco.core.internal.flow.ClassProbesVisitor has interface org.objectweb.asm.ClassVisitor as super class` (JVM do Surefire) |
| javac 8 estrito | WICKET-1677 (JDK 8: inferência de genéricos), FLINK-1133 (`void cannot be converted`), CAMEL/WICKET com JDK 11 (`Source option 5 is no longer supported`) | reactor | precisa javac 7 |
| RAT 0.10 | FLINK-1311/1333/1382 | reactor | `apache-rat-plugin:0.10:check ... Too many files with unapproved license` (o `-Drat.skip` não existe na 0.10) |
| jdk.tools | FLINK-1437/1458/1471 | reactor | `jdk.tools:jdk.tools:jar:1.6 (system)` em `<JDK11>/../lib/tools.jar` (precisa JDK 8) |
| scr | OAK-1024/1081/1093… | reactor | `maven-scr-plugin:1.9.0:scr ... AnnotationProcessor is not an ImageIO SPI class` (JDK ≥ 8) |
| exec .sh | ACCUMULO-1044/1120/1183… | reactor | `exec-maven-plugin:1.2.1:exec (generate-thrift)` tenta rodar `generate-thrift.sh` no Windows |
| sortpom | ACCUMULO-1312/1348 | jaguar2 | `maven-sortpom-plugin:2.1.0:verify` reprova o `jaguar2-pom.xml` gerado |

JDK 7 (Zulu 7.56, o mesmo de `toolchain/download.py`) existe em
`E:\Documentos\GitHub\bugs-dot-jar\relatorios_cobertura\_tools\jdks\zulu7.56.0.11-ca-jdk7.0.352-win_x64`.
Os jars do Jaguar 2 0.0.2 são bytecode 50 (Java 6); Surefire 3.5.3 exige Maven em Java 8.

## Trabalho

### A. Laço de tentativas (Sonnet) — `sbfl/execute.py`, `sbfl/prepare.py`, `sbfl/autosearch.py`
1. **Cópia reaproveitada**: uma cópia de trabalho por execução; entre tentativas, `reset_working_copy`
   restaura o estado do pristine no lugar (apaga o que não existe no pristine, recopia o que mudou de
   tamanho/mtime ou sumiu, respeitando `_ignore_work`). As saídas da melhor tentativa são guardadas
   antes do reset (snapshot de `res.outputs`, não do checkout). Com `archive_checkout` ligado mantém o
   comportamento atual (cópia nova por tentativa).
2. **Poda de tentativas inúteis**: depois de falha na fase `reactor`, pular candidatas cuja "chave de
   reactor" (JDK/Maven efetivos + parâmetros que o reactor usa: offline, unblock-http, flags de reactor
   novas do item B) já falhou no reactor; depois de falha antes da fase de testes (rank < 4), pular
   candidatas que só mudam a versão do Surefire. A tentativa pulada não conta no limite.
3. Testes unitários para os dois itens; nenhuma mudança nas saídas gravadas.

### B. Correções novas (Sonnet) — `sbfl/diagnose.py`, `sbfl/pomgen/fixes.py`, `sbfl/run_maven.py`, `sbfl/run_jaguar2.py`, `sbfl/home.py`
Cada família vira padrão de diagnóstico → sugestão (correção nomeada) → aplicada pela busca automática,
validada rodando 1 bug real da família. Ordem de prioridade (pelo número de bugs pendentes):
javac 7 (Wicket 290, Camel 148, Flink 70) > ASM (log4j2 82) > scr (Oak 278) > RAT / jdk.tools (Flink) >
exec / sortpom (Accumulo 98). Correções que valem no reactor entram como flags da linha de comando do
`install_reactor` (o reactor usa os POMs originais). Se uma família não tiver correção segura, registrar
o motivo e seguir.

### C. Consolidador (Haiku) — `E:\Documentos\GitHub\bugs-dot-jar\matrizes_consolidadas\_tools\consolidar.py`
Junta num só lugar as execuções bem-sucedidas: JFLP (`E:\JFLP\home\jflp.db` + `runs/`) e pipeline
PowerShell (`relatorios_cobertura/`), uma pasta por bug com Maven / Jaguar 1 / Jaguar 2 + `indice.csv`,
`LISTAGEM.md` (todas as execuções, inclusive as que falharam). Idempotente, só leitura nas fontes.

## Depois
Merge de A e B, suíte inteira, cobertura retomada só com Jaguar 2 (`cobertura.kinds = ["jaguar2"]`),
"refazer" dos Jaguar 2 que falharam, consolidação rodada de novo no fim.
