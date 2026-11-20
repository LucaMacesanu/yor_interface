"""Front-hub routes: one UI for every host. The browser only talks to this hub; requests
for a peer are proxied to that peer's hub with the peer's own token (never sent to the
browser). See PI.md "Shared API contract" (v0.2, multi-host).
"""
from __future__ import annotations

import asyncio
import time
import urllib.error
import urllib.request

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, Response

PROXY_TIMEOUT = 60.0  # a stop can take a service's stop_timeout (collection: 30 s)
FWD_REQ_HEADERS = ("content-type", "if-match")


def mount(app: FastAPI, mgr, authed, req_token) -> None:
    self_name = mgr.host["name"]

    def peer(host: str) -> dict | None:
        return mgr.peers.get(host)

    @app.get("/api/hosts")
    async def hosts():
        out = [{"name": self_name, "label": mgr.host.get("label", self_name), "url": None,
                "online": True, "detail": "this hub", "self": True}]
        for name, p in mgr.peers.items():
            out.append({"name": name, "label": p.get("label", name), "url": p["url"],
                        "online": p.get("online"), "detail": p.get("detail"), "self": False})
        return out

    @app.get("/api/state/all")
    async def state_all():
        hosts = {self_name: {**mgr.state_json(), "online": True, "self": True}}
        for name, p in mgr.peers.items():
            st = mgr.peer_states.get(name)
            if st and p.get("online"):
                hosts[name] = {**st, "online": True, "self": False, "age_s": round(time.time() - p.get("seen", 0), 1)}
            else:
                hosts[name] = {"host": name, "label": p.get("label", name), "online": False,
                               "self": False, "detail": p.get("detail") or "connecting…"}
        return {"self": self_name, "order": list(hosts), "hosts": hosts}

    def _forward(url: str, method: str, data: bytes | None, headers: dict) -> tuple[int, bytes, str]:
        req = urllib.request.Request(url, data=data if method not in ("GET", "HEAD") else None,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT) as r:
                return r.status, r.read(), r.headers.get("content-type", "application/json")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("content-type", "application/json")

    @app.api_route("/api/hosts/{host}/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def proxy(host: str, rest: str, request: Request):
        if host == self_name:
            q = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"/api/{rest}{q}", status_code=307)
        p = peer(host)
        if not p:
            return JSONResponse({"error": f"unknown host {host}"}, 404)
        url = p["url"].rstrip("/") + "/api/" + rest
        if request.url.query:
            url += "?" + request.url.query
        headers = {k: v for k, v in request.headers.items() if k.lower() in FWD_REQ_HEADERS}
        headers["Authorization"] = f"Bearer {mgr.peer_token(host)}"
        data = await request.body()
        try:
            status, body, ctype = await asyncio.to_thread(_forward, url, request.method, data, headers)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"{host} hub unreachable: {e}"}, 502)
        if status == 401:  # the peer refused OUR token; don't make the browser think it's logged out
            return JSONResponse({"error": f"{host} hub rejected the Pi's token "
                                          f"(check {p.get('token_file', 'token_file')})"}, 502)
        if request.method != "GET" and rest.startswith(("services/", "agents")):
            asyncio.create_task(mgr._poll_peer(host, p))  # refresh the cached state right away
        return Response(body, status, media_type=ctype)

    @app.websocket("/ws/hosts/{host}/term/{window}")
    async def term_proxy(ws: WebSocket, host: str, window: str):
        if not authed(req_token(ws.headers, ws.cookies)):
            await ws.close(code=4401)
            return
        if host == self_name:
            from . import terminal
            await terminal.bridge(ws, mgr.tmux.session, window)
            return
        p = peer(host)
        await ws.accept()
        if not p:
            await ws.send_bytes(f"\r\n[yor_hub] unknown host {host}\r\n".encode())
            await ws.close()
            return
        from urllib.parse import quote
        from websockets.asyncio.client import connect
        url = p["url"].replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
        url += f"/ws/term/{quote(window, safe='')}"
        try:
            upstream = await asyncio.wait_for(
                connect(url, additional_headers={"Authorization": f"Bearer {mgr.peer_token(host)}"},
                        max_size=None, ping_interval=20), 5)
        except Exception as e:  # noqa: BLE001
            await ws.send_bytes(f"\r\n[yor_hub] can't reach {host} terminal: {e}\r\n".encode())
            await ws.close()
            return

        async def down():
            async for m in upstream:
                if isinstance(m, bytes):
                    await ws.send_bytes(m)
                else:
                    await ws.send_text(m)

        async def up():
            while True:
                m = await ws.receive_text()
                await upstream.send(m)

        tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await upstream.close()
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
