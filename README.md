# JFLP

Localização de defeitos por espectro (SBFL) para Java. Faz o mesmo que o
[fork do Jaguar2](https://github.com/c3nt4ur1/jaguar2) (cobertura por teste, os
10 heurísticos do Jaguar e a `coverageMatrix.txt`), gerando os mesmos arquivos,
só que sem editar o POM, a partir do Java 5 e bem mais rápido.

## Uso

```bash
mvn -q package            # gera target/jflp.jar (requer JDK 8 para compilar; veja Desenvolvimento)
```

No projeto a analisar, basta anexar o agente aos JVMs de teste. Não é preciso mexer no `pom.xml`:

```bash
JAVA_TOOL_OPTIONS="-javaagent:/caminho/jflp.jar" mvn clean test
```

```bash
java -jar /caminho/jflp.jar report
```

`report` lê `target/jflp/*.jflp` e `target/classes` e grava em `target/`:

| arquivo | formato |
|---|---|
| `jaguar2.csv` | igual ao `CsvExporter` do Jaguar2 (`classe,linha,cef,cnf,cep,cnp,susp`) |
| `jaguar2.xml` | igual ao `XmlExporter` do Jaguar2 |
| `coverageMatrix.txt` | igual ao do fork: uma linha por teste, `0`/`1` por linha de código, `+`/`-` no fim |
| `coverageMatrix.columns.csv` | a que classe e linha corresponde cada coluna da matriz (o fork não grava) |
| `coverageMatrix.tests.csv` | a que teste e resultado corresponde cada linha da matriz (o fork não grava) |
| `jflp-summary.txt` | contagens e todos os avisos |

Opções do agente (`-javaagent:jflp.jar=chave=valor,...`): `destdir` (padrão
`target/jflp`), `classdirs` (padrão `target/classes`, separados por `:` no
Linux, `;` no Windows) e `lines` (`entry`, o padrão, ou `jacoco`; veja abaixo). Opções do `report`: `java -jar jflp.jar` sem argumentos
lista todas, entre elas `--heuristic` (ochiai, tarantula, jaccard, zoltar, op,
minus, mccon, drt, kulczynski2, wong3) e `--strict` (sai com status 3 se houver
qualquer aviso).

Outras formas de anexar o agente: `mvn test -DargLine=-javaagent:...` (se o POM
não fixar `argLine`) ou `-javaagent` direto na linha do `java`. Rode `clean`
entre execuções (ou apague `target/jflp`): o `report` junta todos os arquivos
que encontrar.

## O que muda em relação ao Jaguar2

Medido no jsoup 1.11.3 (657 testes, JDK 11), o projeto do `Execution.md` do fork:

| | tempo de `surefire:test` |
|---|---|
| Maven puro | 3,8 s |
| JFLP (agente + `report`) | 4,5 s |
| Jaguar2 fork (JaCoCo 0.8.7) | 26–34 s |

**Mesmas linhas e mesmo layout.** Quais linhas contam como código é decidido
pelos próprios filtros do JaCoCo 0.8.7, a versão do fork. O conjunto de linhas é
idêntico ao do JaCoCo, verificado contra o `Analyzer` dele no `rt.jar` inteiro do
JDK 8, em módulos do JDK 21 e no JUnit/ASM. As colunas da matriz seguem a ordem
do `HashMap` do JaCoCo, desempatada pela ordem de listagem do diretório, como no
fork. No jsoup, a matriz do JFLP tem as mesmas 646 × 7.332 células da do fork.

**Uma diferença deliberada: a linha que lança exceção conta como coberta.** O
JaCoCo só marca uma linha depois que um probe posterior confirma que ela
terminou, então a linha onde a exceção nasce aparece como não coberta, e em
SBFL essa costuma ser a linha defeituosa. No jsoup, isso explica as 67 células,
de 4,7 milhões, em que as matrizes diferem: todas 0→1, em testes que esperam
exceção, mais as 3 linhas do teste que falha (`conn.connect()` e a cadeia de
chamadas até ela).

