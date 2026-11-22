# Changelog

All notable changes to the Hermes Agent (WebUI) Home Assistant app are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions match `version` in `config.yaml`.

This app is a fork of [WolframRavenwolf/hermes-ha-addon](https://github.com/WolframRavenwolf/hermes-ha-addon) at `v1.3.10`, with a different architecture. For earlier versions, see the changelog in that repository.

## [0.1.1] - 2026-11-21

A fix release. The agent and the WebUI stay at the tested pair.

### Component versions

| Component | Version |
|---|---|
| hermes-agent | `v0.22.7` (official image, pinned by digest, unchanged) |
| hermes-webui | `v0.52.544` (unchanged) |
| `homeassistant` plugin | 2.0.1 (commit `6313fad`, was `4d492b1`) |
| `claude-subscription-directsdk` plugin | 0.3.5 (commit `6313fad`, was 0.3.4 at `e7a218f`) |
| Claude Code CLI | 2.1.325 (was 2.1.316) |

The marketplace page of the Claude plugin named 0.4.0 (`72eb3b8`) on 2026-11-20, a day after it appeared, and that plugin's own changelog still lists 0.4.0 as unreleased. This release keeps the tested 0.3.5. The move to 0.4.0, whose telemetry default changes, comes with a later release.

### Changed

- The Claude subscription plugin moves to 0.3.5 (commit `6313fad`, as its marketplace page names), which adds Fable 5.2 and makes it the `fable` alias, and lists Mythos 5.2 for verified organizations. The Claude Code CLI moves to 2.1.325, which Fable 5.2 needs (2.1.321 or newer) and which includes a security fix for compressed responses.
- New app icon: a winged sandal on a teal circle, drawn for this app. It replaces the gold robot taken over from Wolfram's fork.
- `ha_get_state` also returns the times in Home Assistant's local time zone (`last_changed_local` and similar). To learn the zone, the plugin reads Home Assistant's `/api/config` about once an hour (same plugin move).
- DOCS and the README give the measured times for a Raspberry Pi 5 with a microSD card: about 22 minutes for the first install and 3 to 5½ minutes of downtime per backup, against about 5 minutes and about a minute on an SSD.

### Added

- An app logo (`logo.png`) for the app's detail page.

### Fixed

- The WebUI's "Check now" button and the update channel switch no longer contact GitHub or show an update banner. The update check answers "disabled" for every request.
- `hermes update` after an option with a value, such as `hermes -p work update` or the abbreviated `hermes --mod x update`, now gets the app's message instead of the base image's `docker pull` advice. `hermes -z update`, a one-word prompt, is no longer refused.
- Starting, stopping or restarting the gateway from the WebUI now explains that the app manages the gateway, instead of saying the app runs without one.
- Home Assistant actions with extra settings, such as "turn the light on at 30 %", now work on the first try: the settings are no longer refused when the model passes them as an object. The built-in `homeassistant` plugin moves to commit `6313fad` (still version 2.0.1, the same commit as the Claude plugin). Before, the first such call was refused, and only a model that retried with a text form got through. Seen, and confirmed fixed, on a real Raspberry Pi 5.
- A device that confirms a switch a moment after Home Assistant answers is no longer reported as "already in that state". The Home Assistant tools now re-check the state for up to 1.5 s, and report an error if the device never reaches the requested state (same plugin move).
- The Claude setup in DOCS and the README now says to start a new chat after choosing the model. A chat opened earlier keeps the WebUI's default model `openai/gpt-5.4-mini` and fails with `Model not found`; DOCS also lists this message under Claude troubleshooting.

### Security

- The Home Assistant tools refuse domain, service and entity names with a trailing line break, such as `shell_command` followed by one. Such a name could slip past the plugin's list of blocked domains (for example `shell_command`, `python_script` or `hassio`) and its entity allow and deny lists. Part of the `homeassistant` plugin move to `6313fad`.
- A blocked script can no longer run when the same call also names an allowed entity. The plugin now checks every target of a call against its entity policy (same plugin move).
- Requests whose path holds raw bytes outside printable ASCII are refused with 400. Some of these bytes let a request from inside the system slip past the app's blocked paths; through Home Assistant's Ingress they were already refused.

### Verified

All numbers are measured unless marked otherwise. Evidence: `docs/verification/0.1.1-qa.md` and `0.1.1-pi.md` in the development repository.

- **Real Raspberry Pi 5 (4 GB, Home Assistant OS 18.5, AppArmor on, microSD card, Wi-Fi), 2026-11-20:** `pi-gate.sh` GREEN on the final build.
  - Build on the device: 1,351 s for the first install, 1,042 s for a rebuild. The SD card is the limit; on the SSD of 0.1.0 it was 310 s.
  - RAM: 177 MiB idle, 463 MiB after a chat, 585 MiB with the gateway after scheduled tasks.
  - Real Ingress, the logged-out Claude checks, and the scheduled-task checks including restore and catch-up all passed.
  - Cold backup: payload 8 KB fresh and 3.6 MB after setup, plus `image.tar` 1.29 GB. The container was down 196 s and 322 s in two runs on the SD card.
- **By hand on that Pi:**
  - A real chat with the Claude subscription (Pro, Sonnet 5.5, CLI 2.1.325, plugin 0.3.5) answered through the Home Assistant tools.
  - "Turn the WLED light on at 30 %, then off" succeeded on the first call with the new `homeassistant` plugin. With the old one, the first call was refused.
- **amd64 (WSL testbed with a real Supervisor), 2026-11-20:** `gate.sh` GREEN.
  - Unit tests 242 OK, harness smoke 217/0, Claude smoke 95/0.
  - Install, refused options, real Ingress, cold backup (payload 8 KB), Claude checks and scheduled tasks all passed.
- **Update from 0.1.0:**
  - In the testbed, through the real Supervisor: 70 s, settings and files kept.
  - Migration test with real chats, files, `.env` keys and a scheduled task: 25/0. No schema change.
- **Review:** an independent QA review of all 0.1.1 changes was GREEN; its findings are fixed.
- **Architectures built:** aarch64 (on the Pi) and amd64. `image.tar` is 1.29 GB (aarch64) and 1.30 GB (amd64).
- **Policy re-check** of the Anthropic pages and the Claude plugin, 2026-11-20: unchanged.
- **Not tested:**
  - a real messenger connection
  - Email and SMS end to end
  - scheduled agent tasks with the Claude subscription (plugin issue #47)
  - the update from 0.1.0 through the public store, which follows after publication
  - an 8 GB Pi with an SSD on this release

## [0.1.0] - 2026-11-12

First version of this fork.

### Component versions

| Component | Version |
|---|---|
| hermes-agent | `v0.22.7` (official image, pinned by digest) |
| hermes-webui | `v0.52.544` |
| `homeassistant` plugin | 2.0.1 (commit `4d492b1`) |
| `claude-subscription-directsdk` plugin | 0.3.4 (commit `e7a218f`) |
| Claude Code CLI | 2.1.316 |

### Added

- hermes-webui as the user interface, through Home Assistant Ingress.
- All code in an immutable image. Nothing is downloaded or installed at container start.
- Home Assistant tools through the Supervisor token, with an optional long-lived token.
- An optional web terminal, off by default.
- An optional Claude subscription provider, off by default.
- Cold backups that hold user data only.
- The Hermes gateway as a permanent service, so scheduled tasks fire by themselves, and missed runs are caught up once after a stop.
- Failed scheduled tasks can be reported as Home Assistant notifications.
- Messengers that connect outward (for example Telegram, Discord, Slack, Matrix), set up in the terminal. None is configured by default, and unknown senders get no answer.

### Changed

- The app folder and slug are now `hermes_webui`.
- Updates arrive only as new app versions. The built-in update paths of Hermes, hermes-webui and Claude Code are closed.

### Removed

- The runtime git install and `auto_update`.
- Multi-profile support, and the original's own gateway launcher, supervisor and logger, as well as the dashboard and Desktop launchers. The gateway itself now runs as an s6 service.
- The API server, inbound webhooks, `access_password`, direct ports, the self-signed CA and htpasswd.
- The landing page, Homebrew, Go, gh, tmux terminals and dotfiles in `/config`.
- The `/share` and `/media` mappings.

### Verified

All numbers are measured unless marked otherwise.

- **Real Raspberry Pi 5 (8 GB, Home Assistant OS 18.4, AppArmor on), test SSD, 2026-11-11 and 2026-11-12:** `pi-gate.sh` GREEN twice.
  - Build on the device 310 s the first time, 9 to 45 s for a rebuild. RAM 166 MiB idle, 464 MiB after a chat, 560 MiB with the gateway after scheduled tasks.
  - Real Ingress, the logged-out Claude checks and the scheduled-task checks all passed: tasks fire every minute, a failing task makes one Home Assistant notification, missed runs are caught up once after a stop and after a restore, and only the Ingress port listens.
  - Cold backup: container down 56 s; payload 8 KB fresh and 3.6 MB after setup (the rest is Hermes' bundled skills), plus `image.tar` 1.27 GB.
  - A real chat with the Claude subscription (Pro, Sonnet 5.5) used the Home Assistant tools.
- **amd64 (WSL testbed with a real Supervisor), 2026-11-12:** `gate.sh` GREEN (unit tests, both smoke tests, install, refused options, real Ingress, cold backup, scheduled tasks), and the watchdog replaced a frozen WebUI after 194 s; restore and reinstall checked earlier; image 1.28 GB compressed, 3.6 GB unpacked.
- **Not tested:** a real messenger connection; Email and SMS end to end; scheduled agent tasks with the Claude subscription (plugin issue #47); the update from a previous version (not applicable to 0.1.0).
