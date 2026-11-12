# Test harness

A simulated Supervisor and Ingress around the real app image, for the fast loop. The real gate before a release is still [`tests/testbed/`](../testbed/README.md), with a real Supervisor.

Everything here uses fake tokens, and needs Docker and a built image. It needs no internet. No file in this folder may be named `config.*`, because the Supervisor parses those at any depth.

## Quick start

```bash
docker buildx build --platform linux/amd64 --load -t local/hermes_webui:0.1.0-amd64 hermes_webui/
python3 tests/harness/smoke.py                 # all checks; removes everything afterwards
python3 tests/harness/harness.py up            # or start it and look around
python3 tests/harness/harness.py down
```

`harness.py up` prints the addresses. Open `http://localhost:18123/` in a browser, also from Windows through WSL2 localhost forwarding: that page frames the app as the sidebar panel does.

## What runs

Everything sits on the Docker network `hermes-harness`, subnet `172.30.32.0/23`.

| Container | Address | Plays |
|---|---|---|
| `hermes-harness-ingress` | `172.30.32.2` | Core and the Supervisor's Ingress: serves the app under `/api/hassio_ingress/TESTTOKEN/`, sets `X-Ingress-Path` and `X-Remote-User-*`, and drops client copies |
| `hermes-harness-app` | `172.30.33.10` | The app, started as the real Supervisor starts it |
| `hermes-harness-ha` | `172.30.32.3`, names `supervisor` and `homeassistant` | The Core API at `http://supervisor/core/api/` and, on port 8123, `http://homeassistant:8123/api/`. It checks the Bearer token, as the real ones do, and keeps every service call's data, so tests can read the notifications the app created. Its WebSocket API answers the login, `subscribe_events` and empty registry lists |
| `hermes-harness-model` | `172.30.33.250`, name `model-stub` | An OpenAI-compatible model that streams delayed chunks |

All four run from the image under test, which has nginx and Python, so the harness pulls nothing.

| Host port (localhost only) | Kind of user |
|---|---|
| `18123` | With a username |
| `18124` | Without a username: no `X-Remote-User-Name` |
| `18125` | No `X-Remote-User-*` at all |

`/data` and `/config` are bind mounts under `/tmp/hermes-harness`, or wherever `HERMES_HARNESS_STATE` says. Set `HERMES_HARNESS_IMAGE` or `--image` to test another image, for example an arm64 one under emulation.

## Files

| File | What it does |
|---|---|
| `harness.py` | `up`, `restart`, `status`, `logs`, `payload`, `exec`, `down`. `restart` recreates the app on the same data, as every Supervisor stop and start does. `--option NAME=JSON` sets options |
| `smoke.py` | The smoke test of `CLAUDE.md` section 6. Every check prints PASS or FAIL with what it measured. `--only SECTION` runs parts on a running harness, `--keep` leaves it running |
| `backup_payload.py` | What a Supervisor backup would contain, with `backup_exclude` applied the Supervisor's way to host-style paths |
| `webui_client.py` | A small webui client: JSON, SSE, one chat turn, one terminal session. It also works through real Ingress |
| `stubs/` | The HA API stub and the model stub |
| `ingress/ingress-sim.nginx.conf` | The Ingress simulator |
| `lint.sh` | `shellcheck`, `yamllint` and `hadolint` through pinned images |

**Gateway and cron sections:** `gateway`, `cron`, `gateway_restarts`, `gateway_isolated` and `gateway_exit_78` need about 15 minutes, because cron jobs fire at minute boundaries. They check the wait for the setup wizard, the gateway's environment and HA connection, `no_agent` and agent jobs that fire by themselves, one HA notification per failure, jobs made in a WebUI chat (#87932), listeners, `kill -9`, a stop during a running job, fake Telegram and listener settings in `.env`, and the exit-78 pause.

The model stub follows the last user message: `CALL_TOOL <name> <json>` answers with a tool call, and `FAIL_HTTP <status>` with that error. Anything else streams `chunk-1 ... chunk-8`.

## What the simulator can't show

- **Real Ingress details:** the simulator is nginx, not the Supervisor's Python proxy. It does send bodies chunked when asked to, and it forwards the Host header as the Supervisor does.
- **The Supervisor's option validation, watchdog, backups and restores.** `backup_payload.py` calculates the payload but packs nothing.
- **A real browser.** Use the testbed's sidebar panel for that.
- **Core's WebSocket API, beyond a login and a subscription.** The stub sends no events and has empty registries, so the HA tools' area filter and the gateway's event handling aren't tested here.
- **Real messengers.** Fake messenger tokens run only with the app on no network at all (`isolated=True` in `harness.py`), so nothing reaches a messenger's servers. The Home Assistant stub is then out of reach too.
- **A real exit code 78.** Agent v0.22.7 exits with it only for a token that another gateway holds while no platform connects, and the Home Assistant platform connects here. `smoke.py` runs the real `finish` and `run` under the real s6 with a stand-in that exits 78 once.
- **`image.tar`:** a local build adds about 1.28 GB to every real backup.

## Claude checks

`claude_smoke.py` checks the opt-in Claude subscription feature against the built image, logged out, with fake values only.
