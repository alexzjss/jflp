from __future__ import annotations

import json
import sys
import unittest
from unittest import mock

from tests.web_helpers import HAVE_WEB, WebFixture

if HAVE_WEB:
    from sbfl.web import comum


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class Recentes(WebFixture, unittest.TestCase):
    def test_grava_dedup_limita_e_ordena(self):
        for i in range(12):
            comum.add_recente(self.home, f"/p/{i}", f"p{i}", ["jaguar2"], [], True)
        comum.add_recente(self.home, "/p/5", "p5", ["maven"], ["a.B"], False)
        lst = comum.load_recentes(self.home)
        self.assertEqual(len(lst), 10)
        self.assertEqual(lst[0]["spec"], "/p/5")           # mais recente primeiro
        self.assertEqual(lst[0]["kinds"], ["maven"])
        self.assertEqual([r["spec"] for r in lst].count("/p/5"), 1)
        self.assertNotIn("/p/0", [r["spec"] for r in lst])
        self.assertTrue(lst[0]["data"])

    def test_arquivo_invalido_vira_lista_vazia(self):
        path = self.home.root / "recentes.json"
        for texto in ("{nao e json", '{"a": 1}', '[1, "x", {"nome": "sem spec"}]'):
            path.write_text(texto, encoding="utf-8")
            self.assertEqual(comum.load_recentes(self.home), [])
        comum.add_recente(self.home, "/p/1", "p", ["maven"], [], True)    # sobrescreve o lixo
        self.assertEqual(len(comum.load_recentes(self.home)), 1)
        json.loads(path.read_text(encoding="utf-8"))

    def test_kinds_validos_ordem_e_padrao(self):
        self.assertEqual(comum.kinds_validos(["maven", "jaguar2", "x"]), ["maven", "jaguar2"])
        self.assertEqual(comum.kinds_validos(["x"]), ["jaguar1", "maven"])


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class Seletor(WebFixture, unittest.TestCase):
    def test_botao_so_com_tkinter(self):
        c = self.authed()
        with mock.patch("sbfl.web.routes_rodar._tk_available", return_value=True):
            self.assertIn("/rodar/escolher", c.get("/rodar").text)
        with mock.patch("sbfl.web.routes_rodar._tk_available", return_value=False):
            self.assertNotIn("/rodar/escolher", c.get("/rodar").text)
            self.assertEqual(c.post("/rodar/escolher").status_code, 404)

    def test_escolher_devolve_campo_preenchido(self):
        c = self.authed()
        with mock.patch("sbfl.web.routes_rodar._tk_available", return_value=True), \
                mock.patch("sbfl.web.routes_rodar._pick_folder", return_value='C:\\Meus "Projetos"'):
            r = c.post("/rodar/escolher", data={"projeto": "antigo"})
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="projeto"', r.text)
        self.assertIn("C:\\Meus &quot;Projetos&quot;", r.text)
        with mock.patch("sbfl.web.routes_rodar._tk_available", return_value=True), \
                mock.patch("sbfl.web.routes_rodar._pick_folder", return_value=""):    # cancelou: mantém o digitado
            r = c.post("/rodar/escolher", data={"projeto": "antigo"})
        self.assertIn('value="antigo"', r.text)

    def test_pick_folder_usa_tkinter_topmost(self):
        tk, fd = mock.MagicMock(), mock.MagicMock()
        fd.askdirectory.return_value = "E:/proj"
        tk.filedialog = fd
        with mock.patch.dict(sys.modules, {"tkinter": tk, "tkinter.filedialog": fd}):
            self.assertEqual(comum._pick_folder(), "E:/proj")
        tk.Tk.return_value.attributes.assert_any_call("-topmost", True)
        tk.Tk.return_value.destroy.assert_called()


if __name__ == "__main__":
    unittest.main()
