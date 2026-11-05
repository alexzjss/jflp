from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sbfl import planilha as pl
from sbfl import sincronia as sc
from tests.fake_hub import FakeHub
from tests.test_sincronia import Maq, chave


class TestSincroniaDePonta(unittest.TestCase):
    """Duas máquinas, um hub: A conclui, o Enviador e o Sincronizador de A sobem, B puxa; B não devolve eco."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hub = FakeHub()
        self.a, self.b = Maq(self.tmp, "A"), Maq(self.tmp, "B")

    def enviador(self, m):
        return pl.Enviador(m.home, m.settings, post=self.hub, reconciliar_cada=None)

    def sincronizador(self, m):
        return sc.Sincronizador(m.home, m.settings, post=self.hub)

    def test_a_conclui_b_recebe_e_nao_ha_eco(self):
        rel_a = self.a.concluir(0, "jaguar1")
        pl.enfileirar(self.a.home, [chave(0)])          # o que o on_bug_change do worker faz
        self.assertEqual(sc.pacotes_pendentes(self.a.home), 1)
        self.enviador(self.a).ciclo()
        self.assertEqual(set(self.hub.sync[chave(0)]["dados"]), {"jaguar1"})
        self.assertEqual(self.hub.sync[chave(0)]["dados"]["jaguar1"]["run_rel"], rel_a)
        sinc_a = self.sincronizador(self.a)
        sinc_a.ciclo()
        self.assertEqual(sc.pacotes_pendentes(self.a.home), 0)
        self.assertIsNotNone(self.hub.sync[chave(0)]["dados"]["jaguar1"]["pacote"])
        self.assertEqual(sinc_a.status()["aplicadas"], 0)       # A não importa o que ela mesma produziu

        sinc_b = self.sincronizador(self.b)
        sinc_b.ciclo()
        j = self.b.job(0, "jaguar1")
        self.assertEqual((j["state"], j["origem"]), ("done", "remoto"))
        self.assertEqual(sinc_b.status()["aplicadas"], 1)

        # B envia a linha do bug (já enfileirada ao aplicar): o sync dela não leva a etapa remota
        seq = self.hub.seq
        self.enviador(self.b).ciclo()
        enviado = [c for c in self.hub.chamadas if c.get("maquina") == self.b.id and c.get("linhas")]
        self.assertTrue(enviado)
        self.assertNotIn(chave(0), enviado[-1].get("sync") or {})
        self.assertEqual(self.hub.seq, seq)
        self.assertEqual(self.hub.sync[chave(0)]["dados"]["jaguar1"]["maquina"], self.a.id)
        linha = json.loads(pl._ler(self.b.home, [chave(0)])[chave(0)][0])
        self.assertIn("A", linha[pl.COLUNAS.index("Máquina")])

        # e A puxando de novo não recebe nada de volta
        sinc_a.acordar(forcar=True)
        sinc_a.ciclo()
        self.assertEqual(sinc_a.status()["aplicadas"], 0)


if __name__ == "__main__":
    unittest.main()
