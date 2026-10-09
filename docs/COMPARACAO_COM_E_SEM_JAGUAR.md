# Protocolo: o mesmo módulo do Bugs.jar com e sem o Jaguar

**Objetivo.** Executar o mesmo módulo do Bugs.jar de duas formas, comparar os resultados e registrar o rank que o próprio Jaguar fornece. Nada além disso: não são calculadas fórmulas próprias nem EXAM.

**Como interpretei o pedido**

- **Sem Jaguar:** o Maven executa os testes do módulo (`mvn test`).
- **Com Jaguar:** o `JaguarRunner` executa, no mesmo módulo, os mesmos testes.
- **Comparação:** quantos testes rodam, quais passam e quais falham nos dois casos.
- **Rank:** a posição das linhas do defeito segundo o `suspicious-value` que o Jaguar grava no relatório XML.

**Caso:** bug MNG-5742 (Maven), módulo `maven-core`, branch `bugs-dot-jar_MNG-5742_6ab41ee8`. Use JDK 8.

---

## A. Passo a passo manual (Maven e Jaguar)

Os comandos têm duas versões: bash (Linux/macOS) e PowerShell (Windows). Substitua `$L` pela pasta `lib` do Jaguar (a que contém `jacocoagent.jar`).

### A1. Preparar o módulo

```bash
cd <clone do Bugs.jar>/maven
git checkout bugs-dot-jar_MNG-5742_6ab41ee8
mvn -B -q install -DskipTests -Drat.skip=true -Denforcer.skip=true -pl maven-core -am
cd maven-core
mvn -B -q dependency:copy-dependencies -DoutputDirectory=target/dependency -Drat.skip=true -Denforcer.skip=true
```

### A2. Sem Jaguar: só o Maven

```bash
mvn -B test -Dmaven.test.failure.ignore=true -Drat.skip=true -Denforcer.skip=true
```

Anote a linha final `Tests run: N, Failures: F, Errors: E, Skipped: S` e a lista dos testes que falham:

```bash
# bash
grep -lE "<<< (FAILURE|ERROR)!" target/surefire-reports/*.txt
```

```powershell
# PowerShell
Select-String -Path target\surefire-reports\*.txt -Pattern "<<< (FAILURE|ERROR)!" -List | ForEach-Object Path
```

### A3. Com Jaguar: as mesmas classes de teste

Para garantir que são os mesmos testes, use a lista de classes que o Maven acabou de executar:

```bash
# bash
ls target/surefire-reports/TEST-*.xml | sed 's#.*/TEST-##; s#\.xml$##' > tests.txt
```

```powershell
# PowerShell (ascii evita o BOM, que pode corromper o primeiro nome)
Get-ChildItem target\surefire-reports\TEST-*.xml | ForEach-Object { $_.BaseName -replace '^TEST-','' } | Set-Content tests.txt -Encoding ascii
```

Execute o Jaguar guardando a saída em `jaguar.log`:

```bash
# bash
L=<pasta lib do Jaguar>
java -javaagent:$L/jacocoagent.jar=output=tcpserver,port=6300 \
     -cp "$L/*:target/classes:target/test-classes:target/dependency/*" \
     br.usp.each.saeg.jaguar.core.cli.JaguarRunner \
     -p . -c target/classes -t target/test-classes -tf tests.txt \
     -h Ochiai -ot F -o manual -l DEBUG 2>&1 | tee jaguar.log
```

```powershell
# PowerShell
$L = "<pasta lib do Jaguar>"
java "-javaagent:$L\jacocoagent.jar=output=tcpserver,port=6300" `
     -cp "$L\*;target\classes;target\test-classes;target\dependency\*" `
     br.usp.each.saeg.jaguar.core.cli.JaguarRunner `
     -p . -c target\classes -t target\test-classes -tf tests.txt `
     -h Ochiai -ot F -o manual -l DEBUG 2>&1 | Tee-Object jaguar.log
```

O relatório fica em `.jaguar/manual.xml`. **Se a JVM terminar sem gerar o XML, isso é um resultado:** anote o último teste que aparece em `jaguar.log` e prossiga com o pipeline (parte B), que identifica o teste e repete sem ele.

