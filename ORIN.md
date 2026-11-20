# ORIN.md — Orin-side plan and status (yor_hub)

Owner: the Claude agent on the **Orin** (192.168.123.116). Works on branch
**`orin`**. Read [PI.md](PI.md) first: it holds the architecture and the
**shared API contract**. Edit only this file and Orin-specific code/config.
Put requests for the Pi agent under **Requests to Pi** below.

## Your job

Run the same `yor_hub` package on the Orin, so that the Pi's web GUI can manage
both machines. Until the Pi pushes the hub core (`yor_hub/` on `main`), work on
the Orin-specific parts:

1. **`config/orin.yaml`**: the services the Orin runs, in the same schema as
   `config/pi.yaml`. Fill in the real commands for:
   - cycle server / data collector (`~/yor_ws/.../collect.py`, announces `collection/cycle` on :5559)
   - ZED / camera / video server (:8444, :6000) if it isn't already a system service
   - policy deployment server(s)
   - `agent`: `claude` in tmux (multi-instance)
   For each one, give: cwd, env activation (conda/venv/ROS source), command,
   default args, `requires`, and a `ready` check (usually a tcp port).
2. **Orin checks and stats** (they go in `Stats.extra` and the `checks` map):
   - GPU utilization and memory, power mode, temperature (tegrastats / jtop / sysfs)
   - camera presence (ZED serial, wrist cams /dev/video*, fisheye)
   - disk free where episodes are written
   Write these as functions that return the `Check` / `Stats` shapes in PI.md, so
   they drop into `yor_hub/checks.py` behind a host-specific check `type`.
3. **Install path**: python version, whether you can make a venv,
   tmux installed, systemd available, and how the Orin boots (what already
   autostarts). Record the facts under "Orin facts" below.

The user said Orin integration details come later. Keep this lightweight and
ask the user before changing anything already running on the Orin.

## Orin facts

- IP: 192.168.123.116 (Tailscale 100.126.142.47). Power mode MAXN (required for 30 fps ZED).
- L4T R36.5 (JetPack 6.x), aarch64, kernel 5.15-tegra. System python 3.10.12; Orin
  scripts use system `python3` + `~/.local` (no conda). micromamba env `vr_teleop`
  is used only by `vr-teleop/start_video.sh`.
- tmux at /usr/bin/tmux (an unrelated session `0` exists; the hub uses `yor`).
  systemd available. `claude` at `~/.local/bin/claude`.
- Workspace: `~/yor_ws` (collect.py, zed_rgbd.py, pi_atlas_observer.py, vr-teleop/),
  this repo at `~/yor_ws/yor_interface`.
- Data disk: T7 SSD at `/mnt/t7` (exFAT, fstab automount). **Not plugged in on 2026-11-20.**
- Cameras (by USB port): ZED 2i `3.4`, left gripper Innomaker `1.4`, right gripper `1.1`.
- Nothing robot-related autostarts. Running at boot: ssh, tailscaled, docker, gdm,
  user unit `globusconnectpersonal` (shares /mnt/t7 read-only).
- Ports in use by Orin tools: **8080 collect.py preview**, 5551 zed/view, 5559 cycle
  announce, 8444 headset video, 5580/5583 pi-atlas observer, 5549/5550 old deploy stack.
  No process uses :6000 that I could find.
- Hub deps (fastapi, uvicorn) are not installed yet; pyyaml is.
- **Inference never runs on the Orin** (user, 2026-11-20): policies run on an external
  machine. The Orin's deploy role is only publishing observations (camera observers),
  so `config/orin.yaml` has no policy-server services.

## Requests to Pi

- **Orin hub port 8090, not 8080.** collect.py's camera preview binds 0.0.0.0:8080 on the
  Orin (and the browser habit/docs point there), so the hub can't share it. Please set
  the Orin peer in `config/pi.yaml` to `http://192.168.123.116:8090` and note in the
  contract that the port is per-host (from `host.port`).
- **Shared-code fix in `yor_hub/checks.py` (on `orin`, please take it into main):**
  `host_stats` called `shutil.disk_usage(disk_path)`, and on the Orin `disk_path` is the
  T7's systemd automount point. With the drive unplugged that stat blocks in
  `autofs_wait` (D state, unkillable) until the mount times out, so the hub hung at
  startup and never bound its port. Added `_real_mount()` (reads /proc/self/mounts,
  never touches the path; uses abspath because realpath stats it too), used it to guard
  `host_stats`, and added a `mount` check type (`{type: mount, path, min_free_gb}`).
  Note `path` checks have the same problem under an automount point.
- Tokens differ: the Orin generated its own `~/.config/yor_hub/token`, so the Pi shows
  the Orin as "bad token" and vice versa. Copy the Pi's token over once it's final.

## Log

- 2026-11-20: file created by the Pi agent as a starting brief.
- 2026-11-20: merged main (phase 1) into orin. Added `config/orin.yaml`: collection
  (start_collection.sh), vr_only, video, pi_atlas_observer, agent; path checks for
  ZED/grippers/T7, tcp checks for 8080/5551/5559/8444/5580. The old deploy_observer stack
  is left out (still uses the unplugged fisheyes). Validated by loading it through
  `ServiceSpec` and `run_checks`. Next: Orin stats (GPU, power mode, temps) in checks.py.
- 2026-11-20: hub runs on the Orin by hand (`.venv/bin/python -m yor_hub --config
  config/orin.yaml`, port 8090), not yet as a systemd service. No python3-venv package:
  made the venv with `--without-pip` and bootstrapped pip from system pip
  (`--ignore-installed pip`). Fixed the automount hang (see Requests to Pi).
