from __future__ import annotations

import unittest
from unittest import mock

from web_helpers import HAVE_WEB, WebFixture

from sbfl import sincronia
from sbfl.home import load_settings

URL = "https://hub.example/exec"


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx não instalados")
class TestSincroniaWeb(WebFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for p in self.no_tools():
            p.start()
            self.addCleanup(p.stop)
        self.c = self.authed()

    def com_sincronizador(self, **status):
        sinc = mock.Mock()
        base = {"ultimo_puxar": None, "aplicadas": 0, "pacotes_pendentes": 0, "reservas_remotas": 0,
                "erro": None, "hub_antigo": False, "recusadas": []}
        sinc.status.return_value = {**base, **status}
        self.app.state.sincronizador = sinc
        return sinc

    def com_url(self):
        self.settings.set("planilha", "url", URL)
        self.settings.save()

    def test_sem_sincronizador_a_pagina_abre(self):
        r = self.c.get("/ambiente")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sincronização entre computadores", r.text)
        self.assertIn("não está rodando nesta sessão", r.text)
        self.assertNotIn("Sincronizar agora", r.text)
        r = self.c.post("/ambiente/planilha/sincronizar", follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    def test_mostra_estado(self):
        self.com_url()
        self.com_sincronizador(aplicadas=3, pacotes_pendentes=2, reservas_remotas=4,
                               ultimo_puxar="2026-11-05T10:00:00")
        html = self.c.get("/ambiente").text
        self.assertIn("3 execuções recebidas", html)
        self.assertIn("2 pacotes a enviar", html)
        self.assertIn("4 bugs reservados por outros computadores", html)
        self.assertIn("Sincronizar agora", html)
        self.assertIn("Sincronizar com outros computadores", html)

    def test_hub_antigo_avisa(self):
        self.com_url()
        self.com_sincronizador(hub_antigo=True, erro=sincronia.AVISO_ANTIGO)
        html = self.c.get("/ambiente").text
        self.assertIn("Nova versão", html)
        self.assertIn("atualize-o", html)

    def test_mostra_erro(self):
        self.com_url()
        self.com_sincronizador(erro="rede caiu")
        self.assertIn("erro: rede caiu", self.c.get("/ambiente").text)

    def test_salva_nome_e_sincronizar(self):
        r = self.c.post("/ambiente/planilha/maquina", data={"maquina_nome": "  pc   lab ", "sincronizar": "1"},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(load_settings(self.home).get("planilha", "maquina_nome"), "pc lab")
        self.assertIs(load_settings(self.home).get("planilha", "sincronizar"), True)
        self.assertIn('value="pc lab"', self.c.get("/ambiente").text)
        self.c.post("/ambiente/planilha/maquina", data={"maquina_nome": "pc lab"}, follow_redirects=False)
        self.assertIs(load_settings(self.home).get("planilha", "sincronizar"), False)

    def test_sincronizar_agora_forca(self):
        sinc = self.com_sincronizador()
        r = self.c.post("/ambiente/planilha/sincronizar", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        sinc.acordar.assert_called_with(forcar=True)

    def test_painel_da_cobertura_mostra_bugs_em_outra_maquina(self):
        from sbfl.web import routes_cobertura as rc
        self.com_url()
        self.app.state.enviador = mock.Mock()
        self.app.state.enviador.status.return_value = {"configurada": True, "pendentes": 0, "erro": None,
                                                       "ultimo_ok": None, "falhas_linha": 0}
        self.com_sincronizador(reservas_remotas=2)
        req = mock.Mock(app=self.app)
        self.assertEqual(rc._planilha(req)["remotas"], 2)
        self.app.state.sincronizador = None
        self.assertEqual(rc._planilha(req)["remotas"], 0)


if __name__ == "__main__":
    unittest.main()
