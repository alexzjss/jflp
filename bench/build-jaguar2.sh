#!/usr/bin/env bash
# Builds the Jaguar2 fork (c3nt4ur1/jaguar2) for comparison runs and writes the
# class path needed to run its JUnit 4 runner with the JaCoCo provider and the
# CSV exporter.
#   bench/build-jaguar2.sh JDK11_HOME OUT_DIR
# Produces OUT_DIR/jaguar2 (checkout), OUT_DIR/jaguar2.classpath and
# OUT_DIR/jacocoagent.jar. The fork compiles to Java 11 bytecode, so a JDK 11+
# is required both to build and to run it.
set -euo pipefail
jdk="$1"; out="$(mkdir -p "$2" && cd "$2" && pwd)"
[ -d "$out/jaguar2" ] || git clone -q https://github.com/c3nt4ur1/jaguar2 "$out/jaguar2"
cd "$out/jaguar2"
mods=jaguar2-junit4,jaguar2-providers/jaguar2-jacoco-provider,jaguar2-exporters/jaguar2-csv-exporter
JAVA_HOME="$jdk" mvn -q -B install -DskipTests -Dmaven.javadoc.skip -Danimal.sniffer.skip -Dpmd.skip \
  -pl "$mods" -am
cp=""
for m in jaguar2-api jaguar2-commons jaguar2-core jaguar2-junit4 \
         jaguar2-providers/jaguar2-jacoco-provider jaguar2-exporters/jaguar2-csv-exporter; do
  cp="$cp:$(ls "$PWD/$m"/target/*-SNAPSHOT.jar | grep -v sources)"
done
dep="$(JAVA_HOME="$jdk" mvn -q -B -f jaguar2-providers/jaguar2-jacoco-provider/pom.xml \
  dependency:build-classpath -Dmdep.includeScope=runtime -Dmdep.outputFile=/dev/stdout 2>/dev/null \
  | grep -v JAVA_)"
# JUnit comes from the project under test; Mockito and friends leak from the
# fork's test scope and would clash with the project's own versions.
dep="$(echo "$dep" | tr ':' '\n' | grep -v -e '/junit/' -e '/hamcrest/' -e '/mockito/' -e '/bytebuddy/' -e '/objenesis/' | paste -sd: -)"
echo "${cp#:}:$dep" > "$out/jaguar2.classpath"
agent="$(echo "$dep" | tr ':' '\n' | grep 'org.jacoco.agent' | head -1)"
[ -n "$agent" ] || agent="$(ls ~/.m2/repository/org/jacoco/org.jacoco.agent/0.8.7/org.jacoco.agent-0.8.7-runtime.jar)"
cp "$agent" "$out/jacocoagent.jar"
echo "jaguar2 class path: $out/jaguar2.classpath"
