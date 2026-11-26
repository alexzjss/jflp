#!/usr/bin/env bash
# Runs the sample tests under the real Jaguar2 fork (JaCoCo provider) for comparison.
# $1 = JDK home, $2 = file listing the Jaguar2 runtime jars (one per line), $3 = Jaguar2 build dir
# (holding the module jars). Output goes to src/it/sample/target-jaguar2/target.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
jdk="$1"; cpfile="$2"; j2="$3"
work="$here/sample/target-jaguar2"
rm -rf "$work"; mkdir -p "$work/target"
"$here/run-sample.sh" "$jdk" "$work/jflp-build" >/dev/null 2>&1 || true
cp -r "$work/jflp-build/classes" "$work/jflp-build/test-classes" "$work/target/"
base="$(grep -v 'jaguar2-\(csv\|xml\)-exporter' "$cpfile" | tr '\n' ':')"
base="$base$(ls "$j2"/jaguar2-core/target/jaguar2-core-*-SNAPSHOT.jar "$j2"/jaguar2-api/target/jaguar2-api-*-SNAPSHOT.jar "$j2"/jaguar2-commons/target/jaguar2-commons-*-SNAPSHOT.jar "$j2"/jaguar2-junit4/target/jaguar2-junit4-*-SNAPSHOT.jar "$j2"/jaguar2-providers/jaguar2-jacoco-provider/target/jaguar2-jacoco-provider-*-SNAPSHOT.jar | grep -v sources | tr '\n' ':')"
agent="$(grep org.jacoco.agent "$cpfile")"
for fmt in csv xml; do
  exp="$(ls "$j2"/jaguar2-exporters/jaguar2-$fmt-exporter/target/jaguar2-$fmt-exporter-*-SNAPSHOT.jar | grep -v sources)"
  rm -f "$work/target/coverageMatrix.txt"
  ( cd "$work" && "$jdk/bin/java" "-javaagent:$agent=destfile=target/jacoco.exec" -Djaguar2.classesDirs=target/classes \
      -cp "target/classes:target/test-classes:$base$exp" \
      br.usp.each.saeg.jaguar2.junit.JaguarTestRunner sample.CalcTest sample.LegacyTest ) > "$work/junit-$fmt.log" 2>&1 || true
done
ls "$work/target"
