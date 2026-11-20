# PI.md — Pi-side plan and status (yor_hub)

Owner: the Claude agent on the **Pi** (`AI4CE-YOR`, 192.168.123.55, Tailscale 100.122.166.69).
Works on branch **`pi`**. The Orin agent works on branch **`orin`** and writes
[ORIN.md](ORIN.md). Each side only edits its own `.md`; to ask the other side
for something, add it under **Requests to Orin** here (or read **Requests to
Pi** in ORIN.md). Shared changes (API contract, common code) land on `main`.

## Goal

Replace "ssh in → `./create_windows.sh` → run panes left to right → rerun CAN
setup if an adapter came up late" with a web GUI that starts at boot.
From any browser on the LAN or Tailscale you can:

- see CAN / arm / lift / neck / host health at a glance
- start and stop `yor.py`, a teleop client, or a policy client, in the right order
- open a live terminal into any running process (and keep using `tmux attach` over ssh)
- spin up a Claude agent in a tmux window and talk to it from the browser
- see and control the Orin from the same page (it runs its own hub)

## Architecture

```
browser ──http/ws──► Pi hub :8080 ──http/ws──► Orin hub :8080
                       │                          │
                  tmux session "yor"         tmux session "yor"
                  windows: can, yor,         windows: cycle server,
                  teleop_*, agent-N, ...     policy server, agent-N, ...
```

- **yor_hub** = one Python package (FastAPI + uvicorn), the same code on both
  machines. A per-host YAML (`config/pi.yaml`, `config/orin.yaml`) lists
  services, checks, and peers. The only difference between hosts is config
  plus host-specific check types.
- **Every service is a named tmux window** in session `yor`, with
  `remain-on-exit on` so the final output and exit code stay visible.
  `tmux attach -t yor` over ssh still works.
- **Stop = Ctrl-C first** (`send-keys C-c`), wait `stop_timeout`, then SIGTERM, then kill the window.
  yor.py's own shutdown path must run (arms, lift stop).
- **Web terminal**: xterm.js (vendored, works offline) ↔ websocket ↔ pty
  running `tmux new-session -t yor` (a grouped session, so each browser tab
  has its own current window), destroyed when the tab closes.
- **Hub restart does not kill services**: systemd `KillMode=process`, so the tmux
  server outlives the hub.
- **Nothing that moves the robot autostarts.** Only CAN preflight autostarts:
  it is what you do by hand today, and it does not enable motors.
- **Auth**: a token in `~/.config/yor_hub/token`. Open `http://host:8080/?token=…`
  once and a cookie is set. Hub-to-hub calls send `Authorization: Bearer <token>`.

## Phase 1 (done 2026-11-20, Pi; installed as `yor-hub.service`)

- [x] repo, remote, venv (`.venv`, python3.13: fastapi, uvicorn, pyyaml)
- [x] vendored xterm.js 5.5.0 + addon-fit
- [x] `yor_hub/tmux.py`: session/window management, status from `list-panes`
- [x] `yor_hub/services.py`: start/stop/restart, requires/ready gating, autostart, per-service editable args
- [x] `yor_hub/checks.py`: `can` (ip -json: up, bus state, bitrate), `tcp`, `path`
- [x] host stats: CPU temp, load, mem, disk free for `teleop_data/`, Pi throttling
- [x] `yor_hub/terminal.py`: pty ↔ websocket into a tmux window
- [x] `yor_hub/app.py`: REST + ws, token auth, static UI
- [x] UI: service cards (state, start/stop, args box, open terminal), health strip, peer tile
- [x] `scripts/can_preflight.sh`: retries `~/YOR/extra/setup.sh` until all CAN ifaces are up and arms answer (gives up after a timeout)
- [x] `systemd/yor-hub.service` + `scripts/install.sh` (system unit, `User=yor`, `KillMode=process`)

Pi services (initial `config/pi.yaml`):

| id | cmd (cwd `~/YOR`, conda `yor-nero`) | requires | ready check |
|---|---|---|---|
| `can` | `yor_interface/scripts/can_preflight.sh` (oneshot, autostart) | – | can0, can_left, can_right up |
| `yor` | `python robot/yor.py {args}` | can0, can_left, can_right | tcp :5557 |
| `whole_body` | `python robot/whole_body_teleop.py {args}` | yor_rpc | tcp :8443 |
| `vr_teleop` | `python robot/teleop/vr_teleop.py {args}` | yor_rpc | – |
| `joystick` | `python robot/teleop/joystick.py` | yor_rpc | – |
| `telestick` | `python robot/teleop/telestick.py` | yor_rpc | – |
| `agent` | `claude` (multi-instance: agent-1, agent-2, …) | – | – |

## Running it

```bash
scripts/install.sh                 # venv + systemd unit + start (Pi: config/pi.yaml)
sudo systemctl restart yor-hub     # after code/config changes (robot processes survive)
journalctl -u yor-hub -f           # hub log
tmux attach -t yor                 # the processes themselves; logs also in ~/.local/state/yor_hub/logs/
.venv/bin/python -m yor_hub --config config/pi.yaml --port 8099   # dev instance (use another tmux_session!)
```

Open `http://192.168.123.55:8080/?token=$(cat ~/.config/yor_hub/token)` once per browser.
Services are added or changed in `config/pi.yaml` only; no code change needed.
Extra schema keys beyond the table below: `conflicts` (blocked while these are
ok/running, e.g. CAN setup is blocked while yor.py runs because setup.sh bounces
the arm buses), `stop_timeout`, `description`, `env` (key into `env_setups`).

## Phase 1.5: unified front hub (next, Pi)

