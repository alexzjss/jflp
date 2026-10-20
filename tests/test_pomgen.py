import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from sbfl.home import Home, load_settings
from sbfl.pomgen import FIXES, PomPlan
from sbfl.pomgen.generate import generate, write
from sbfl.pomgen.pom import chain

FIX = Path(__file__).parent / "fixtures" / "poms"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}
LISTENER = "br.usp.each.saeg.jaguar2.junit.JaguarJUnitRunListener"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = load_settings(Home(self.root / "home"))

    def module(self, fixture, sub=None, name="pom.xml"):
        d = self.root / sub if sub else self.root
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIX / fixture, d / name)
        return d

    def gen(self, d, jdk=11, **kw):
        return generate(d, self.settings, jdk, **kw)

    @staticmethod
    def parse(plan):
        return ET.fromstring(plan.text)

    @staticmethod
    def plugins(root, artifact):
        return [p for p in root.findall("m:build/m:plugins/m:plugin", NS)
                if p.findtext("m:artifactId", namespaces=NS) == artifact]


class TestPomgen(Base):
    def test_base_injection_minimal_pom(self):
        plan = self.gen(self.module("minimal.xml"))
        self.assertIsInstance(plan, PomPlan)
        root = self.parse(plan)
        deps = {d.findtext("m:artifactId", namespaces=NS): d for d in
                root.findall("m:dependencies/m:dependency", NS)
                if d.findtext("m:groupId", namespaces=NS) == "br.usp.each.saeg"}
        self.assertEqual(set(deps), {"jaguar2-junit4", "jaguar2-jacoco-provider", "jaguar2-core",
                                     "jaguar2-xml-exporter", "jaguar2-csv-exporter"})
        for d in deps.values():
            self.assertEqual(d.findtext("m:version", namespaces=NS), "0.0.2")
            self.assertEqual(d.findtext("m:scope", namespaces=NS), "test")
        jac = self.plugins(root, "jacoco-maven-plugin")
        self.assertEqual(len(jac), 1)
        self.assertEqual(jac[0].findtext("m:version", namespaces=NS), "0.8.7")
        self.assertEqual(jac[0].findtext("m:executions/m:execution/m:goals/m:goal", namespaces=NS),
                         "prepare-agent")
        sf = self.plugins(root, "maven-surefire-plugin")
        self.assertEqual(len(sf), 1)
        self.assertEqual(sf[0].findtext("m:version", namespaces=NS), "3.5.3")
        self.assertEqual(sf[0].findtext("m:configuration/m:properties/m:property/m:value",
                                        namespaces=NS), LISTENER)
        props = sf[0].find("m:configuration/m:properties", NS)
        self.assertEqual(props.get("combine.children"), "append")
        self.assertNotIn("ns0:", plan.text)
        self.assertTrue(plan.text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n'))
        self.assertIn("xsi:schemaLocation", plan.text)

    def test_original_untouched(self):
        d = self.module("argline.xml")
        before = (d / "pom.xml").read_bytes()
        plan = self.gen(d)
        out = write(plan, d)
        self.assertEqual(out, d / "jaguar2-pom.xml")
        self.assertTrue(out.exists())
        self.assertEqual((d / "pom.xml").read_bytes(), before)

    def test_existing_argline_preserved(self):
        plan = self.gen(self.module("argline.xml"))
        root = self.parse(plan)
        sf = self.plugins(root, "maven-surefire-plugin")
        self.assertEqual(len(sf), 1)
        self.assertEqual(sf[0].findtext("m:configuration/m:argLine", namespaces=NS),
                         "@{argLine} -Xmx1g")
        values = [v.text for v in sf[0].findall("m:configuration/m:properties/m:property/m:value", NS)]
        self.assertEqual(len(values), 1)           # não duplica a propriedade listener
        self.assertIn("com.acme.OtherListener", values[0])
        self.assertIn(LISTENER, values[0])

    def test_argline_not_wrapped_twice(self):
        d = self.module("argline.xml")
        p = d / "pom.xml"
        p.write_text(p.read_text().replace("-Xmx1g", "@{argLine} -Xmx1g"), encoding="utf-8")
        root = self.parse(self.gen(d))
        self.assertEqual(self.plugins(root, "maven-surefire-plugin")[0]
                         .findtext("m:configuration/m:argLine", namespaces=NS), "@{argLine} -Xmx1g")

    def test_surefire_only_in_pluginmanagement(self):
        plan = self.gen(self.module("pm_only.xml"))
        root = self.parse(plan)
        sf = self.plugins(root, "maven-surefire-plugin")
        self.assertEqual(len(sf), 1)
        self.assertEqual(sf[0].findtext("m:version", namespaces=NS), "3.5.3")
        managed = root.findall("m:build/m:pluginManagement/m:plugins/m:plugin", NS)
        self.assertEqual(managed[0].findtext("m:version", namespaces=NS), "2.12.4")

    def test_detect_animal_sniffer_in_parent(self):
        self.module("parent_sniffer.xml")
        d = self.module("child_sniffer.xml", "child")
        self.assertEqual(len(chain(d)), 2)
        plan = self.gen(d)
        self.assertIn("skip-animal-sniffer", plan.detected)
        self.assertIn("skip-animal-sniffer", plan.applied)
        sn = self.plugins(self.parse(plan), "animal-sniffer-maven-plugin")
        self.assertEqual(len(sn), 1)
        self.assertEqual(sn[0].findtext("m:configuration/m:skip", namespaces=NS), "true")
        self.assertEqual(sn[0].findtext("m:version", namespaces=NS), "1.15")

    def test_chain_missing_parent_ends_quietly(self):
        d = self.module("child_sniffer.xml", "solo")
        self.assertEqual(chain(d), [d / "pom.xml"])
        self.assertEqual(chain(self.root / "nada"), [])

    def test_source_dirs_detects_src_test(self):
        d = self.module("minimal.xml")
        t = d / "src" / "test" / "org" / "x"
        t.mkdir(parents=True)
        (t / "FooTest.java").write_text("package org.x;\nclass FooTest {}\n", encoding="utf-8")
        plan = self.gen(d)
        self.assertIn("source-dirs", plan.applied)
        self.assertEqual(self.parse(plan).findtext("m:build/m:testSourceDirectory", namespaces=NS),
                         "src/test")
        self.assertTrue((t / "FooTest.java").exists())

    def test_source_dirs_standard_layout_not_detected(self):
        d = self.module("minimal.xml")
        t = d / "src" / "test" / "java" / "org" / "x"
        t.mkdir(parents=True)
        (t / "FooTest.java").write_text("package org.x;\nclass FooTest {}\n", encoding="utf-8")
        self.assertNotIn("source-dirs", self.gen(d).detected)

    def test_fix_toggle_off(self):
        self.module("parent_sniffer.xml")
        d = self.module("child_sniffer.xml", "child")
        p = d / "pom.xml"
        p.write_text(p.read_text().replace("1.8", "1.5"), encoding="utf-8")
        on = self.gen(d)
        self.assertIn("compiler-java", on.applied)
        comp = self.plugins(self.parse(on), "maven-compiler-plugin")[0]
        self.assertEqual(comp.findtext("m:version", namespaces=NS), "3.8.0")
        self.assertEqual(comp.findtext("m:configuration/m:source", namespaces=NS), "11")
        self.assertEqual(comp.findtext("m:configuration/m:encoding", namespaces=NS), "UTF-8")
        off = self.gen(d, fixes_off={"compiler-java"})
        self.assertIn("compiler-java", off.detected)
        self.assertNotIn("compiler-java", off.applied)
        self.assertEqual(self.plugins(self.parse(off), "maven-compiler-plugin"), [])

    def test_fixes_on_explicit_and_params(self):
        d = self.module("minimal.xml")
        plan = self.gen(d, fixes_on={"override-version", "exclude-duplicates"}, params={
            "override-version": {"deps": {"org.x:y": "2.0"}, "plugins": {"org.x:p": "3.0"}},
            "exclude-duplicates": {"files": ["**/Dup.java"]}})
        self.assertEqual(plan.applied, ["override-version", "exclude-duplicates"])
        root = self.parse(plan)
        self.assertEqual(self.plugins(root, "p")[0].findtext("m:version", namespaces=NS), "3.0")
        comp = self.plugins(root, "maven-compiler-plugin")[0]
        self.assertEqual(comp.findtext("m:configuration/m:excludes/m:exclude", namespaces=NS),
                         "**/Dup.java")
        self.assertNotIn("junit4", plan.applied)

    def test_diff_is_semantic(self):
        plan = self.gen(self.module("weird.xml"), jdk=11)
        changed = [l for l in plan.diff.splitlines()
                   if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        self.assertTrue(changed)
        self.assertTrue(all(l.startswith("+") for l in changed), changed)   # só adições
        self.assertTrue(plan.diff.startswith("--- pom.xml\n+++ jaguar2-pom.xml"))
        self.assertIn("um comentario no meio", plan.text)

    def test_junit5_only_warning(self):
        plan = self.gen(self.module("junit5.xml"))
        self.assertIn("projeto só tem JUnit 5: o Jaguar 2 só roda testes JUnit 4", plan.warnings)

    def test_no_pom(self):
        d = self.root / "vazio"
        d.mkdir()
        plan = self.gen(d)
        self.assertIn("sem pom.xml: Jaguar 2 não suportado", plan.warnings)
        self.assertEqual(plan.text, "")

    # ---------------------------------------------------------------- herança (Review Focus 4)
    def _child_of(self, parent_fixture):
        self.module(parent_fixture)                 # pai em ../pom.xml
        return self.module("child_plain.xml", "child")

    def _listener_values(self, plan):
        root = self.parse(plan)
        out = []
        for sf in self.plugins(root, "maven-surefire-plugin"):
            props = sf.find("m:configuration/m:properties", NS)
            out.append((props.get("combine.children"),
                        [v.text for v in props.findall("m:property[m:name='listener']/m:value", NS)]))
        return out

    def test_listener_inherited_from_parent(self):
        d = self._child_of("parent_listener.xml")
        (combine, values), = self._listener_values(self.gen(d))
        self.assertEqual(values, ["com.acme.ParentListener," + LISTENER])
        self.assertIsNone(combine)

    def test_listener_in_pluginmanagement(self):
        d = self._child_of("parent_pm_listener.xml")
        (combine, values), = self._listener_values(self.gen(d))
        self.assertEqual(values, ["com.acme.ManagedListener," + LISTENER])
        self.assertIsNone(combine)

    def test_listener_already_has_ours(self):
        d = self._child_of("parent_listener_ours.xml")
        plan = self.gen(d)
        for _, values in self._listener_values(plan):
            self.assertEqual(len(values), 0)             # nada a acrescentar no filho
        own = self.module("argline.xml", "own")
        p = own / "pom.xml"
        p.write_text(p.read_text().replace("com.acme.OtherListener",
                                           "com.acme.OtherListener," + LISTENER), encoding="utf-8")
        (_, values), = self._listener_values(self.gen(own))
        self.assertEqual(values[0].count(LISTENER), 1)

    def test_jacoco_prepare_agent_in_parent_not_duplicated(self):
        d = self._child_of("parent_jacoco.xml")
        root = self.parse(self.gen(d))
        jac, = self.plugins(root, "jacoco-maven-plugin")
        self.assertEqual(jac.findtext("m:version", namespaces=NS), "0.8.7")
        execs = jac.findall("m:executions/m:execution", NS)
        self.assertEqual([e.findtext("m:id", namespaces=NS) for e in execs], ["agent"])

    # ---------------------------------------------------------------- pastas de fontes
    def _java(self, d, rel, pkg):
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("package " + pkg + ";\nclass X {}\n", encoding="utf-8")

    def test_source_dirs_helpers_in_test_tree(self):
        d = self.module("minimal.xml")
        self._java(d, "src/main/java/org/x/Foo.java", "org.x")
        for n in ("MockFoo", "FakeBar", "Helper"):
            self._java(d, f"src/test/java/org/x/{n}.java", "org.x")
        self._java(d, "src/test/java/org/x/FooTest.java", "org.x")
        plan = self.gen(d)
        self.assertNotIn("source-dirs", plan.detected)
        root = self.parse(plan)
        self.assertIsNone(root.find("m:build/m:sourceDirectory", NS))

    def test_source_dirs_only_helpers_never_main(self):
        d = self.module("minimal.xml")
        self._java(d, "src/main/java/org/x/Foo.java", "org.x")
        self._java(d, "src/test/org/x/MockFoo.java", "org.x")
        root = self.parse(self.gen(d))
        self.assertEqual(root.findtext("m:build/m:testSourceDirectory", namespaces=NS), "src/test")
        self.assertIsNone(root.find("m:build/m:sourceDirectory", NS))

    def test_source_dirs_ignores_src_it(self):
        d = self.module("minimal.xml")
        for i in range(5):
            self._java(d, f"src/it/java/org/x/Foo{i}IT.java", "org.x")
            self._java(d, f"src/it/java/org/x/Test{i}.java", "org.x")
        self._java(d, "src/test/java/org/x/FooTest.java", "org.x")
        plan = self.gen(d)
        self.assertNotIn("source-dirs", plan.detected)

    # ---------------------------------------------------------------- erros
    def test_invalid_pom_returns_warning_plan(self):
        d = self.root / "ruim"
        d.mkdir()
        (d / "pom.xml").write_text("<project><oops>", encoding="utf-8")
        plan = self.gen(d)
        self.assertEqual(plan.text, "")
        self.assertTrue(plan.warnings[0].startswith("pom.xml inválido:"))
        with self.assertRaises(ValueError):
            write(plan, d)

    def test_override_version_dep_requires_group(self):
        d = self.module("minimal.xml")
        plan = self.gen(d, fixes_on={"override-version"},
                        params={"override-version": {"deps": {"semgrupo": "1.0"}}})
        self.assertEqual(plan.applied, [])
        self.assertTrue(any("grupo:artefato" in w for w in plan.warnings))
        self.assertNotIn("semgrupo", plan.text)

    def test_fixes_registry(self):
        self.assertEqual(list(FIXES), ["skip-animal-sniffer", "compiler-java", "source-dirs",
                                       "junit4", "override-version", "exclude-duplicates",
                                       "skip-rat"])

    def test_skip_rat_detected_and_applied(self):
        self.module("parent_sniffer.xml")
        d = self.module("child_sniffer.xml", "child")
        self.assertNotIn("skip-rat", self.gen(d).detected)
        rat = ("<plugin><groupId>org.apache.rat</groupId><artifactId>apache-rat-plugin</artifactId>"
               "<version>0.10</version></plugin>")
        text = (d / "pom.xml").read_text(encoding="utf-8").replace("<build>", "<build><plugins>" + rat + "</plugins>", 1)
        if "<plugins>" + rat not in text:
            text = text.replace("</project>", "<build><plugins>" + rat + "</plugins></build></project>")
        (d / "pom.xml").write_text(text, encoding="utf-8")
        plan = self.gen(d)
        self.assertIn("skip-rat", plan.detected)
        self.assertIn("skip-rat", plan.applied)
        p = self.plugins(self.parse(plan), "apache-rat-plugin")
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0].findtext("m:configuration/m:skip", namespaces=NS), "true")
        self.assertEqual(p[0].findtext("m:configuration/m:numUnapprovedLicenses", namespaces=NS), "999999")


if __name__ == "__main__":
    unittest.main()
