"""FastAPI app: REST + websocket terminal + static UI."""
from __future__ import annotations

import asyncio
import hmac
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import terminal
from .services import HubError, ServiceManager

VERSION = "0.1.0"
STATIC = Path(__file__).parent / "static"
COOKIE = "yor_hub_token"


def load_token() -> str:
    path = Path(os.environ.get("YOR_HUB_TOKEN_FILE", "~/.config/yor_hub/token")).expanduser()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(18) + "\n")
        path.chmod(0o600)
    return path.read_text().strip()


def create_app(config_path: str) -> FastAPI:
    cfg = yaml.safe_load(Path(config_path).read_text())
    mgr = ServiceManager(cfg)
    token = load_token()
    mgr._token = token
    auth_on = cfg.get("auth", True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await mgr.autostart()
        tasks = [asyncio.create_task(mgr.poll_forever(cfg.get("poll_period", 1.0))),
                 asyncio.create_task(mgr.poll_peers_forever())]
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="yor_hub", version=VERSION, lifespan=lifespan)

    def authed(token_in: str | None) -> bool:
        return (not auth_on) or (token_in is not None and hmac.compare_digest(token_in, token))

    def req_token(request_headers, cookies) -> str | None:
        h = request_headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return cookies.get(COOKIE)

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        q = request.query_params.get("token")
        if q is not None:
            if authed(q):
                resp = RedirectResponse(request.url.path or "/")
                resp.set_cookie(COOKIE, q, max_age=365 * 86400, httponly=True, samesite="lax")
                return resp
            return JSONResponse({"error": "bad token"}, 401)
        if request.url.path.startswith("/static/") or request.url.path == "/favicon.ico":
            return await call_next(request)
        if not authed(req_token(request.headers, request.cookies)):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, 401)
            return FileResponse(STATIC / "login.html")
        return await call_next(request)

    @app.exception_handler(HubError)
    async def hub_err(_: Request, e: HubError):
        return JSONResponse({"error": e.msg, **e.extra}, e.status)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/info")
    async def info():
        return {"host": cfg["host"]["name"], "label": cfg["host"].get("label"),
                "version": VERSION, "peers": cfg.get("peers", [])}

    @app.get("/api/state")
    async def state():
        return mgr.state_json()

    async def body(request: Request) -> dict:
        try:
            return await request.json()
        except Exception:  # noqa: BLE001
            return {}

    @app.post("/api/services/{sid}/start")
    async def start(sid: str, request: Request):
        b = await body(request)
        return await mgr.start(sid, args=b.get("args"), force=bool(b.get("force")))

    @app.post("/api/services/{sid}/stop")
    async def stop(sid: str):
        return await mgr.stop(sid)

    @app.post("/api/services/{sid}/restart")
    async def restart(sid: str, request: Request):
        b = await body(request)
        return await mgr.restart(sid, args=b.get("args"))

    @app.post("/api/services/{sid}/clear")
    async def clear(sid: str):
        return await mgr.clear(sid)

    @app.get("/api/services/{sid}/log")
    async def log(sid: str, lines: int = 500):
        return {"text": await asyncio.to_thread(mgr.log, sid, min(lines, 20000))}

    @app.post("/api/agents")
    async def agents(request: Request):
        b = await body(request)
        return await mgr.new_agent(cwd=b.get("cwd"))

    @app.websocket("/ws/term/{window}")
    async def term(ws: WebSocket, window: str):
        if not authed(req_token(ws.headers, ws.cookies)):
            await ws.close(code=4401)
            return
        await terminal.bridge(ws, mgr.tmux.session, window)

    if cfg.get("cycles"):  # Orin: cycle editor API (yor_hub/cycles.py)
        from . import cycles
        cycles.mount(app, cfg["cycles"], mgr)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
