# Hermes Agent (WebUI) for Home Assistant

[Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research with the [Hermes WebUI](https://github.com/nesquena/hermes-webui) as an app in your Home Assistant sidebar. Home Assistant handles the sign-in, and the agent can read and control your home.

**Claude Pro can work too, as an experimental feature:** you can turn on an optional provider and sign in with your own Claude subscription. It worked on a Pro plan in our tests, including a chat on a Raspberry Pi 5 that used the Home Assistant tools, and Anthropic could change that at any time. It's off by default, and [what it involves](hermes_webui/DOCS.md#claude-subscription) is worth reading first, including a possible risk for your account.

To set it up:

1. Open the app. When the setup wizard appears, choose **Skip setup** (**Überspringen** in German), because this provider isn't in the wizard.
2. On the app's **Configuration** tab, turn on `claude_subscription` and `enable_terminal`, save, and restart the app.
3. Open the app again. In a session, type `/terminal` to open the terminal.
4. Sign in with Anthropic's own flow:
   1. Run `claude auth login`. The terminal shows a long URL and then waits for a code.
   2. Copy the whole URL, and open it in a browser. It can be any browser, also on another device.
   3. Sign in with your Claude account, and confirm the access on Anthropic's page.
   4. The page then shows a code. Copy it, paste it into the terminal, and press Enter.
   5. Run `claude auth status` to check it. It should show `"loggedIn": true`.

   The code and the URL belong to your account, so please don't share them. The app never sees your password.
5. Run `hermes model`, and pick **Claude Subscription DirectSDK (Experimental)** near the end of the list, not **Anthropic**. Pressing Enter twice then keeps Sonnet and the suggested reasoning level.

**Status:** first release, 0.1.0. Tested on a real Raspberry Pi 5 (8 GB) with a separate test installation; see the changelog. This is an independent project, not made or endorsed by Nous Research or Anthropic.

## Main features

- **Web UI in the sidebar:** you open the WebUI from Home Assistant, signed in as you already are. There's no second login and no open port.
- **Home Assistant ready:** the official Hermes plugin is built in and connected automatically, with no token to create. You can use your own long-lived token instead.
- **Your choice of model:** pick any provider Hermes supports in the setup wizard, with your own API key or your own endpoint.
- **Optional Claude subscription:** an experimental, opt-in route through the official Hermes plugin and Anthropic's unmodified Claude Code CLI. It's off by default, and you sign in through Anthropic's own flow in the terminal. Please read [what this involves](hermes_webui/DOCS.md#claude-subscription) first.
- **Optional web terminal:** off by default, for administrators only, and completely unreachable while it's off.
- **Official Hermes image:** built on `nousresearch/hermes-agent`, with Hermes, the WebUI, the plugins and the Claude Code CLI pinned to exact versions and tested together.
- **Nothing changes by itself:** all code is fixed when the app is built. Nothing is downloaded or installed when it starts, and updates arrive only as new app versions, so every update is one you can see and roll back.
- **Your data stays small and safe:** only your data is kept, in the app's folder. Caches and logs stay out of backups, and a backup stops the app briefly so that the database stays consistent.
- **Built with security in mind:** the WebUI only accepts connections from Home Assistant, the browser's security rules stay in place, and the Home Assistant token never lands in a file on your shares.

## Versions

| Component | Version |
|---|---|
| Hermes Agent | `v0.22.7` (official image, pinned by digest) |
| Hermes WebUI | `v0.52.544` |
| `homeassistant` plugin | 2.0.1 |
| `claude-subscription-directsdk` plugin | 0.3.4 (commit `e7a218f`), used only when you turn it on |
| Claude Code CLI | 2.1.316, used only when you turn it on |

The agent and the WebUI are a pair, because the WebUI runs the agent inside its own process. They're always updated and tested together.

## Differences from the original

This project started as a fork of [hermes-ha-addon](https://github.com/WolframRavenwolf/hermes-ha-addon) by Wolfram Ravenwolf, with a different architecture. Compared with its version 1.3.10:

| Area | Original | This app |
|---|---|---|
| **Interface** | Hermes dashboard and terminal, behind a landing page | Hermes WebUI directly in the sidebar |
| **Where the code lives** | Installed at start by `git clone`, and kept with your data | Fixed in the image; nothing is installed at start |
| **Updates** | App and agent separately; the source can update itself at start with `auto_update` | Only new app versions, with `hermes update` blocked |
| **Version control** | Any branch or tag through `git_url` and `git_ref` | One tested set of versions per app version |
| **Backups** | Agent data plus source code and tools, with some folders left out, plus the built image | Your data only, plus the built image; the app stops for a consistent copy |
| **Access** | Sidebar, plus direct LAN ports with HTTP and HTTPS and an optional password | Sidebar only, with no ports of its own |
| **Sign-in** | Home Assistant, or an access password on the direct ports | Home Assistant only |
| **Home Assistant access** | A long-lived token you create and enter in the options | Automatic through the Supervisor, with the token in the running process only, never in a file on your shares |
| **Claude subscription** | Not included | Optional, off by default |
| **Terminal** | ttyd with tmux | The WebUI's built-in terminal, blocked completely while it's off |
| **Folders it can reach** | App config, `/share` and `/media` | App config only |
| **Scheduled tasks** | Run by the gateway | Run by the gateway, as a permanent service; failures can appear as Home Assistant notifications |
| **Messaging and APIs** | Gateway for messengers, OpenAI-compatible API, Desktop backend | Messengers that connect outward, such as Telegram, set up by you in the terminal; none by default. No API server, webhooks or other inbound ports |
| **Multiple profiles** | Yes | No, one profile |
| **Testing** | Unit tests | Unit tests, a simulated Home Assistant, a real Supervisor with a real backup and restore, and a real Raspberry Pi 5 |

### Trade-offs

- **The first install and every update are built on your device.** That downloads about 1.3 GB and takes a while on a small device. On a Raspberry Pi 5 the first build took about 5 minutes, mostly the download, and a rebuild under a minute; a slower connection or storage takes longer.
- **Every backup holds the built image, about 1.3 GB.** The upside is that restoring needs no rebuild and no download. Home Assistant Cloud keeps one backup of under 5 GB, so you may want to leave the app out of automatic backups.
- **The app is large,** about 3.6 GB unpacked, partly because the official image contains Chromium and ffmpeg, which can't be removed from it.
- **New versions of Hermes arrive only through app updates,** so they can arrive later than Hermes' own releases.
- **No inbound messenger webhooks, API server, MCP or multiple profiles in this version.** Messengers that need an inbound port can't receive messages.
- **A running chat or cron job stops during a backup,** because the app stops for it.
- **The terminal needs internet access in your browser,** because the WebUI loads its display from `cdn.jsdelivr.net`.
- **The agent has real power.** With a model connected, it can control your Home Assistant, so choose the tasks you give it with care.

## Installing in Home Assistant

1. Create a full Home Assistant backup.
2. Add this repository under **Settings > Apps > Install app > ⋮ > Repositories**, and install **Hermes Agent (WebUI)**. Home Assistant builds it on your device.
3. Start the app, turn on **Show in sidebar**, and turn on **Watchdog**.
4. Open it from the sidebar and follow the setup wizard.

The full guide is in the [documentation](hermes_webui/DOCS.md), which Home Assistant also shows on the app's page.

## Updates

New versions of Hermes and the WebUI aren't taken over automatically. Each update is built as a new app version and has to pass the same checks as the first one, then Home Assistant offers it as a normal app update. Changes are listed in the [changelog](hermes_webui/CHANGELOG.md).

## Layout

```
.
├── repository.yaml        # Home Assistant app repository
├── hermes_webui/          # The app
│   ├── config.yaml        # Manifest and options
│   ├── Dockerfile         # Base image and pinned versions
│   ├── managed-config.yaml # Hermes settings the app enforces
│   ├── DOCS.md            # Documentation, shown in Home Assistant
│   ├── CHANGELOG.md
│   ├── translations/      # Option labels
│   └── rootfs/            # s6 services, nginx and wrappers
├── tests/
│   ├── gate.sh            # All automated checks on this machine
│   ├── harness/           # A simulated Supervisor and Ingress, for the fast loop
│   └── testbed/           # A real Supervisor, the gate before a release, and pi-gate.sh for a real Pi
└── LICENSE
```

## Credits

This project started as a fork of [hermes-ha-addon](https://github.com/WolframRavenwolf/hermes-ha-addon) by Wolfram Ravenwolf. Its `.env` handling, parts of its nginx template, its translation format and its test style live on here.

It runs [hermes-agent](https://github.com/NousResearch/hermes-agent) by Nous Research and [hermes-webui](https://github.com/nesquena/hermes-webui) by nesquena, both under the MIT licence.

## Licence

MIT, see [`LICENSE`](LICENSE). The image also contains the Claude Code CLI, which is under Anthropic's own terms.
