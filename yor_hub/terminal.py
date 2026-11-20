"""Browser terminal: websocket <-> pty running a tmux client on a grouped session.

Each browser tab gets its own grouped session ("web-xxxx" sharing session "yor"'s
windows), so switching windows in one tab doesn't move anyone else's view.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import secrets
import signal
import struct
import subprocess
import termios

from fastapi import WebSocket, WebSocketDisconnect


def _set_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def bridge(ws: WebSocket, session: str, window: str) -> None:
    await ws.accept()
    web = f"web-{secrets.token_hex(3)}"
    target = f"={session}:={window}"
    if subprocess.run(["tmux", "has-session", "-t", target], capture_output=True).returncode != 0:
        await ws.send_bytes(f"\r\n[yor_hub] no tmux window {window}\r\n".encode())
        await ws.close()
        return
    argv = ["tmux", "new-session", "-t", f"={session}", "-s", web,
            ";", "set-option", "-t", web, "destroy-unattached", "on",
            ";", "set-option", "-t", web, "status", "off",
            ";", "select-window", "-t", f"{web}:={window}"]
    env = {**os.environ, "TERM": "xterm-256color"}
    env.pop("TMUX", None)
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execvpe("tmux", argv, env)
        finally:
            os._exit(127)

    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _on_readable() -> None:
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(fd)
            out_q.put_nowait(None)
        else:
            out_q.put_nowait(data)

    _set_size(fd, 30, 100)
    loop.add_reader(fd, _on_readable)

    async def pump_out() -> None:
        while True:
            data = await out_q.get()
            if data is None:
                break
            await ws.send_bytes(data)

    async def pump_in() -> None:
        while True:
            msg = await ws.receive_text()
            try:
                m = json.loads(msg)
            except ValueError:
                continue
            if m.get("t") == "i":
                os.write(fd, m["d"].encode())
            elif m.get("t") == "r":
                _set_size(fd, max(5, int(m["rows"])), max(20, int(m["cols"])))

    tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        try:
            loop.remove_reader(fd)
        except Exception:  # noqa: BLE001
            pass
        subprocess.run(["tmux", "kill-session", "-t", f"={web}"], capture_output=True)
        try:
            os.kill(pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            await asyncio.wait_for(asyncio.to_thread(os.waitpid, pid, 0), 3)
        except (ChildProcessError, asyncio.TimeoutError):
            pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