**Para reproduzir o Jaguar2 bit a bit, use `lines=jacoco`**
(`-javaagent:jflp.jar=lines=jacoco`). Nesse modo as classes são instrumentadas
pelo próprio JaCoCo 0.8.7, e o `report` calcula uma única vez, por classe, quais
linhas cada probe do JaCoCo cobre. No jsoup, o `jaguar2.csv` e a
`coverageMatrix.txt` saem byte a byte idênticos aos do fork, em 12 s contra 26–34
s. O `report` detecta o modo sozinho e o registra no `jflp-summary.txt`.

Problemas do Jaguar2/fork que o JFLP não tem:

- sem `-Djaguar2.classesDirs`, que o `Execution.md` não menciona, o Jaguar2
  analisa zero classes e grava um `jaguar2.csv` vazio sem avisar nada;
- com o agente JaCoCo em `output=none`, a cobertura não é zerada entre testes
  (quem zera é o *output* do JaCoCo) e os espectros saem acumulados;
- o fork compila para bytecode Java 11 e não roda em Java 8, embora o Jaguar2
  original rode a partir do Java 6;
- a matriz não diz qual coluna é qual linha nem qual linha é qual teste, e é
  aberta em modo *append*: rodar duas vezes mistura execuções;
- a matriz reanalisa todas as classes a cada teste (`Analyzer.analyzeAll`), daí
  o tempo;
- o `%f` do CSV/XML usa o locale do JVM, então em `pt_BR` sai `0,707107` e o CSV
  quebra; o JFLP sempre usa ponto;
- no XML, uma linha que pertence a duas classes do mesmo arquivo-fonte (por
  exemplo `new Runnable() {`) é contada duas vezes por teste; o JFLP conta uma;
- os heurísticos fazem as contas em `int` e estouram em suítes grandes (Zoltar
  já com `cnf * cep > 214748`); o JFLP usa `double`;
- só funciona com JUnit 4 via listener no POM; o JFLP pega os testes pelo
  próprio framework (JUnit 3, JUnit 4 e, por consequência, a engine vintage do
  JUnit 5), o que independe do launcher. Testado: Surefire 2.7, 2.22 e 3.2,
  `JUnitCore` e o runner do Defects4J (Ant, inclusive com o `AntClassLoader`
  isolado). Gradle ainda não foi testado. JUnit 5 Jupiter e TestNG ainda não
  são detectados;
- com `forkCount > 1` cada JVM sobrescreve `target/jaguar2.csv`; o JFLP grava um
  arquivo por JVM e o `report` junta tudo (testado com `forkCount=2`);
- um JVM morto no meio (timeout, `System.exit`) perde tudo; o JFLP grava cada
  teste assim que termina.

Semântica mantida do Jaguar2: a cobertura é zerada quando cada teste começa, então
o código de `@BeforeClass` não é atribuído a teste nenhum, e o inicializador
estático de uma classe conta para o primeiro teste que a carrega. Um teste com
*assumption* falha não é contado. Uma falha fora de qualquer teste (por exemplo,
em `@BeforeClass`) não é contada, mas vira aviso. Testes rodando em paralelo no
mesmo JVM não podem ser separados, e isso vira aviso em vez de dado misturado.

## Comparação em benchmarks

`bench/` tem os harnesses (veja o cabeçalho de cada script):
`build-jaguar2.sh` constrói o fork; `d4j.py` (Defects4J) e `bugsjar.py`
(Bugs.jar) rodam por bug o baseline sem ferramenta, o Jaguar2 e o JFLP nos dois
modos; `summarize.py` recalcula a tabela a partir dos arquivos que eles deixam.
Os três modos de um mesmo benchmark usam o mesmo runner (Defects4J: `JUnitCore`
com a lista `tests.all`; Bugs.jar: `surefire:test`, testes no JDK 11, que o fork
exige). "rank" é a posição média, com empates, da linha defeituosa mais bem
colocada pelo Ochiai, com as linhas tiradas do patch de cada benchmark. Os
tempos são só da execução dos testes, sem o `report`.

