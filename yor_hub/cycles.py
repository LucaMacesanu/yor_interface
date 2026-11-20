"""Cycles API: list / add / edit / rename / delete the cycles collect.py reads.

Orin-owned. Mounted by app.py only when the host config has
    cycles: {file: ~/yor_ws/cycles.json, service: collection}
so other hosts don't get these routes.

File format = collect.py's load_cycles (same rules as the Pi's parse_announcement):
    {"<cycle name>": ["<subtask>", ...], ...}
where a subtask is a string or {"task": str, ...}. Dict subtasks keep their extra keys
as long as the client sends the dict back (GET returns entries exactly as stored).

Writes are atomic (tmp + fsync + rename), formatted like the existing file
(indent=2 + trailing newline). Before each write the current file is copied to
<file>.bak and to <history_dir>/<stem>_<stamp>.json (last `keep` kept), because
cycles.json is not under version control. `If-Match: <mtime>` (the `mtime` string
from GET) makes a stale write fail with 409 instead of clobbering someone else's edit.

    GET    /api/cycles               -> {file, mtime, cycles}
    PUT    /api/cycles/{name}        body {subtasks: [...], rename_to?: str} -> {file, mtime, cycles, note?}
    DELETE /api/cycles/{name}        -> {file, mtime, cycles, note?}
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request

from .services import HubError

MAX_NAME = 100


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise HubError(400, "cycle name must be a non-empty string")
    name = name.strip()
    if len(name) > MAX_NAME:
        raise HubError(400, f"cycle name longer than {MAX_NAME} characters")
    if any(ord(c) < 32 for c in name):
        raise HubError(400, "cycle name contains control characters")
    if "/" in name:
        raise HubError(400, "cycle name can't contain '/' (it goes in the URL)")
    if name.startswith("-"):
        raise HubError(400, "cycle name can't start with '-' (it would read as a flag)")
    return name


def validate_subtasks(name: str, subtasks: Any) -> list:
    """Same rules as collect.load_cycles. Strings are stripped; dicts keep extra keys,
    only their `task` is stripped."""
    if not isinstance(subtasks, list) or not subtasks:
        raise HubError(400, f"cycle {name!r} needs a non-empty list of subtasks")
    out = []
    for i, s in enumerate(subtasks):
        task = s.get("task") if isinstance(s, dict) else s
        if not isinstance(task, str) or not task.strip():
            raise HubError(400, f"cycle {name!r}: subtask {i + 1} has no task text")
        out.append({**s, "task": task.strip()} if isinstance(s, dict) else task.strip())
    return out


class CycleStore:
    def __init__(self, file: str, history_dir: str | None = None, keep: int = 50):
        self.path = Path(file).expanduser()
        self.history = Path(history_dir).expanduser() if history_dir else self.path.parent / ".cycles_history"
        self.keep = keep
        self._lock = threading.Lock()

    def _mtime(self) -> str:
        try:
            return str(self.path.stat().st_mtime_ns)
        except FileNotFoundError:
            return "0"

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as e:
            raise HubError(500, f"{self.path} is not valid JSON ({e}); fix it by hand or restore {self.path}.bak")
        if not isinstance(raw, dict):
            raise HubError(500, f"{self.path}: expected an object {{cycle name: [subtasks]}}")
        return raw

    def get(self) -> dict:
        with self._lock:
            return {"file": str(self.path), "mtime": self._mtime(), "cycles": self._load()}

    def _backup(self) -> None:
        if not self.path.exists():
            return
        shutil.copy2(self.path, self.path.with_name(self.path.name + ".bak"))
        self.history.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1_000_000_000:09d}"
        shutil.copy2(self.path, self.history / f"{self.path.stem}_{stamp}.json")
        old = sorted(self.history.glob(f"{self.path.stem}_*.json"))
        for p in old[:-self.keep] if self.keep > 0 else []:
            p.unlink(missing_ok=True)

    def _write(self, cycles: dict) -> None:
        self._backup()
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        data = json.dumps(cycles, indent=2, ensure_ascii=False) + "\n"
        with open(tmp, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _check_match(self, if_match: str | None) -> None:
        if if_match is not None and if_match.strip('"') != self._mtime():
            raise HubError(409, "cycles file changed since you loaded it; reload and redo the edit",
                           mtime=self._mtime(), cycles=self._load())

    def put(self, name: str, subtasks: Any, rename_to: str | None = None,
            if_match: str | None = None) -> dict:
        name = validate_name(name)
        new_name = validate_name(rename_to) if rename_to is not None else name
        subtasks = validate_subtasks(new_name, subtasks)
        with self._lock:
            self._check_match(if_match)
            cycles = self._load()
            if new_name != name:
                if name not in cycles:
                    raise HubError(404, f"no cycle {name!r} to rename")
                if new_name in cycles:
                    raise HubError(409, f"a cycle named {new_name!r} already exists")
                # keep the cycle's position in file order
                cycles = {(new_name if k == name else k): (subtasks if k == name else v)
                          for k, v in cycles.items()}
            else:
                cycles[name] = subtasks      # edit in place, or append a new cycle
            self._write(cycles)
            return {"file": str(self.path), "mtime": self._mtime(), "cycles": cycles}

    def delete(self, name: str, if_match: str | None = None) -> dict:
        with self._lock:
            self._check_match(if_match)
            cycles = self._load()
            if name not in cycles:
                raise HubError(404, f"no cycle {name!r}")
            if len(cycles) == 1:
                raise HubError(400, "can't delete the last cycle (collect.py refuses an empty cycles file)")
            del cycles[name]
            self._write(cycles)
            return {"file": str(self.path), "mtime": self._mtime(), "cycles": cycles}


def mount(app: FastAPI, cfg: dict, mgr) -> CycleStore:
    """Add the /api/cycles routes. cfg = the host config's `cycles:` block."""
    store = CycleStore(cfg["file"], cfg.get("history_dir"), int(cfg.get("keep", 50)))
    service = cfg.get("service")

    def note() -> dict:
        if service and mgr.service_state(service) in ("running", "ready", "stopping"):
            return {"note": f"{service} is running; changes take effect at its next start"}
        return {}

    async def body(request: Request) -> dict:
        try:
            b = await request.json()
        except Exception:  # noqa: BLE001
            raise HubError(400, "body must be JSON")
        if not isinstance(b, dict):
            raise HubError(400, "body must be a JSON object")
        return b

    @app.get("/api/cycles")
    async def list_cycles():
        return await asyncio.to_thread(store.get)

    @app.put("/api/cycles/{name}")
    async def put_cycle(name: str, request: Request):
        b = await body(request)
        res = await asyncio.to_thread(store.put, name, b.get("subtasks"), b.get("rename_to"),
                                      request.headers.get("if-match"))
        return {**res, **note()}

    @app.delete("/api/cycles/{name}")
    async def delete_cycle(name: str, request: Request):
        res = await asyncio.to_thread(store.delete, name, request.headers.get("if-match"))
        return {**res, **note()}

    return store