- [ ] multi-host proxy: `/api/hosts`, `/api/state/all`, `/api/hosts/{host}/*`, ws term proxy
- [ ] UI: one section per host, Orin cards and terminals through the proxy, offline banner
- [ ] cross-host `requires` (`orin:...`)
- [ ] service `params` (select from `options_from: cycles`), remembered per service
- [ ] cycle editor UI (add / rename / delete cycles; add / remove / reorder / edit subtasks)

## Later phases

2. Diagnostics: passive AF_CAN listeners (frame rate per bus, Nero 0x251–0x257
   feedback present), lift / neck serial present, SameHand bridge, `:5558`
   episode stream activity, plots.
3. Federation: Pi UI shows the Orin hub's services and terminals, proxied through
   the Pi hub (`/api/peers/orin/...`, ws too), so the browser only needs the Pi.
4. Presets: "data collection" = can → yor → whole_body on the Pi + cycle server
   on the Orin, as one click. Same for "policy deploy".
5. Optional: systemd-networkd `[CAN] BitRate=1M` so adapters configure on hotplug.

## Shared API contract (v0.2, 2026-11-20; every hub implements v0, the front hub adds the multi-host part)

All JSON. Auth: cookie `yor_hub_token` or header `Authorization: Bearer <token>`.

```
GET  /api/info                -> {host, label, version, peers:[{name,url}]}
GET  /api/state               -> {host, time, services:[Service], checks:{name:Check}, stats:Stats}
POST /api/services/{id}/start   body {args?: str, params?: {name: str|null}, force?: bool} -> Service (409 if requires unmet and not force)
POST /api/services/{id}/stop    -> Service
POST /api/services/{id}/restart body {args?: str} -> Service
POST /api/services/{id}/clear   -> Service      (removes the dead window)
POST /api/agents                body {cwd?: str} -> Service   (new claude window agent-N)
GET  /api/services/{id}/log?lines=N -> {text}    (tmux capture-pane scrollback)
WS   /ws/term/{window}         binary frames out = pty bytes;
                               text frames in = {"t":"i","d":str} | {"t":"r","cols":int,"rows":int}

Service = {id, label, group, kind: "daemon"|"oneshot"|"agent",
           state: "stopped"|"running"|"ready"|"exited"|"failed",
           exit_code: int|null, pid: int|null, started_at: float|null,
           args: str, requires: [str], blocked_by: [str], ready_check: str|null,
           window: str|null}
Check   = {ok: bool, level: "ok"|"warn"|"error", detail: str, data?: {}}
Stats   = {cpu_temp_c, load: [1,5,15], mem_used_pct, disk_free_gb, extra: {}}
Service += {params: [{name, label, type: "select", options: [str], value: str|null}]}   (v0.2)
```

Multi-host (v0.2, implemented by the **front** hub only, i.e. the Pi):

```
GET  /api/hosts                     -> [{name, label, url|null, online, detail, self: bool}]
GET  /api/state/all                 -> {hosts: {name: State | {online:false, detail}}}  (peer state cached ≤1 s)
ANY  /api/hosts/{host}/{rest...}    -> proxied to that host's /api/{rest} with its token (host=self: local)
WS   /ws/hosts/{host}/term/{window} -> proxied to that host's /ws/term/{window}
```

Cycles (v0.2, a node with `cycles: {file}` in its config, i.e. the Orin; see ORIN.md):

```
GET    /api/cycles          -> {file, mtime, cycles: {name: [subtask]}}
PUT    /api/cycles/{name}   body {subtasks, rename_to?} -> {cycles}
DELETE /api/cycles/{name}   -> {cycles}
```

Cross-host `requires`/`conflicts` refs look like `orin:collect_preview` or
`orin:service:collection`. The front hub evaluates them; a node ignores refs to other
hosts. The port is per-host (`host.port`): Pi 8080, Orin 8090 (collect.py's preview
owns the Orin's 8080).

State meaning: `running` = process alive but its ready check isn't passing yet
(or it has none). `ready` = ready check passing. `exited` = exit code 0
(normal end for a oneshot). `failed` = non-zero exit.

Host-specific stats go in `Stats.extra`: Pi `throttled`; Orin `gpu_util`,
`gpu_mem`, tegrastats fields.

## Network

- Pi 192.168.123.55 (Wi-Fi), Orin **192.168.123.116** (it was .117 for a while;
  all references in ~/YOR were updated 2026-11-20).
- Hub port: Pi **8080** (front, serves the UI), Orin **8090** (node). Already in use, so avoid:
  Pi 5557 (yor RPC), 5558 (episodes), 8443 (VR); Orin 8080 (collect.py preview), 5551,
  5559 (cycle announce), 8444 (video), 5580/5583 (pi-atlas observer).
- The Pi can't ssh to the Orin. Hubs talk over HTTP, so none is needed.

## Requests to Orin

- See "NEXT: unified hub" at the top of ORIN.md (2026-11-20): token handoff to
  `~/.config/yor_hub/peers/orin.token`, systemd install, Orin stats, cycles API.
- Keep to the API contract above. If you need a change, propose it under
  "Requests to Pi" in ORIN.md, not by silently changing the contract.

## Log

- 2026-11-20: repo created, plan written, phase 1 started.
- 2026-11-20: phase 1 done on `pi`, merged to `main`. Installed and enabled on the Pi;
  CAN preflight ran at service start (both arms detected). Orin tile shows
  "unreachable" until the Orin hub runs on :8080.
- 2026-11-20: merged `orin` (orin.yaml, automount-safe `mount` check) into main. Orin
  hub runs on :8090. Plan changed to one front hub: the Pi serves the UI and proxies
  the Orin. Proposal and cycles request are in ORIN.md.
