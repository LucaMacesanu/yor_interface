"""Thin wrapper around the tmux CLI. Every managed process is one window in one session."""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

FMT = "\t".join([
    "#{window_name}", "#{pane_dead}", "#{pane_dead_status}", "#{pane_pid}",
    "#{pane_start_command}", "#{window_activity}",
])


@dataclass
class Pane:
    window: str
    dead: bool
    exit_code: int | None
    pid: int | None


class Tmux:
    def __init__(self, session: str, home_cwd: str):
        self.session = session
        self.home_cwd = home_cwd

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check, timeout=5)

    def has_session(self) -> bool:
        return self._run("has-session", "-t", f"={self.session}", check=False).returncode == 0

    def ensure_session(self) -> None:
        if self.has_session():
            return
        # Window 0 is a plain shell, so the session always has at least one window.
        self._run("new-session", "-d", "-s", self.session, "-n", "shell", "-c", self.home_cwd)
        self._run("set-option", "-t", self.session, "mouse", "on")
        self._run("set-option", "-t", self.session, "history-limit", "20000")

    def panes(self) -> dict[str, Pane]:
        if not self.has_session():
            return {}
        r = self._run("list-panes", "-s", "-t", f"={self.session}", "-F", FMT, check=False)
        out: dict[str, Pane] = {}
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            name, dead, status, pid = parts[:4]
            if name in out:  # first pane of a window wins
                continue
            out[name] = Pane(
                window=name,
                dead=dead == "1",
                exit_code=int(status) if dead == "1" and status.lstrip("-").isdigit() else None,
                pid=int(pid) if pid.isdigit() else None,
            )
        return out

    def target(self, window: str) -> str:
        return f"={self.session}:={window}"

    def new_window(self, window: str, shell_cmd: str, cwd: str, log_path: str | None = None) -> None:
        """Start `shell_cmd` (run by bash -lc) in a fresh window. Kills a stale window of the same name."""
        self.ensure_session()
        if window in self.panes():
            self._run("kill-window", "-t", self.target(window), check=False)
        cmd = f"bash -lc {shlex.quote(shell_cmd)}"
        # Chained in one tmux invocation, so remain-on-exit is set before the
        # server processes the child's exit, even when it dies instantly.
        args = ["new-window", "-d", "-t", f"={self.session}:", "-n", window, "-c", cwd, cmd,
                ";", "set-option", "-w", "-t", self.target(window), "remain-on-exit", "on",
                ";", "set-option", "-w", "-t", self.target(window), "automatic-rename", "off"]
        if log_path:
            args += [";", "pipe-pane", "-t", self.target(window), f"cat >> {shlex.quote(log_path)}"]
        self._run(*args)

    def send_keys(self, window: str, *keys: str) -> None:
        self._run("send-keys", "-t", self.target(window), *keys, check=False)

    def kill_window(self, window: str) -> None:
        self._run("kill-window", "-t", self.target(window), check=False)

    def capture(self, window: str, lines: int = 500) -> str:
        r = self._run("capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", self.target(window), check=False)
        return r.stdout
