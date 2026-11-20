"""Health checks and host stats. Each check returns {ok, level, detail, data?}."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from typing import Any

Check = dict[str, Any]


def _res(ok: bool, detail: str, level: str | None = None, **data) -> Check:
    out: Check = {"ok": ok, "level": level or ("ok" if ok else "error"), "detail": detail}
    if data:
        out["data"] = data
    return out


def check_can(iface: str, **_) -> Check:
    try:
        r = subprocess.run(["ip", "-details", "-statistics", "-json", "link", "show", iface],
                           capture_output=True, text=True, timeout=2)
    except Exception as e:  # noqa: BLE001
        return _res(False, f"ip failed: {e}")
    if r.returncode != 0:
        return _res(False, "interface missing (adapter unplugged or not renamed by udev)")
    try:
        link = json.loads(r.stdout)[0]
    except Exception:  # noqa: BLE001
        return _res(False, "could not parse ip output")
    flags = link.get("flags", [])
    info = (link.get("linkinfo") or {}).get("info_data") or {}
    state = info.get("state", "?")
    bitrate = (info.get("bittiming") or {}).get("bitrate")
    stats = link.get("stats64") or link.get("stats") or {}
    rx = (stats.get("rx") or {}).get("packets")
    tx = (stats.get("tx") or {}).get("packets")
    errs = (stats.get("rx") or {}).get("errors", 0) + (stats.get("tx") or {}).get("errors", 0)
    up = "UP" in flags
    data = dict(state=state, bitrate=bitrate, rx=rx, tx=tx, errors=errs)
    if not up:
        return _res(False, "down", **data)
    br = f"{bitrate // 1000}k" if bitrate else "?"
    if state in ("ERROR-ACTIVE",):
        return _res(True, f"up {br} {state}", **data)
    if state in ("ERROR-WARNING", "ERROR-PASSIVE"):
        return _res(True, f"up {br} {state}", level="warn", **data)
    if state == "?" :  # vcan etc.
        return _res(True, f"up {br}", **data)
    return _res(False, f"up {br} {state}", **data)  # BUS-OFF / STOPPED


async def check_tcp(host: str = "127.0.0.1", port: int = 0, timeout: float = 0.3, **_) -> Check:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return _res(True, f"{host}:{port} open")
    except Exception:  # noqa: BLE001
        return _res(False, f"{host}:{port} closed")


def check_path(path: str, **_) -> Check:
    p = os.path.expanduser(path)
    return _res(os.path.exists(p), "present" if os.path.exists(p) else f"missing: {p}")


def _real_mount(path: str) -> tuple[str, str] | None:
    """(mountpoint, fstype) of the real filesystem holding `path`, read from
    /proc/self/mounts without touching `path`. None when the nearest mountpoint is
    only an autofs trigger: stat'ing such a path blocks (D state) until systemd's
    automount gives up, which is ~90 s when the drive is unplugged."""
    p = os.path.abspath(os.path.expanduser(path))  # not realpath: it stats the path
    best: dict[str, list[str]] = {}
    try:
        with open("/proc/self/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    best.setdefault(parts[1], []).append(parts[2])
    except OSError:
        return None
    mp = p
    while True:
        if mp in best:
            real = [t for t in best[mp] if t != "autofs"]
            return (mp, real[-1]) if real else None
        if mp == "/":
            return None
        mp = os.path.dirname(mp)


def check_mount(path: str, min_free_gb: float = 0, **_) -> Check:
    """Is `path` on a real (non-autofs) filesystem, and how much is free. Never
    triggers an automount, so it is safe to poll for unplugged drives."""
    m = _real_mount(path)
    if not m:
        return _res(False, f"not mounted: {path}")
    mp, fstype = m
    try:
        free = shutil.disk_usage(mp).free / 1e9
    except OSError as e:
        return _res(False, f"{fstype} at {mp}: {e}")
    detail = f"{fstype} at {mp}, {free:.0f} GB free"
    if free < min_free_gb:
        return _res(True, detail + f" (< {min_free_gb:g} GB)", level="warn", free_gb=round(free, 1))
    return _res(True, detail, free_gb=round(free, 1))


SYNC_CHECKS = {"can": check_can, "path": check_path, "mount": check_mount}

from . import tegra  # noqa: E402  (Jetson-only stats/checks; inert elsewhere)
if tegra.IS_TEGRA:
    SYNC_CHECKS["power_mode"] = tegra.check_power_mode
ASYNC_CHECKS = {"tcp": check_tcp}


async def run_checks(specs: dict[str, dict]) -> dict[str, Check]:
    names, coros = [], []
    for name, spec in specs.items():
        spec = dict(spec)
        typ = spec.pop("type")
        spec.pop("label", None)
        if typ in ASYNC_CHECKS:
            coro = ASYNC_CHECKS[typ](**spec)
        elif typ in SYNC_CHECKS:
            coro = asyncio.to_thread(SYNC_CHECKS[typ], **spec)
        else:
            async def _unknown(t=typ):
                return _res(False, f"unknown check type {t}")
            coro = _unknown()
        names.append(name)
        coros.append(coro)
    results = await asyncio.gather(*coros, return_exceptions=True)
    out = {}
    for name, res in zip(names, results):
        out[name] = res if isinstance(res, dict) else _res(False, f"check crashed: {res}")
        out[name]["label"] = specs[name].get("label", name)
    return out


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def host_stats(disk_path: str) -> dict:
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    mem = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        mem[k] = int(v.split()[0]) if v.split() else 0
    mem_used = 100.0 * (1 - mem.get("MemAvailable", 0) / mem["MemTotal"]) if mem.get("MemTotal") else None
    disk_free = None
    m = _real_mount(disk_path)  # never stat an unmounted automount point (it blocks)
    if m:
        try:
            disk_free = shutil.disk_usage(os.path.expanduser(disk_path)).free / 1e9
        except OSError:
            pass
    extra: dict[str, Any] = {}
    if tegra.IS_TEGRA:
        extra.update(tegra.tegra_stats(mem))
    if shutil.which("vcgencmd"):
        try:
            r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=1)
            val = int(r.stdout.strip().split("=")[1], 16)
            extra["throttled"] = hex(val)
            extra["undervoltage_now"] = bool(val & 0x1)
            extra["throttled_now"] = bool(val & 0x4)
        except Exception:  # noqa: BLE001
            pass
    return {
        "cpu_temp_c": round(int(temp) / 1000, 1) if temp and temp.isdigit() else None,
        "load": list(os.getloadavg()),
        "mem_used_pct": round(mem_used, 1) if mem_used is not None else None,
        "disk_free_gb": round(disk_free, 1) if disk_free is not None else None,
        "extra": extra,
    }
