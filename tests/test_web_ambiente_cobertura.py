from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path
from unittest import mock

from web_helpers import HAVE_WEB, WebFixture

from sbfl.home import load_settings


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class TestAmbienteCoberturaExtra(WebFixture, unittest.TestCase):
    """Seção Cobertura do Ambiente: espaço mínimo, Jaguar 1 em paralelo e pasta de trabalho em uso."""

    def setUp(self):
        super().setUp()
        for p in self.no_tools():
            p.start()
            self.addCleanup(p.stop)
        self.c = self.authed()

    def test_form_mostra_espaco_minimo_e_jaguar1_paralelo(self):
        html = self.c.get("/ambiente").text
        self.assertIn('name="min_livre_gb"', html)
        self.assertIn('value="5"', html)
        self.assertIn("Jaguar 1 em paralelo (o jar do Jaguar 1 aceita -Djaguar.port)", html)
        self.assertIn('name="jaguar1_paralelo"', html)
        self.assertIn("rodam um de cada vez", html)

    def test_salva_espaco_minimo_e_jaguar1_paralelo(self):
        r = self.c.post("/ambiente/cobertura", data={"workers": "4", "work_dir": "", "min_livre_gb": "20",
                                                     "jaguar1_paralelo": "on"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.settings.get("cobertura", "min_livre_gb"), 20)
        self.assertIs(self.settings.get("jaguar1", "port_aware"), True)
        # gravado no config.toml: outra leitura enxerga os mesmos valores
        again = load_settings(self.home)
        self.assertEqual(again.get("cobertura", "min_livre_gb"), 20)
        self.assertIs(again.get("jaguar1", "port_aware"), True)

    def test_jaguar1_paralelo_desmarcado_volta_a_serializado(self):
        self.settings.set("jaguar1", "port_aware", True)
        self.c.post("/ambiente/cobertura", data={"workers": "4", "work_dir": "", "min_livre_gb": "5"})
        self.assertIs(self.settings.get("jaguar1", "port_aware"), False)

    def test_espaco_minimo_fora_da_faixa_da_erro_e_mantem_o_digitado(self):
        for ruim in ("0", "501", "abc"):
            r = self.c.post("/ambiente/cobertura", data={"workers": "4", "work_dir": "", "min_livre_gb": ruim})
            self.assertEqual(r.status_code, 400, ruim)
            self.assertIn("O espaço mínimo vai de 1 a 500 GB", r.text)
            self.assertNotIn('"detail"', r.text)
            self.assertIn(f'value="{ruim}"', r.text)
        self.assertEqual(self.settings.get("cobertura", "min_livre_gb"), 5)

    def test_mostra_a_pasta_de_trabalho_em_uso(self):
        self.app.state.home = dataclasses.replace(self.home, work_root=Path("E:\\JFLP\\cobertura"))
        html = self.c.get("/ambiente").text
        self.assertIn("Pasta de trabalho em uso", html)
        self.assertIn("E:\\JFLP\\cobertura", html)

    def test_pasta_em_uso_sem_configuracao_e_a_do_jflp(self):
        html = self.c.get("/ambiente").text
        self.assertIn("Pasta de trabalho em uso", html)
        self.assertIn(str(self.home.work), html)


if __name__ == "__main__":
    unittest.main()
