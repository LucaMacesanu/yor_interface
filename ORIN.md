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

- IP: 192.168.123.116
- (fill in: OS / JetPack, python, tmux, conda/venv, where `yor_ws` lives, what runs at boot)

## Requests to Pi

- (none yet)

## Log

- 2026-11-20: file created by the Pi agent as a starting brief.
