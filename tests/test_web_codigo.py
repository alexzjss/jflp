from __future__ import annotations

import io
import unittest
import zipfile

from sbfl import store
from tests.test_web_runs import BUG, RunsBase
from tests.web_helpers import HAVE_WEB

SRC_B = "package a;\npublic class B {\n  void f() { /* <script>alert(1)</script> */ }\n  int g;\n}\n"
CSV = "a/B,3,3,0,1,5,0.9\na/B$In,4,1,2,1,5,0.2\na/C,1,0,3,0,6,0.0\n"


def checkout(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, c in files.items():
            zf.writestr(n, c)
    return buf.getvalue()


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class Codigo(RunsBase):
    def with_checkout(self, rel: str, files: dict):
        (store.run_dir(self.home, rel) / "checkout.zip").write_bytes(checkout(files))

    def test_renders_highlight_anchors_and_escapes(self):
        rel = self.make_run(csv=CSV)
        self.with_checkout(rel, {"m/src/main/java/a/B.java": SRC_B})
        r = self.authed().get(f"/run/{rel}/codigo")            # sem classe: a de maior susp
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="L3"', r.text)
        self.assertIn('id="L5"', r.text)
        self.assertIn('com-score', r.text)
        self.assertIn("cef 3", r.text)                          # tooltip
        self.assertIn("0.900 · 3/1", r.text)                    # margem susp e cef/cep
        self.assertNotIn("<script>alert(1)", r.text)
        self.assertIn("&lt;script&gt;alert(1)", r.text)
        self.assertIn('getElementById("L3")', r.text)           # rolagem até a mais suspeita
        self.assertIn("classe=a.C", r.text)                     # lista lateral
        self.assertEqual(r.text.count("com-score"), 2)          # linhas 3 e 4 (interna)
        # linha 1 (a.C) não é desta classe: sem cor
        self.assertNotIn('id="L1" style', r.text)

    def test_class_param_inner_maps_to_outer_and_not_found_shows_table(self):
        rel = self.make_run(csv=CSV)
        self.with_checkout(rel, {"m/src/main/java/a/B.java": SRC_B})
        c = self.authed()
        self.assertIn('id="L4"', c.get(f"/run/{rel}/codigo", params={"classe": "a.B$In"}).text)
        r = c.get(f"/run/{rel}/codigo", params={"classe": "a.C"})     # C.java não existe no checkout
        self.assertEqual(r.status_code, 200)
        self.assertIn("não foi encontrada", r.text)
        self.assertIn("<td>1</td>", r.text)                           # tabela de linhas da classe

    def test_missing_checkout_message_with_table(self):
        rel = self.make_run(csv=CSV)
        r = self.authed().get(f"/run/{rel}/codigo?classe=a.B")
        self.assertEqual(r.status_code, 200)
        self.assertIn("não guardou o checkout", r.text)
        self.assertIn("0.9000", r.text)

    def test_invalid_rel_404(self):
        c = self.authed()
        self.assertEqual(c.get("/run/bugsjar/x/1/nada/codigo").status_code, 404)
        self.assertEqual(c.get("/run/a/../b/1/x/codigo").status_code, 404)
        self.assertEqual(c.get("/run/bugsjar/%2e%2e/x/1/codigo").status_code, 404)

    def test_run_without_lines(self):
        w = store.RunWriter(self.home, BUG, "maven")
        w.finish({"status": "ok", "diagnosis": {}, "attempts": 1, "attempt_configs": [], "warnings": []})
        c = self.authed()
        r = c.get(f"/run/{w.rel}/codigo")
        self.assertEqual(r.status_code, 200)
        self.assertIn("não tem pontuações", r.text)
        self.assertNotIn("Ver no código", c.get(f"/run/{w.rel}").text)

    def test_links_from_run_page_and_lines_table(self):
        rel = self.make_run(csv=CSV)
        c = self.authed()
        page = c.get(f"/run/{rel}").text
        self.assertIn(f"/run/{rel}/codigo", page)
        self.assertIn("Ver no código", page)
        self.assertIn(f"/codigo?classe=a.B#L3", page)
        self.assertIn("classe=a.B%24In#L4", page)


if __name__ == "__main__":
    unittest.main()
