#!/usr/bin/env bash
# Runs the sample project's tests under the agent with the JDK in $1 (default: java on PATH)
# and writes the reports to $2 (default: src/it/sample/target).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
jdk="${1:-}"
java="${jdk:+$jdk/bin/}java"
javac="${jdk:+$jdk/bin/}javac"
m2="${M2_REPO:-$HOME/.m2/repository}"
cp_junit="$m2/junit/junit/4.12/junit-4.12.jar:$m2/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"
work="${2:-$here/sample/target}"
rm -rf "$work"
mkdir -p "$work/classes" "$work/test-classes"
"$javac" -nowarn -g -source 1.7 -target 1.7 -d "$work/classes" $(find "$here/sample/src/main/java" -name '*.java') 2>/dev/null \
  || "$javac" -nowarn -g -d "$work/classes" $(find "$here/sample/src/main/java" -name '*.java')
"$javac" -nowarn -g -cp "$work/classes:$cp_junit" -d "$work/test-classes" $(find "$here/sample/src/test/java" -name '*.java') 2>/dev/null \
  || "$javac" -nowarn -g -cp "$work/classes:$cp_junit" -d "$work/test-classes" $(find "$here/sample/src/test/java" -name '*.java')
( cd "$work/.." && "$java" "-javaagent:$root/target/jflp.jar=destdir=$work/jflp,classdirs=$work/classes" \
    -cp "$work/classes:$work/test-classes:$cp_junit" org.junit.runner.JUnitCore sample.CalcTest sample.LegacyTest ) > "$work/junit.log" 2>&1 || true
"$java" -jar "$root/target/jflp.jar" report --data "$work/jflp" --classes "$work/classes" --out "$work"
