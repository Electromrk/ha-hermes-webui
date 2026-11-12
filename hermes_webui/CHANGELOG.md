# Changelog

All notable changes to the Hermes Agent (WebUI) Home Assistant app are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions match `version` in `config.yaml`.

This app is a fork of [WolframRavenwolf/hermes-ha-addon](https://github.com/WolframRavenwolf/hermes-ha-addon) at `v1.3.10`, with a different architecture. For earlier versions, see the changelog in that repository.

## [0.1.0] - Unreleased

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