| bug | testes | falhos (esperado / Jaguar2 / JFLP) | sem ferramenta | Jaguar2 | JFLP | JFLP `jacoco` | rank Jaguar2 | rank JFLP | `lines=jacoco` vs Jaguar2: CSV / matriz |
|---|---|---|---|---|---|---|---|---|---|
| Chart-1 | 2.193 | 1 / 1 / 1 | 2,3 s | 354,5 s | 2,8 s | 2,8 s | 35 | 35 | idêntico / idêntica |
| Cli-1 | 94 | 1 / 1 / 1 | 0 s | 0,8 s | 0,1 s | 0,1 s | 7 | 7 | idêntico / idêntica |
| Codec-1 | 206 | 5 / 5 / 5 | 0,4 s | 2 s | 0,3 s | 0,3 s | 1,5 | 1,5 | idêntico / idêntica |
| Compress-1 | 73 | 1 / 1 / 1 | 0,1 s | 2,1 s | 0,2 s | 0,2 s | 125 | 125 | idêntico / mesmas linhas, outra ordem |
| Csv-1 | 54 | 1 / 1 / 1 | 0,3 s | 1,1 s | 0,4 s | 0,4 s | 105 | 105 | 114/125 / 50/54 ¹ |
| Gson-1 | 720 | 1 / 9 / 9 ² | 0,3 s | 10 s | 0,3 s | 0,3 s | — | — | 553/571 / 625/720 ¹ |
| Jsoup-1 | 139 | 1 / 1 / 1 | 0,6 s | 1,7 s | 0,3 s | 0,3 s | 3 | 3 | idêntico / 137/139 ¹ |
| JxPath-1 | 308 | 2 / 2 / 2 | 1,3 s | 11,9 s | 1,9 s | 1,9 s | 261 | 261 | 1.060/1.621 / 269/308 ³ |
| Lang-1 | 2.291 | 1 / **31** / 1 | 29,8 s | 137,9 s | 31,2 s | 31,3 s | 60 ⁴ | 3,5 | — ⁴ |
| Lang-3 | 2.286 | 1 / **31** / 1 | 28,5 s | 136,6 s | 27,5 s | 28,1 s | 60 ⁴ | 8,5 | — ⁴ |
| Lang-10 | 2.198 | 2 / **30** / 2 | 26,7 s | 108,6 s | 26,3 s | 25,8 s | 102,5 ⁴ | 55,5 | — ⁴ |
| Lang-33 | 1.670 | 1 / 1 / 1 | 11,4 s | 65,4 s | 11,9 s | 11,2 s | 4,5 | 4,5 | idêntico / 1.652/1.670 ¹ |
| Math-5 | 4.181 | 1 / 1 / 1 | 59,2 s | 900,2 s | 225,1 s ⁵ | 207,8 s | 1,5 | 1,5 | idêntico / 4.163/4.181 |
| Time-1 | 4.041 | 1 / 1 / 1 | 1,7 s | 226,8 s | 2,4 s | 2,6 s | 3,5 | 3,5 | 117/128 / 3.924/4.041 ¹ |
| MATH-1005 | 4.986 | 1 / 2 / 2 ⁶ | 59,7 s | 1.050 s | 67 s | 64 s | 8,5 | 9,5 | 230/231 / 4.885/4.986 ¹ |
| MATH-555 | 2.371 | 1 / 2 / 2 ⁶ | 12,8 s | 223 s | 16 s | 14,3 s | — ⁷ | 4 | — ⁷ / 2.316/2.371 ¹ |
| MATH-639 | 2.887 | 1 / 2 / 2 ⁶ | 17,1 s | 351 s | 22,4 s | 20,5 s | — ⁷ | 26,5 | — ⁷ / 2.838/2.887 ¹ |
| MATH-695 | 3.069 | 1 / 2 / 2 ⁶ | 20,3 s | 424 s | 25,5 s | 23,6 s | — ⁷ | 7,5 | — ⁷ / 3.009/3.069 ¹ |
| MATH-704 | 3.144 | 1 / 2 / 2 ⁶ | 20,6 s | 407 s | 26,9 s | 23 s | — ⁷ | 14,5 | — ⁷ / 3.086/3.144 ¹ |
| MATH-713 | 3.123 | 1 / 2 / 2 ⁶ | 20,4 s | 430 s | 26,6 s | 23 s | — ⁷ | 32,5 | — ⁷ / 3.059/3.123 ¹ |

Os 14 primeiros são do Defects4J, os 6 últimos do Bugs.jar. Pelo runner do
próprio Defects4J (Ant), com o agente em `JAVA_TOOL_OPTIONS`, o JFLP registra
exatamente os testes falhos que o Defects4J aponta nos 14 bugs.

