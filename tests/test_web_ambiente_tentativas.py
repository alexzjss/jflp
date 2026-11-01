from __future__ import annotations

import unittest

from web_helpers import HAVE_WEB, WebFixture

from sbfl.home import load_settings


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class TestAmbienteTentativasJaguar(WebFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for p in self.no_tools():
            p.start()
            self.addCleanup(p.stop)
        self.c = self.authed()

    def post(self, valor, **extra):
        data = {"workers": "4", "work_dir": "", "min_livre_gb": "5", **extra}
        if valor is not None:
            data["max_tentativas_jaguar"] = valor
        return self.c.post("/ambiente/cobertura", data=data, follow_redirects=False)

    def test_form_mostra_o_campo_com_o_padrao_e_a_nota(self):
        html = self.c.get("/ambiente").text
        self.assertIn('name="max_tentativas_jaguar"', html)
        self.assertIn("Tentativas por bug nos Jaguar (1 a 8)", html)
        self.assertIn('value="4"', html)
        self.assertIn("O Maven continua com até 8 (perfis legados).", html)

    def test_salva_no_config_toml(self):
        r = self.post("2")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.settings.get("cobertura", "max_tentativas_jaguar"), 2)
        self.assertEqual(load_settings(self.home).get("cobertura", "max_tentativas_jaguar"), 2)

    def test_fora_da_faixa_da_erro_e_mantem_o_digitado(self):
        for ruim in ("0", "9", "abc"):
            r = self.post(ruim)
            self.assertEqual(r.status_code, 400, ruim)
            self.assertIn("As tentativas por bug vão de 1 a 8", r.text)
            self.assertIn(f'name="max_tentativas_jaguar" min="1" max="8" value="{ruim}"', r.text)
        self.assertEqual(self.settings.get("cobertura", "max_tentativas_jaguar"), 4)

    def test_sem_o_campo_mantem_o_valor_atual(self):
        self.settings.set("cobertura", "max_tentativas_jaguar", 3)
        self.assertEqual(self.post(None).status_code, 303)
        self.assertEqual(self.settings.get("cobertura", "max_tentativas_jaguar"), 3)


if __name__ == "__main__":
    unittest.main()
