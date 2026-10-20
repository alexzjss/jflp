"""App FastAPI local: só 127.0.0.1, com token em cookie e checagem de Host/Origin."""
from __future__ import annotations

import hmac
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import queue as jobs_queue
from .. import store
from ..home import Home, Settings, load_settings
from ..queue import Worker

COOKIE = "jflp_token"
_HERE = Path(__file__).parent
_LOCAL_HOSTS = ("127.0.0.1", "localhost")


def _deny(msg: str) -> Response:
    return Response(msg, status_code=403, media_type="text/plain; charset=utf-8")


def _same(a: str, b: str) -> bool:
    """Comparação em tempo constante que aceita texto não-ASCII."""
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


def _host_ok(host_header: str, port: int | None) -> bool:
    """Aceita só 127.0.0.1/localhost (e a porta do servidor), contra DNS rebinding."""
    try:
        parts = urlsplit("//" + host_header)
        name, hport = parts.hostname, parts.port
    except ValueError:
        return False
    if name not in _LOCAL_HOSTS:
        return False
    return port is None or hport == port


def create_app(home: Home, settings: Settings, worker: Worker, token: str,
               port: int | None = None) -> FastAPI:
    app = FastAPI(title="JFLP Studio", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.home, app.state.settings, app.state.worker = home, settings, worker
    app.state.token, app.state.port = token, port
    app.state.local_bugs = {}     # bugs locais cadastrados nesta sessão: {key: Bug}
    app.state.tasks = {}          # downloads e prefetch em andamento: {id: {...}}
    app.state.templates = Jinja2Templates(directory=str(_HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if not _host_ok(host, app.state.port):
            return _deny("Host não permitido.")
        origin = request.headers.get("origin")
        if request.method == "POST" and origin is not None and origin != f"http://{host}":
            return _deny("Origem não permitida.")
        if request.method == "GET" and request.url.path == "/" and "t" in request.query_params:
            if _same(request.query_params["t"], token):
                resp = RedirectResponse("/", status_code=303)
                resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")
                return resp
            return _deny("Token inválido.")
        if not _same(request.cookies.get(COOKIE, ""), token):
            return _deny("Acesso negado: abra o endereço impresso por 'jflp ui'.")
        return await call_next(request)

    def estado() -> dict:
        st = worker.status()
        return {"status": st, "current": st["current"],
                "pending": jobs_queue.list_jobs(home, state="pending", limit=50),
                "runs": store.list_runs(home)[:10]}

    @app.get("/", response_class=HTMLResponse)
    def painel(request: Request):
        return app.state.templates.TemplateResponse(request, "painel.html", {"page": "painel", **estado()})

    @app.get("/painel/estado", response_class=HTMLResponse)
    def painel_estado(request: Request):
        return app.state.templates.TemplateResponse(request, "_painel_estado.html", estado())

    from .routes_env import router as env_router
    app.include_router(env_router)
    from .routes_bugs import router as bugs_router
    app.include_router(bugs_router)
    return app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(home: Home, port: int = 0, open_browser: bool = True, window: bool = False) -> int:
    import uvicorn

    home.ensure()
    settings = load_settings(home)
    port = port or _free_port()
    token = secrets.token_urlsafe(24)
    worker = Worker(home, settings)
    app = create_app(home, settings, worker, token, port)
    url = f"http://127.0.0.1:{port}/?t={token}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="jflp-uvicorn", daemon=True)
    thread.start()
    while not server.started and thread.is_alive():
        time.sleep(0.05)
    if not server.started:
        print(f"erro: não foi possível iniciar o servidor em 127.0.0.1:{port}; "
              "a porta pode estar em uso (tente --port 0).", file=sys.stderr)
        return 1
    worker.start()
    print(f"JFLP Studio em {url}")
    try:
        opened = False
        if window:
            try:
                import webview
                webview.create_window("JFLP Studio", url)
                webview.start()
                opened = True
            except ImportError:
                print("pywebview não instalado; abrindo no navegador.")
        if not opened:
            if open_browser:
                webbrowser.open(url)
            while thread.is_alive():
                thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        worker.stop()
        thread.join(5)
    return 0
