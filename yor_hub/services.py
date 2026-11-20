"""Service manager: config-defined processes run as tmux windows."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from .checks import host_stats, run_checks
from .tmux import Tmux

STATE_DIR = Path(os.environ.get("YOR_HUB_STATE", "~/.local/state/yor_hub")).expanduser()
AGENT_RE = re.compile(r"^agent-(\d+)$")


@dataclass
class ServiceSpec:
    id: str
    label: str
    cmd: str
    group: str = "robot"
    kind: str = "daemon"          # daemon | oneshot | agent
    cwd: str = "~"
    env: str | None = None         # key into config env_setups
    default_args: str = ""
    requires: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # blocked while any of these is ok/running
    ready: str | None = None
    autostart: bool = False
    stop_timeout: float = 10.0
    description: str = ""


class HubError(Exception):
    def __init__(self, status: int, msg: str, **extra):
        super().__init__(msg)
        self.status, self.msg, self.extra = status, msg, extra


class ServiceManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.host = cfg["host"]
        self.env_setups: dict[str, str] = cfg.get("env_setups", {})
        self.check_specs: dict[str, dict] = cfg.get("checks", {})
        self.specs: dict[str, ServiceSpec] = {}
        for sid, s in (cfg.get("services") or {}).items():
            self.specs[sid] = ServiceSpec(id=sid, **s)
        self.tmux = Tmux(cfg.get("tmux_session", "yor"), os.path.expanduser(cfg.get("home_cwd", "~")))
        self.log_dir = STATE_DIR / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = STATE_DIR / "state.json"
        self._persist = self._load_persist()
        self.started_at: dict[str, float] = {}
        self.stopping: set[str] = set()
        self.checks: dict[str, dict] = {}
        self.stats: dict = {}
        self.panes = {}
        self._lock = asyncio.Lock()
        self.peers: dict[str, dict] = {p["name"]: {**p, "online": None} for p in cfg.get("peers", [])}
        self._token: str | None = None

    # ---------- persistence ----------
    def _load_persist(self) -> dict:
        try:
            return json.loads(self._state_file.read_text())
        except Exception:  # noqa: BLE001
            return {"args": {}}

    def _save_persist(self) -> None:
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._persist, indent=2))
        tmp.replace(self._state_file)

    def args_for(self, sid: str) -> str:
        spec = self.specs.get(sid)
        return self._persist["args"].get(sid, spec.default_args if spec else "")

    # ---------- polling ----------
    async def refresh(self) -> None:
        self.panes = await asyncio.to_thread(self.tmux.panes)
        self.checks = await run_checks(self.check_specs)
        self.stats = await asyncio.to_thread(host_stats, self.cfg.get("disk_path", "~"))

    def peer_token(self, name: str) -> str:
        """The peer's own token (from its token_file); falls back to ours (shared-token setups)."""
        tf = self.peers.get(name, {}).get("token_file")
        if tf:
            try:
                return Path(tf).expanduser().read_text().strip()
            except OSError:
                pass
        return self._token or ""

    def _fetch_peer(self, name: str, url: str) -> dict:
        import urllib.request
        req = urllib.request.Request(url.rstrip("/") + "/api/info",
                                     headers={"Authorization": f"Bearer {self.peer_token(name)}"})
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return json.loads(r.read())

    async def poll_peers_forever(self, period: float = 5.0) -> None:
        while True:
            for name, p in self.peers.items():
                try:
                    info = await asyncio.to_thread(self._fetch_peer, name, p["url"])
                    p.update(online=True, detail=f"{info.get('label') or info.get('host')} v{info.get('version')}")
                except Exception as e:  # noqa: BLE001
                    code = getattr(e, "code", None)
                    p.update(online=False, detail="bad token" if code == 401 else "unreachable")
            await asyncio.sleep(period)

    async def poll_forever(self, period: float = 1.0) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as e:  # noqa: BLE001
                print(f"[yor_hub] refresh failed: {e}", flush=True)
            await asyncio.sleep(period)

    async def autostart(self) -> None:
        await asyncio.to_thread(self.tmux.ensure_session)
        await self.refresh()
        for sid, spec in self.specs.items():
            if spec.autostart and sid not in self.panes:
                print(f"[yor_hub] autostart {sid}", flush=True)
                try:
                    await self.start(sid, force=False)
                except HubError as e:
                    print(f"[yor_hub] autostart {sid} skipped: {e.msg}", flush=True)

    # ---------- status ----------
    def _check_ok(self, name: str) -> bool:
        if name.startswith("service:"):
            st = self.service_state(name.split(":", 1)[1])
            return st in ("running", "ready")
        c = self.checks.get(name)
        return bool(c and c["ok"])

    def blocked_by(self, spec: ServiceSpec) -> list[str]:
        out = [r for r in spec.requires if not self._check_ok(r)]
        out += [f"{c} running" for c in spec.conflicts if self._check_ok(c)]
        return out

    def service_state(self, window: str, spec: ServiceSpec | None = None) -> str:
        p = self.panes.get(window)
        if window in self.stopping and (p and not p.dead):
            return "stopping"
        if not p:
            return "stopped"
        if p.dead:
            return "exited" if p.exit_code == 0 else "failed"
        spec = spec or self.specs.get(window)
        if spec and spec.ready and self._check_ok(spec.ready):
            return "ready"
        return "running"

    def _service_json(self, window: str, spec: ServiceSpec) -> dict:
        p = self.panes.get(window)
        return {
            "id": window,
            "label": spec.label if window == spec.id else f"{spec.label} {window.split('-')[-1]}",
            "description": spec.description,
            "group": spec.group,
            "kind": spec.kind,
            "state": self.service_state(window, spec),
            "exit_code": p.exit_code if p else None,
            "pid": p.pid if p and not p.dead else None,
            "started_at": self.started_at.get(window),
            "args": self.args_for(spec.id),
            "requires": spec.requires,
            "blocked_by": self.blocked_by(spec),
            "ready_check": spec.ready,
            "window": window if p else None,
        }

    def services_json(self) -> list[dict]:
        out = []
        for sid, spec in self.specs.items():
            if spec.kind == "agent":
                continue
            out.append(self._service_json(sid, spec))
        agent_spec = next((s for s in self.specs.values() if s.kind == "agent"), None)
        if agent_spec:
            for w in sorted((w for w in self.panes if AGENT_RE.match(w)),
                            key=lambda w: int(AGENT_RE.match(w).group(1))):
                out.append(self._service_json(w, agent_spec))
        return out

    def state_json(self) -> dict:
        return {
            "host": self.host["name"],
            "label": self.host.get("label", self.host["name"]),
            "time": time.time(),
            "services": self.services_json(),
            "checks": self.checks,
            "stats": self.stats,
            "extra_windows": [w for w in self.panes
                              if w not in self.specs and not AGENT_RE.match(w)],
            "peers": list(self.peers.values()),
        }

    # ---------- actions ----------
    def _resolve(self, sid: str) -> tuple[str, ServiceSpec]:
        if sid in self.specs and self.specs[sid].kind != "agent":
            return sid, self.specs[sid]
        if AGENT_RE.match(sid):
            spec = next((s for s in self.specs.values() if s.kind == "agent"), None)
            if spec:
                return sid, spec
        raise HubError(404, f"unknown service {sid}")

    def _shell_cmd(self, spec: ServiceSpec, args: str, window: str) -> str:
        cmd = spec.cmd.replace("{args}", args) if "{args}" in spec.cmd else f"{spec.cmd} {args}".strip()
        parts = [f"cd {shlex.quote(os.path.expanduser(spec.cwd))}"]
        if spec.env:
            if spec.env not in self.env_setups:
                raise HubError(500, f"env {spec.env} not in env_setups")
            parts.append(self.env_setups[spec.env])
        parts.append(f"echo {shlex.quote(f'[yor_hub] {window}: {cmd}')}")
        parts.append(f"exec {cmd}")
        return " && ".join(parts)

    async def start(self, sid: str, args: str | None = None, force: bool = False) -> dict:
        async with self._lock:
            window, spec = self._resolve(sid)
            await self.refresh()
            st = self.service_state(window, spec)
            if st in ("running", "ready", "stopping"):
                raise HubError(409, f"{window} is already {st}")
            blocked = self.blocked_by(spec)
            if blocked and not force:
                raise HubError(409, f"{window} blocked by: {', '.join(blocked)}", blocked_by=blocked)
            if args is not None and spec.kind != "agent":
                self._persist["args"][spec.id] = args
                self._save_persist()
            use_args = args if args is not None else self.args_for(spec.id)
            log = self.log_dir / f"{window}.log"
            if log.exists():
                log.replace(log.with_suffix(".log.1"))
            shell_cmd = self._shell_cmd(spec, use_args, window)
            await asyncio.to_thread(self.tmux.new_window, window, shell_cmd,
                                    os.path.expanduser(spec.cwd), str(log))
            self.started_at[window] = time.time()
            self.stopping.discard(window)
            await asyncio.sleep(0.2)
            await self.refresh()
            return self._service_json(window, spec)

    async def new_agent(self, cwd: str | None = None) -> dict:
        spec = next((s for s in self.specs.values() if s.kind == "agent"), None)
        if not spec:
            raise HubError(404, "no agent service configured")
        await self.refresh()
        used = {int(AGENT_RE.match(w).group(1)) for w in self.panes if AGENT_RE.match(w)}
        n = 1
        while n in used:
            n += 1
        if cwd:
            spec = ServiceSpec(**{**spec.__dict__, "cwd": cwd})
        window = f"agent-{n}"
        log = self.log_dir / f"{window}.log"
        await asyncio.to_thread(self.tmux.new_window, window, self._shell_cmd(spec, spec.default_args, window),
                                os.path.expanduser(spec.cwd), str(log))
        self.started_at[window] = time.time()
        await asyncio.sleep(0.2)
        await self.refresh()
        return self._service_json(window, spec)

    async def stop(self, sid: str) -> dict:
        window, spec = self._resolve(sid)
        await self.refresh()
        p = self.panes.get(window)
        if not p or p.dead:
            return self._service_json(window, spec)
        self.stopping.add(window)
        try:
            # 1) Ctrl-C, so the process runs its own shutdown (arms, lift stop, ...)
            self.tmux.send_keys(window, "C-c")
            if await self._wait_dead(window, spec.stop_timeout):
                return self._service_json(window, spec)
            # 2) SIGTERM
            if p.pid:
                try:
                    os.kill(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if await self._wait_dead(window, 3.0):
                return self._service_json(window, spec)
            # 3) kill the window (SIGHUP to the pane)
            self.tmux.kill_window(window)
            await asyncio.sleep(0.3)
            await self.refresh()
            return self._service_json(window, spec)
        finally:
            self.stopping.discard(window)

    async def _wait_dead(self, window: str, timeout: float) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.25)
            self.panes = await asyncio.to_thread(self.tmux.panes)
            p = self.panes.get(window)
            if not p or p.dead:
                return True
        return False

    async def restart(self, sid: str, args: str | None = None) -> dict:
        await self.stop(sid)
        return await self.start(sid, args=args, force=False)

    async def clear(self, sid: str) -> dict:
        window, spec = self._resolve(sid)
        await self.refresh()
        p = self.panes.get(window)
        if p and not p.dead:
            raise HubError(409, f"{window} is running; stop it first")
        self.tmux.kill_window(window)
        self.started_at.pop(window, None)
        await self.refresh()
        return self._service_json(window, spec)

    def log(self, sid: str, lines: int) -> str:
        window, _ = self._resolve(sid)
        return self.tmux.capture(window, lines)
