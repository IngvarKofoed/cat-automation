# Cat Automation

A camera + computer-vision system at a cat door that identifies each resident cat
versus strangers and tracks who is in or out. Early prototype on a trusted home
LAN (no auth between components; door actuation deferred).

- **What & why:** [`docs/CONCEPT.md`](docs/CONCEPT.md)
- **How it's built:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- **Change log:** [`docs/CHANGELOG.md`](docs/CHANGELOG.md)

Two tiers on the LAN: a thin **edge** (Raspberry Pi + camera — a pure HTTP server
streaming MJPEG) and the **compute** brain (an NVIDIA PC doing all the vision, the
event store, and the dashboards). The Pi only ever listens; the PC connects to it.

## Edge — Raspberry Pi camera node

### Run it directly (dev / first boot)

```sh
./edge.sh                      # serves http://<pi>:8000
CAT_EDGE_PORT=9000 ./edge.sh   # override the port
```

`edge.sh` bootstraps a `.venv` from `edge/requirements.txt` on first run (on Linux
it builds the venv with `--system-site-packages` so it can import the apt-installed
`python3-picamera2`), bakes the `git describe` version into the process, then
launches the server. Surfaces once it's up:

- `GET /` — the camera config UI (clip, rotation, fps, focus, motion tuning)
- `GET /stream` — continuous MJPEG; `GET /frame` — one still
- `GET /status` — motion + camera-health + host CPU/mem snapshot

### Run it under systemd (production, on the Pi)

The edge is meant to run supervised: a wedged camera makes the watchdog exit the
process, and systemd restarts it fresh (`Restart=always`). Install the unit:

```sh
sudo cp deploy/cat-edge.service /etc/systemd/system/
# The unit assumes User=ingvar and /home/ingvar/cat-automation — edit it if your
# login or checkout path differ, then:
sudo systemctl daemon-reload
sudo systemctl enable --now cat-edge      # start now + on every boot
```

Everyday control:

```sh
sudo systemctl restart cat-edge           # after a git pull / config change
sudo systemctl stop cat-edge              # stop (e.g. to run ./edge.sh by hand)
systemctl status cat-edge                 # up/down, restart count, last exit code
```

### Reliability knobs (env, optional)

Operational tuning — not persisted camera config. Set via a systemd `Environment=`
line (or the shell for a manual run); each falls back to its default:

| Env var | Default | Meaning |
|---|---|---|
| `CAT_EDGE_WATCHDOG_S` | `20` | Restart if no successful grab for this long (after a first frame). |
| `CAT_EDGE_BOOT_GRACE_S` | `60` | Restart if the camera never produces a first frame within this long. |
| `CAT_EDGE_STREAM_STALL_S` | `15` | A `/stream` handler sheds itself after this long with no frame sent. |

### Logs & spotting a crash

Under systemd everything goes to the journal — this is the crash record, and it
also captures crashes the app can't log itself (OOM kill, segfault):

```sh
journalctl -u cat-edge -f                 # follow live
journalctl -u cat-edge -b                 # this boot
journalctl -u cat-edge | grep -Ei 'stall|exited|started'   # restart history + cause
journalctl -k | grep -i oom               # kernel OOM kills
```

`systemctl status cat-edge` shows the restart count and the **last exit code**,
which names the cause without reading logs:

| Exit | Meaning |
|---|---|
| `70` | Camera wedge — watchdog fired after a frame had been seen. |
| `71` | Camera never produced a first frame within the boot grace. |
| `1` | Unhandled exception (traceback is in the journal just above the restart). |
| killed by signal (`9`/`SIGKILL`, `11`/`SIGSEGV`) | OOM kill or native crash. |

A healthy edge logs an `edge alive: …` heartbeat every ~30 s; routine `GET /stream`
access lines are suppressed so a reconnect storm can't bury the real signal.

**Keep history across reboots (optional).** Pi OS keeps the journal in tmpfs, so a
full power-cycle erases it (a plain process restart does not). To persist it:

```sh
sudo mkdir -p /var/log/journal
sudo systemctl restart systemd-journald
# then cap the size in /etc/systemd/journald.conf, e.g. SystemMaxUse=200M
```

## Compute — NVIDIA PC (brain)

```sh
./compute.sh                   # Linux / macOS
./compute.ps1                  # Windows (the dedicated collection PC)
```

Bootstraps `.venv-compute`, resolves the edge URL (`CAT_PI_URL`, default
`localhost:8000`), and serves the workbench UI on `:8001` (`/admin`) with the user
dashboard at `/`. See [`compute/CLAUDE.md`](compute/CLAUDE.md) for the details.
