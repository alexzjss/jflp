from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi.testclient import TestClient
    from sbfl.web.app import create_app
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

from sbfl.adapters.base import Bug  # noqa: F401
from sbfl.home import Home, load_settings

TOKEN = "tok-de-teste"
BASE = "http://127.0.0.1:8123"


class WebFixture:
    """Cliente, token e home temporária; sem testes próprios (use com unittest.TestCase)."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Home(self.tmp / "home").ensure()
        self.settings = load_settings(self.home)
        worker = mock.Mock()
        worker.status.return_value = {"alive": True, "last_error": None, "current": None}
        self.worker = worker
        self.app = create_app(self.home, self.settings, worker, TOKEN, 8123)
        self.client = TestClient(self.app, base_url=BASE)

    def authed(self) -> TestClient:
        c = TestClient(self.app, base_url=BASE)
        c.cookies.set("jflp_token", TOKEN)
        return c

    def no_tools(self):
        wsl = mock.Mock(installed=False, distros=[], d4j=None, message="WSL indisponível")
        return [mock.patch("sbfl.web.routes_env.find_jdks", return_value=[]),
                mock.patch("sbfl.web.routes_env.find_mavens", return_value=[]),
                mock.patch("sbfl.web.routes_env.wsl_status", return_value=wsl),
                mock.patch("sbfl.web.routes_env._git", return_value=None),
                mock.patch("sbfl.toolchain.profiles.find_jdks", return_value=[]),
                mock.patch("sbfl.toolchain.profiles.find_mavens", return_value=[])]