1. A ordem dos testes muda de uma execução para outra (JUnit 3, ou JUnit < 4.11,
   ordenam métodos por reflexão), e a matriz do fork não nomeia os testes, então
   só dá para comparar como conjunto de linhas. O resto da diferença é código
   que roda uma vez e conta para o teste que estiver rodando no momento
   (inicializadores estáticos, caches); o próprio JFLP, rodado duas vezes, varia
   na mesma medida (MATH-1005: 4.902/4.986 linhas entre duas execuções dele).
2. Pelo `JUnitCore`, 8 testes do Gson falham nas duas ferramentas e sem
   ferramenta também; pelo runner do Defects4J, só o esperado.
3. JxPath guarda expressões compiladas num cache de `SoftReference`. A memória
   que o Jaguar2 aloca ao reanalisar tudo a cada teste faz o GC esvaziar o
   cache, e mais testes passam pelo parser: o Jaguar2 muda o comportamento do
   programa que mede. JFLP contra ele mesmo: 1.603/1.621 linhas do CSV.
4. O Jaguar2 quebra 30 testes do Lang. O ASM 9.1 que ele traz sem realocar
   conflita com o ASM antigo do cglib/EasyMock em qualquer ordem de classpath
   (com o projeto na frente, quebra o JaCoCo; com o Jaguar2 na frente, o
   cglib), e o `$jacocoData` que o JaCoCo injeta nas classes de teste aparece
   nos testes de reflexão. O espectro do Jaguar2 fica corrompido.
5. Os testes do BOBYQAOptimizer: o método `bobyqb` passa de 5.770 para 12.111
   bytes com os probes, acima do limite de 8.000 do JIT do HotSpot, e roda
   interpretado (9 s → 93 s; 28 s com `-XX:-DontCompileHugeMethods`). Os
   probes do JaCoCo, e portanto o Jaguar2, têm o mesmo problema. O agente avisa
   quando isso acontece.
6. O segundo teste falho, `FastMathTest.checkMissingFastMathClasses`, falha sem
   ferramenta nenhuma: ele compara com o `StrictMath` do JVM, e o fork exige
   Java 11 (o Bugs.jar registrou os resultados com Java 7).
7. Com o Surefire 2.7.2 desses bugs, o Jaguar2 grava a matriz mas nunca o
   `jaguar2.csv`, que só é escrito no `testRunFinished` do listener.

Outros problemas do fork encontrados nos benchmarks: em projetos sem `target/`
(Chart, layout Ant) todo teste falha dentro do listener, porque o fork grava em
`target/` sem criá-lo (o harness cria o diretório para poder comparar);
projetos com JUnit 3.8 no classpath não têm `JUnitCore` para o runner dele.

## Compatibilidade

- O jar é bytecode Java 5 (verificado pelo animal-sniffer com a assinatura
  `java15`) e foi testado com JDK 8, 11 e 21 (classes de até v65). Em Java 5 o jar
  precisa se chamar `jflp.jar`, porque o Java 5 só o põe no boot class path pelo
  manifesto. Não havia JVM 5/6 neste ambiente para testar a execução de fato.
- Classes de qualquer versão que o ASM 9.11 leia; sub-rotinas JSR (Java ≤ 1.4)
  são inlinadas como no JaCoCo.

## Desenvolvimento

```bash
JAVA_HOME=/caminho/jdk8 mvn package
```

O JDK 8 é o último `javac` que gera bytecode Java 5. Os testes unitários
comparam o conjunto de linhas com o JaCoCo (`-Djflp.corpus=a.jar:b.jar` usa outro
corpus) e codificam a semântica de cobertura. `src/it/run-sample.sh` roda o
projeto de exemplo com o agente; `src/it/run-sample-jaguar2.sh` roda o mesmo
exemplo no Jaguar2 real para comparação.

Componentes de terceiros embutidos no jar (realocados em `jflp.internal`): ASM
(BSD-3-Clause) e JaCoCo core 0.8.7 (EPL-2.0, só os filtros de análise).

## Licença

MIT, veja [LICENSE](LICENSE).
