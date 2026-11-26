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
  JUnit 5), o que independe do launcher. Testado até aqui: Surefire 2.22 e 3.2 e
  `JUnitCore`. Ant, Gradle e o runner do Defects4J ainda não foram testados;
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
modos, e comparam tempo, testes falhos, CSV, matriz e o rank médio da linha
defeituosa (tirada do patch do benchmark) pelo Ochiai.

| bug | testes | sem ferramenta | Jaguar2 | JFLP | JFLP `lines=jacoco` | rank Jaguar2 | rank JFLP |
|---|---|---|---|---|---|---|---|
| Defects4J Lang-1 | 2.291 | 30,2 s | 143,4 s | 32,5 s | 45,3 s | 60 | 3,5 |
| Bugs.jar MATH-1005 | 4.986 | 61,1 s | 1.051,6 s | 69,8 s | 82,3 s | 8,5 | 9,5 |

Tempos com duas execuções em paralelo numa máquina de 4 núcleos. O que cada
número quer dizer:

- **Lang-1:** o Jaguar2 registra 31 testes falhos, contra o 1 que o Defects4J
  aponta. O Lang tem um ASM antigo no classpath de teste (cglib/EasyMock), e o
  ASM 9.1 sem realocar do Jaguar2 conflita com ele em qualquer ordem: com o
  projeto na frente, o JaCoCo do Jaguar2 quebra; com o Jaguar2 na frente, o
  cglib quebra. Mais 2 falhas vêm do `$jacocoData` que o JaCoCo injeta nas
  classes de teste e que aparece nos testes de reflexão. O rank 60 do Jaguar2
  reflete esse espectro corrompido. O JFLP registra exatamente o teste do
  Defects4J, também quando roda dentro do runner do próprio Defects4J.
- **MATH-1005:** o commons-math usa JUnit 4.10, que ordena os métodos de teste
  pela reflexão do JVM, e a ordem muda a cada execução. Como a matriz do fork não
  nomeia os testes, as linhas só podem ser comparadas como conjunto. O modo
  `lines=jacoco` bate com o Jaguar2 em 230 de 231 linhas do CSV e 4.896 de 4.986
  linhas da matriz, a mesma variação que o próprio JFLP tem entre duas execuções
  seguidas (4.902 de 4.986, por cobertura que depende da ordem). O segundo teste
  falho, `FastMathTest.checkMissingFastMathClasses`, também falha sem ferramenta
  nenhuma: ele compara com o `StrictMath` do JVM, e o fork exige Java 11.

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