### A4. Comparar os testes

```bash
# bash: testes executados e testes que falham no Jaguar
grep -E "Test .* : (Passed|Failed)" jaguar.log | sort -u | wc -l
grep -E "Test .* : Failed" jaguar.log | sort -u
```

```powershell
# PowerShell
(Select-String -Path jaguar.log -Pattern "Test .* : (Passed|Failed)" | ForEach-Object Line | Sort-Object -Unique).Count
Select-String -Path jaguar.log -Pattern " : Failed" | ForEach-Object Line | Sort-Object -Unique
```

Compare com o resultado da etapa A2: o total de testes e o conjunto dos que falham.

### A5. O rank que o Jaguar fornece

1. **Descubra as linhas do defeito**, que não saem do ranking e sim do commit de correção. O diff mostra, à esquerda, a versão com defeito:

   ```bash
   git diff -U0 HEAD 6ab41ee8 -- '*.java' ':(exclude)*/src/test/*'
   ```

   Os trechos `@@ -a,b +c,d @@` dão as linhas `a` a `a+b-1` da versão com defeito. Se `b` for 0, a correção só **adicionou** código: não há linha defeituosa propriamente dita, e as linhas vizinhas (`a` e `a+1`) são o alvo. Registre isso no relatório.

2. **Leia o rank no relatório do Jaguar.** O script abaixo usa só a biblioteca padrão do Python e mostra o valor, a posição e o rank com empates:

   ```python
   import xml.etree.ElementTree as ET
   R = [(e.get("name"), int(e.get("location")), float(e.get("suspicious-value")))
        for e in ET.parse(".jaguar/manual.xml").getroot().iter("requirements")]
   alvo = ("<classe do defeito>", <linha>)
   v = next(r[2] for r in R if r[:2] == alvo)
   print(len(R), "linhas | valor", v,
         "| melhor rank", 1 + sum(r[2] > v for r in R),
         "| pior rank", sum(r[2] >= v for r in R))
   ```

   Reporte o **melhor e o pior rank**, não só um número: muitas linhas costumam empatar com o mesmo valor.

---

## B. Pelo pipeline (JFLP)

```bash
python -m sbfl run --benchmark bugsjar --project maven --bug bugs-dot-jar_MNG-5742_6ab41ee8 --force
python -m sbfl compare bugs-dot-jar_MNG-5742_6ab41ee8 --benchmark bugsjar
```

O `run` executa o Jaguar (e se a JVM cair, identifica o teste culpado e repete sem ele). O `compare` refaz o checkout e o build do mesmo módulo, executa o `mvn test` e compara com a última tentativa do Jaguar. Ele imprime e grava `compare.md` e `compare.json` na pasta de resultados do bug (`maven_test.log` guarda a saída do Maven). O relatório traz:

- testes por situação (passam, falham, erro, ignorados) no Maven e no Jaguar;
- testes que só o Maven ou só o Jaguar executou, e testes com resultado diferente;
- classes que o Jaguar precisou excluir;
- para cada linha do defeito: posição no arquivo do Jaguar, `suspicious-value`, rank (melhor e pior caso), nº de linhas empatadas e `cef/cep/cnf/cnp`.

**Diferenças esperadas.** O pipeline não executa classes abstratas nem classes só JUnit 5, e exclui classes que derrubam a JVM. Por isso "só no Maven" pode ser maior que zero. Isso deve ser explicado no relatório, não escondido.

## C. O que registrar

| | Sem Jaguar (Maven) | Com Jaguar |
|---|---|---|
| Testes executados | | |
| Passam / falham / erro | | |
| Testes que falham | | |
| Classes excluídas ou JVM encerrada | | |

| Linha do defeito (classe:linha) | `suspicious-value` | Melhor rank | Pior rank | Linhas empatadas | Total de linhas |
|---|---:|---:|---:|---:|---:|
| | | | | | |

Confira também se a execução manual (parte A) e a do pipeline (parte B) dão o mesmo valor para a mesma linha. Se não derem, a diferença é um achado.
