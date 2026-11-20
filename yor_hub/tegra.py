"""Jetson (Tegra) host stats and checks. Orin-owned; imported by checks.py only when
/etc/nv_tegra_release exists, so other hosts never touch it.

Every read is guarded and non-blocking: sysfs files that vanish or return EAGAIN
(gpu-thermal does while the GPU is power-gated) give None, never an exception.
nvpmodel is a subprocess (~0.2 s), so its result is cached for NVP_CACHE_S.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import time
from typing import Any

IS_TEGRA = os.path.exists("/etc/nv_tegra_release")
NVP_CACHE_S = 30.0
_nvp_cache: tuple[float, str | None] = (0.0, None)


def _read(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace").strip()
    except Exception:  # noqa: BLE001  missing, EAGAIN (power-gated sensor; in text
        return None    # mode that surfaces as a TypeError from the decoder), permission


def _num(path: str) -> int | None:
    v = _read(path)
    return int(v) if v is not None and v.lstrip("-").isdigit() else None


def _gpu_dir() -> str | None:
    for d in glob.glob("/sys/devices/platform/bus@0/*.gpu") + ["/sys/devices/platform/gpu.0"]:
        if os.path.exists(os.path.join(d, "load")):
            return d
    return None


_GPU = _gpu_dir() if IS_TEGRA else None


def power_mode() -> str | None:
    """nvpmodel mode name (e.g. 'MAXN', 'MODE_30W'), cached."""
    global _nvp_cache
    t, mode = _nvp_cache
    if time.monotonic() - t < NVP_CACHE_S and t:
        return mode
    mode = None
    try:
        r = subprocess.run(["nvpmodel", "-q"], capture_output=True, text=True, timeout=2)
        m = re.search(r"NV Power Mode:\s*(\S+)", r.stdout)
        mode = m.group(1) if m else None
    except Exception:  # noqa: BLE001
        pass
    _nvp_cache = (time.monotonic(), mode)
    return mode


def thermal() -> dict[str, float]:
    """{zone type without '-thermal': °C} for every zone that reads right now."""
    out = {}
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        name = (_read(f"{z}/type") or "").removesuffix("-thermal")
        t = _num(f"{z}/temp")
        if name and t is not None and t > -40000:
            out[name] = round(t / 1000, 1)
    return out


def power_rails() -> dict[str, float]:
    """INA3221 rails in watts, e.g. {'VDD_GPU_SOC': 1.7, ...}."""
    out = {}
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(f"{h}/name") != "ina3221":
            continue
        for lab in glob.glob(f"{h}/in*_label"):
            label = _read(lab)
            n = os.path.basename(lab)[2:-6]
            mv, ma = _num(f"{h}/in{n}_input"), _num(f"{h}/curr{n}_input")
            if label and mv is not None and ma is not None and not label.startswith("sum"):
                out[label] = round(mv * ma / 1e6, 2)
    return out


def tegra_stats(meminfo: dict[str, int] | None = None) -> dict[str, Any]:
    """Fields for Stats.extra on a Jetson."""
    extra: dict[str, Any] = {}
    if _GPU:
        load = _num(f"{_GPU}/load")             # per-mille
        extra["gpu_util_pct"] = round(load / 10, 1) if load is not None else None
        freqs = glob.glob(f"{_GPU}/devfreq/*/cur_freq")
        f = _num(freqs[0]) if freqs else None
        extra["gpu_freq_mhz"] = round(f / 1e6) if f else None
    temps = thermal()
    extra["temps_c"] = temps
    extra["gpu_temp_c"] = temps.get("gpu")      # None while the GPU is power-gated
    extra["power_mode"] = power_mode()
    rails = power_rails()
    if rails:
        extra["power_w"] = rails
        extra["power_total_w"] = round(sum(rails.values()), 1)
    if meminfo and meminfo.get("SwapTotal"):
        used = meminfo["SwapTotal"] - meminfo.get("SwapFree", 0)
        extra["swap_used_pct"] = round(100.0 * used / meminfo["SwapTotal"], 1)
    # Orin GPU shares system RAM (no separate VRAM), so gpu_mem == mem_used_pct.
    fan = _num("/sys/class/hwmon/hwmon4/rpm") if _read("/sys/class/hwmon/hwmon4/name") == "pwm_tach" else None
    if fan is not None:
        extra["fan_rpm"] = fan
    return extra


def check_power_mode(expect: str = "MAXN", **_) -> dict:
    """ok when nvpmodel reports `expect`; warn otherwise (ZED drops to ~18-23 fps in MODE_30W)."""
    mode = power_mode()
    if mode is None:
        return {"ok": False, "level": "warn", "detail": "nvpmodel unavailable"}
    if mode == expect:
        return {"ok": True, "level": "ok", "detail": mode}
    return {"ok": False, "level": "warn", "detail": f"{mode} (want {expect}: sudo nvpmodel -m 0)"}
