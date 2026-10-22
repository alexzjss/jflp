"""Repositório falso do Bugs.jar (modelado no maven/MNG-5742): raiz pom, módulo de distribuição sem código
(apache-maven), módulo com o defeito (maven-core) e a pasta `.bugs-dot-jar/` com patch e resultados."""
from __future__ import annotations

from pathlib import Path

ROOT_POM = ("<project><modelVersion>4.0.0</modelVersion><groupId>g</groupId><artifactId>maven</artifactId>"
            "<version>1</version><packaging>pom</packaging>"
            "<modules><module>maven-core</module><module>apache-maven</module></modules></project>")


def _child(name: str, packaging: str = "jar") -> str:
    return ("<project><modelVersion>4.0.0</modelVersion><parent><groupId>g</groupId>"
            f"<artifactId>maven</artifactId><version>1</version></parent><artifactId>{name}</artifactId>"
            f"<packaging>{packaging}</packaging></project>")


PATCH = """diff --git a/maven-core/src/main/java/org/x/Foo.java b/maven-core/src/main/java/org/x/Foo.java
index c815920..5704276 100644
--- a/maven-core/src/main/java/org/x/Foo.java
+++ b/maven-core/src/main/java/org/x/Foo.java
@@ -4,1 +4,0 @@ class Foo
-        // TODO velho
@@ -10,0 +10,3 @@ class Foo
+        for (int i = 0; i < 1; i++) {
+        }
diff --git a/maven-core/src/test/java/org/x/FooTest.java b/maven-core/src/test/java/org/x/FooTest.java
index 1111111..2222222 100644
--- a/maven-core/src/test/java/org/x/FooTest.java
+++ b/maven-core/src/test/java/org/x/FooTest.java
@@ -5,1 +5,1 @@ class FooTest
-        old();
+        novo();
"""

TEST_RESULTS = """[INFO] Building Maven Core 3.2.6-SNAPSHOT
Running org.x.FooTest
Tests run: 12, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.62 sec <<< FAILURE! - in org.x.FooTest
testA(org.x.FooTest)  Time elapsed: 0.067 sec  <<< FAILURE!
junit.framework.AssertionFailedError: expected:<1> but was:<null>
Running org.x.OkTest
Tests run: 2, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.1 sec - in org.x.OkTest
Running org.x.BoomTest
Tests run: 3, Failures: 0, Errors: 1, Skipped: 0, Time elapsed: 0.1 sec <<< FAILURE! - in org.x.BoomTest
Results :

Failed tests:
  FooTest.testA:340 expected:<1> but was:<null>

Tests in error:
  BoomTest.testB:12 NullPointer

Tests run: 17, Failures: 1, Errors: 1, Skipped: 0
"""


def make_bugsjar_repo(base: Path, patch: str = PATCH, results: str | None = TEST_RESULTS) -> Path:
    """Cria `base/maven` com `.git` (pasta vazia) e `.bugs-dot-jar/`; devolve a raiz do repositório."""
    repo = Path(base) / "maven"
    files = {"pom.xml": ROOT_POM,
             "apache-maven/pom.xml": _child("apache-maven", "pom"),
             "maven-core/pom.xml": _child("maven-core"),
             "maven-core/src/main/java/org/x/Foo.java": "package org.x;\nclass Foo {}\n",
             "maven-core/src/test/java/org/x/FooTest.java": "package org.x;\nclass FooTest {}\n",
             ".bugs-dot-jar/developer-patch.diff": patch}
    if results is not None:
        files[".bugs-dot-jar/test-results.txt"] = results
    for rel, text in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    (repo / ".git").mkdir()
    return repo


def ramo_de(bug_id: str) -> str:
    """Nome do ramo do bug no repositório falso (`bugs-dot-jar_<ID>_<sha8>`)."""
    import hashlib
    return f"bugs-dot-jar_{bug_id}_{hashlib.sha1(bug_id.encode()).hexdigest()[:8]}"


def make_bugsjar_git(base: Path, ids=("MNG-1", "MNG-2", "MNG-3"), atual: str | None = None) -> Path:
    """Repositório `base/maven` com git de verdade: `master` e um ramo `bugs-dot-jar_<ID>_<sha8>` por id, cada um com
    `.bugs-dot-jar/bug-report.yml` (Summary). `atual` (um id, ou None = master) é o ramo em uso; nomes via `ramo_de`."""
    import subprocess
    repo = make_bugsjar_repo(base)
    for x in (repo / ".git").iterdir():
        x.unlink() if x.is_file() else x.rmdir()
    (repo / ".git").rmdir()

    def g(*a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *a],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)

    g("init", "-q", "-b", "master")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    nomes = {}
    for i in ids:
        nome = ramo_de(i)
        nomes[i] = nome
        g("checkout", "-q", "-b", nome, "master")
        (repo / ".bugs-dot-jar" / "bug-report.yml").write_text(f"BugID: {i}\nSummary: resumo do {i}\n", encoding="utf-8")
        g("add", "-A")
        g("commit", "-q", "-m", i)
    g("checkout", "-q", nomes[atual] if atual else "master")
    return repo
