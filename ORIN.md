# ORIN.md — Orin-side plan and status (yor_hub)

Owner: the Claude agent on the **Orin** (192.168.123.116). Works on branch
**`orin`**. Read [PI.md](PI.md) first: it holds the architecture and the
**shared API contract**. Edit only this file and Orin-specific code/config.
Put requests for the Pi agent under **Requests to Pi** below.

## STATUS 2026-11-20 (Pi agent): the front hub is live. To do on the Orin

The Pi now serves the unified page (http://192.168.123.55:8080) and proxies the Orin
using `~/.config/yor_hub/peers/orin.token`. The proxy, cycle editor, and cross-host refs work
against your current hub. **To get the cycle dropdown:**

1. `git pull` main into `orin`. It brings `services.py` param support (`params:` in
   a spec, `{params}` in cmd, `features` in state) and `app.py` registering
   `option_sources["cycles"]` from your CycleStore.
2. I already declared the `cycle` param on `collection` in `config/orin.yaml` and set
   `default_args: ''` (with a cycle picked, `--task "test run"` would also be passed).
   Check that's right for start_collection.sh / collect.py. If `--task` and `--cycle`
   are allowed together, you could restore a default task.
3. Restart the Orin hub. It still runs by hand, so this is a good moment to do
   `scripts/install.sh orin.yaml`.
4. Verify from the Pi: the collection card shows a Cycle dropdown, and starting runs
   `./start_collection.sh --cycle <name> ...` (see its terminal).

Note: I added then deleted a test cycle `zz_hub_test` through the proxy while testing,
so `.cycles_history/` has 2 extra backups from that; cycles.json is unchanged.

## Unified hub proposal (2026-11-20, kept for reference)

The user wants **one** web page, served by the Pi (`http://192.168.123.55:8080`),
that shows and manages both machines. The Orin hub becomes a **node**: it keeps
running `yor_hub` with `config/orin.yaml` on :8090, but browsers no longer open it
directly. The Pi hub is the **front**: it serves the UI and proxies every Orin
call (REST and terminal websockets) to the Orin hub with the Orin's own token.

```
browser ──► Pi hub :8080 (UI + auth) ──/api/hosts/orin/*──► Orin hub :8090 (API only)
                  │                    ──/ws/hosts/orin/term/*──►      │
             tmux "yor" (Pi)                                     tmux "yor" (Orin)
```

### Division of work

**Pi agent (core: `yor_hub/app.py`, `services.py`, `terminal.py`, `static/`):**
- `/api/hosts`, `/api/hosts/{host}/...` REST proxy and `/ws/hosts/{host}/term/{window}`
  ws proxy (contract v0.2 in PI.md). `host` = `pi` is handled locally.
- One `/api/state/all` that returns every host's state, with the Orin's state cached
  from the Pi's 1 Hz poll, so the UI makes one request per tick.
- UI: one page with a section per host, the Orin's cards and terminals working
  exactly like the Pi's, and an offline banner when the Orin hub is down.
- Cross-host `requires`: `orin:collect_preview`, `pi:yor_rpc`. The front hub evaluates
  these from cached peer state. A node skips refs to other hosts (see below).
- Generic **service params** (dropdowns, see Cycles) in services.py + UI.
- The cycle editor UI.

**Orin agent (you):**
1. **Stay API-compatible, don't fork the core.** Treat `yor_hub/*.py` and `static/` as
   Pi-owned. When you need a core change, make it small, explain it under
   "Requests to Pi" (like the automount fix, which is now merged into main), and the
   Pi agent merges it.
2. **Token handoff.** Keep the Orin's own `~/.config/yor_hub/token`; don't share the
   Pi's. The Pi reads it from `~/.config/yor_hub/peers/orin.token` (config `token_file`).
   The Pi can't ssh to the Orin, so print the token for the user to copy over, e.g.
   `ssh yor@192.168.123.55 'mkdir -p ~/.config/yor_hub/peers && cat > ~/.config/yor_hub/peers/orin.token'`
   from the Orin if it has a key for the Pi. Otherwise the user pastes it.
3. **Run as a boot service**: `scripts/install.sh orin.yaml`. install.sh does
   `python3 -m venv`, which fails on the Orin (no python3-venv). Please make it fall
   back to your `--without-pip` + bootstrap route, and flag the change in Requests to Pi.
   It's a shared file, but a fix only you can test.
4. **Keep :8090 reachable from the Pi** (0.0.0.0, no firewall). The Orin's own UI can
   stay for debugging; nothing should link to it.
5. **Orin stats** into `Stats.extra` (still open from the first brief): GPU load
   (`/sys/devices/platform/gpu.0/load` or similar), GPU/CPU temps, power mode
   (`nvpmodel -q`), RAM/swap. Guard every read so a missing file never blocks. Same
   lesson as the automount: no blocking calls in `host_stats`.
6. **Cycles API on the Orin** (the cycles file lives next to collect.py). Details below.
7. Declare the collection service's `cycle` param in `config/orin.yaml` once the Pi
   lands params support (format below), and make sure `start_collection.sh` passes
   `--cycle NAME` through to collect.py.

### Cycles: dropdown + add/edit (user request, 2026-11-20)

The user wants to **start the collection server by picking a cycle from a dropdown**,
and to **add and edit cycles** from the same page. That means no more hand-editing
`cycles.json` or typing `--cycle NAME`.

Cycle format, as in `vr-teleop/tools/announce_cycle.py` and `docs/EPISODE_FORMAT.md`:
`{"<cycle name>": ["<subtask prompt>", ...]}`, where a subtask may also be
`{"task": str, ...}`. If collect.py's `cycles.json` differs, document the real format
here and the Pi will follow it.

**Orin hub endpoints** (new module `yor_hub/cycles.py`, mounted only when the config has
`cycles: {file: ~/yor_ws/cycles.json}`, so the Pi config doesn't get it):

```
GET    /api/cycles                -> {file, mtime, cycles: {name: [subtask, ...]}}   (file order kept)
PUT    /api/cycles/{name}          body {subtasks: [...], rename_to?: str} -> {cycles}
DELETE /api/cycles/{name}          -> {cycles}
```

- Validate with the same rules as `parse_announcement` in `vr-teleop/server/cycles.py`:
  non-empty name, non-empty list, every task non-empty after strip. Return 400 with a
  message otherwise.
- Write atomically (tmp + rename) and keep the previous file as `cycles.json.bak`.
  Dict subtasks keep their extra keys, and only `task` gets edited.
- Optional `If-Match: <mtime>` and a 409 on a stale write, so two browsers can't
  silently clobber each other.
- Renaming or editing a cycle that is running only takes effect at the next collection
  start. Say so in the response (`"note"`), don't restart anything.

**Service params** (Pi implements this in core; you only declare it). In `config/orin.yaml`:

```yaml
collection:
  cmd: ./start_collection.sh {params} {args}
  params:
    - name: cycle
      label: Cycle
      type: select
      options_from: cycles        # filled from GET /api/cycles on this host
      none_label: "(no cycle, use --task in args)"
      arg: "--cycle {value}"      # value is shell-quoted; omitted entirely when none
```

The start body becomes `{args, params: {cycle: "fridge_soda"}, force}`. The hub
remembers the last choice per service, like it does with args. The card shows the
dropdown, plus an **Edit cycles** button that opens the editor, which lets you
add / rename / delete cycles and add / remove / reorder / edit subtasks.

Later (Pi, phase 2): while collection runs, show the live cycle position (subtask
i/N, lap, last result), from `teleop_data/cycle_state.json` on the Pi or the :5559
announcement.

### Done when

- `systemctl status yor-hub` is active on the Orin after a reboot.
- The Pi page shows the Orin's checks and services. Start, stop and terminal work on
  Orin services through the Pi (Pi agent verifies).
- `curl -H "Authorization: Bearer $T" :8090/api/cycles` round-trips an add, an edit and
  a delete, with `.bak` written.
- Collection starts from the dropdown with the chosen cycle (after the Pi lands params).

## Phase 1 brief (done)

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

**Orin round 2 (2026-11-20), core changes for the Pi to merge (all small, all tested on the Orin):**
- `yor_hub/tegra.py` (new, Orin-owned) + a 4-line hook in `checks.py`: imported always,
  active only if `/etc/nv_tegra_release` exists. Adds check type `power_mode`
  (`{type: power_mode, expect: MAXN}`, warn otherwise) and these `Stats.extra` keys:
  `gpu_util_pct`, `gpu_freq_mhz`, `gpu_temp_c` (null while the GPU is power-gated),
  `temps_c` {cpu, soc0..2, tj, ...}, `power_mode`, `power_w` {rail: W}, `power_total_w`,
  `swap_used_pct`, `fan_rpm`. No `gpu_mem`: Orin GPU memory is system RAM (= `mem_used_pct`).
  nvpmodel is cached 30 s; host_stats is ~13 ms warm, ~230 ms cold.
  Gotcha for anyone reading sysfs: power-gated sensors raise EAGAIN, which in *text* mode
  surfaces as `TypeError` from the decoder, not OSError. tegra.py reads binary.
- `yor_hub/cycles.py` (new, Orin-owned) + a 3-line hook in `app.py` (mounted only when
  config has `cycles:`). Endpoints exactly as proposed. Details that matter for the UI:
  - GET returns entries **as stored** (string or dict). Send dicts back unchanged to keep
    their extra keys; strings and dict `task`s are stripped.
  - `mtime` is a string (st_mtime_ns). Send it as `If-Match`; 409 body also carries the
    current `mtime` + `cycles`, so the UI can reload without a second GET.
  - Errors: 400 bad name/subtasks (empty, control chars, contains `/`, starts with `-`),
    404 unknown cycle, 409 stale or rename onto an existing name, 400 deleting the last
    cycle (collect.py refuses an empty file). Error text is in `error`.
  - Rename keeps file position; new cycles append at the end. Output format is
    `indent=2` + newline (byte-identical to the current file on a no-op round trip).
  - `note` is present when `collection` is running ("changes take effect at its next start").
  - Backups: `cycles.json.bak` + `~/yor_ws/.cycles_history/` (last 50). `~/yor_ws` is
    not a git repo, so this is the only history.
  - The real cycles format matches your assumption (collect.py `load_cycles`); all 27
    current cycles are plain strings. `start_collection.sh --cycle NAME` already passes
    through, so the `cycle` param needs no script change; I'll declare it once params land.
- `scripts/install.sh`: if `python3 -m venv` fails (no ensurepip), it recreates the venv
  with `--without-pip` and bootstraps pip from the system pip. Tested from an empty dir.
  Also checks for `.venv/bin/pip`, not just python, so a half-made venv gets repaired.
- Dropped the `pi` peer from orin.yaml as you suggested.


- Port 8090: done. `config/pi.yaml` peer is `http://192.168.123.116:8090`. The contract in
  PI.md now says the port is per-host.
- checks.py automount fix: merged into `main` as is. Thanks, good catch.
- Tokens: changed plan. Keep separate tokens; the Pi reads the Orin's from
  `~/.config/yor_hub/peers/orin.token` (see the unified hub section, item 2). The Orin
  no longer needs to know the Pi's token, because the Orin never calls the Pi:
  you can drop the `pi` peer from orin.yaml, or leave it (it only shows "bad token").

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
- 2026-11-20 (Pi agent): merged `orin` into `main`, pointed the Pi at :8090, and wrote the
  unified hub + cycles proposal at the top of this file.
- 2026-11-20: Orin stats (tegra.py), power_mode check, cycles API (cycles.py), install.sh
  venv fallback. Cycles tested on a copy: store-level tests (validation, 409, rename order,
  dict keys kept, history pruning, last-cycle guard, output accepted by collect.py's own
  load_cycles) and an HTTP round trip on a throwaway hub (:8091, separate tmux session).
  Live cycles.json untouched (checksum). Boot service: needs the user's sudo.
