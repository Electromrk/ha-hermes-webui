# Hermes Agent (WebUI)

This app runs [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research with the [Hermes WebUI](https://github.com/nesquena/hermes-webui) by nesquena, in your Home Assistant sidebar.

All code is part of the app's image. Your data stays in two folders, and updates arrive only as new app versions.

## Installation

1. Create a full Home Assistant backup.
2. Add this repository under **Settings > Apps > Install app > ⋮ > Repositories**, and install **Hermes Agent (WebUI)**.
3. Start the app, and turn on **Show in sidebar**.
4. Turn on the **Watchdog** switch on the app's page. It's off by default on every install, and an app can't turn it on by itself. With it on, Home Assistant restarts the app if the WebUI hangs.

The app supports `amd64` and `aarch64`. Home Assistant builds it on your device, which downloads about 1.3 GB and takes a while on a small device.

## First steps: choosing a provider

Open **Hermes Agent** in the sidebar. As long as the app has no `config.yaml`, a setup wizard appears.

1. Pick a provider and a model.
2. Enter that provider's API key, or the URL of your own OpenAI-compatible endpoint.
3. Start chatting.

The key goes into `.env` in the app's folder. You can change the provider later in the WebUI's settings, or with `hermes model` in the terminal.

Good to know:
- **Comments in `config.yaml` disappear** when the WebUI saves that file.
- **Switching provider** in the WebUI forgets the URL of a custom endpoint. Enter it again when you switch back.
- **All Home Assistant administrators share one Hermes profile**, with the same chats, memory and keys. The sidebar entry is for administrators only.

## Options

| Option | Default | What it does |
|---|---|---|
| `enable_terminal` | off | The terminal inside the WebUI. See [Terminal](#terminal). |
| `claude_subscription` | off | The experimental Claude provider. See [Claude subscription](#claude-subscription). |
| `homeassistant_token` | empty | A long-lived token instead of the Supervisor's. See [Home Assistant access](#home-assistant-access). |
| `env_vars` | empty | Environment variables for Hermes, as a list of `name` and `value`. |
| `log_level` | `info` | How much is logged: `debug`, `info`, `warning` or `error`. |

**`env_vars`**
- The values go into `.env` at every start. A name you remove from the option is removed from `.env` again. Keys you added to that file by hand stay.
- Names must match `^[A-Z_][A-Z0-9_]*$`, and values must be a single line. Hermes expands `${NAME}` inside a value.
- Names the app sets itself, such as `HASS_TOKEN`, `HERMES_HOME`, `HERMES_WEBUI_*`, `HERMES_GATEWAY_*`, `PATH` or `HOME`, are refused when you save the option. So are the settings for inbound listeners, such as `WEBHOOK_ENABLED`, `API_SERVER_*`, Telegram's `TELEGRAM_WEBHOOK_*` and listen addresses like `A2A_HOST` or `SMS_WEBHOOK_HOST`, because the app has no port for them (see [Messengers](#messengers)). The error quotes the rule with all of them and repeats the option values you entered, so please don't share a screenshot of it. The app checks again at every start. If such a name got in another way, for example from a restored backup, it stops with a message in its log until you remove the name.
- If such a name is edited into `.env` by hand, it usually overrides the app's value, because Hermes loads that file with override. The app logs a warning at every start and leaves the file alone. Please remove the line. The listener settings are the exception: the app's own values win over `.env`, so those lines have no effect.

**`log_level`** applies to the app's startup messages, nginx and Hermes' own files in `logs/`. It's set by the app, so `hermes config set logging.level` answers that the value is managed. The WebUI's request lines in the app log stay as they are.

## Terminal

Turn on `enable_terminal` and restart the app. Then, in a session in the WebUI, type `/terminal`.

- The terminal runs as the app's runtime user, in the workspace folder. Only Home Assistant administrators can reach it, and no token is in its environment, but like everything that runs as the runtime user it can read the WebUI's token (see Home Assistant access).
- **Your browser needs internet access** for it, because the WebUI loads the terminal display from `cdn.jsdelivr.net`. Without that, the panel stays empty. The rest of the WebUI doesn't need it.
- When the option is off, the terminal isn't reachable. `/terminal` then shows a message that tells you to turn it on.
- **This switch controls your terminal only. The agent's own `terminal` tool is separate** and stays available to the agent, whatever this option says.

## Home Assistant access

The agent can read and control Home Assistant through its tools `ha_list_entities`, `ha_get_state`, `ha_list_services` and `ha_call_service`. A model finds them through its `tool_search` tool. They work the same in a WebUI chat, in a scheduled task and for a messenger.

By default the agent uses the app's Supervisor token, with the rights of a Home Assistant administrator. **So a model you connect here can control your home, as can anything else it runs.**

With `homeassistant_token`, the agent talks to Home Assistant directly, with the rights of the user who created the token. At start, the app tries `http://homeassistant:8123`, then `http://homeassistant:80`, and takes the first that accepts the token. If neither does, it logs a warning and uses the Supervisor's token. That also happens when Home Assistant itself runs on HTTPS.

The log names the mode, never the token. The token is only in the process environment of the WebUI and the gateway, not in `.env`, so it never appears on your file shares. Anything running as the app's runtime user can still read it from there, including the agent's `terminal` tool, your scheduled scripts and any plugin you install.

**Scripts get the Supervisor token.** A script the agent runs, in a chat or as a scheduled task, has `SUPERVISOR_TOKEN` in its environment, but not `HASS_TOKEN` or `HASS_URL`, which Hermes keeps from it. Call Home Assistant's REST API at `http://supervisor/core/api/` with the header `Authorization: Bearer $SUPERVISOR_TOKEN`. That works whether or not you set `homeassistant_token`.

**The gateway's own connection.** The gateway connects to Home Assistant's WebSocket API and subscribes to its state changes. Without watch filters it drops all of them, and it logs a warning about that at every start, which you can ignore. While it's connected, it sends notifications as persistent notifications; otherwise it calls `notify.persistent_notification`, which makes the same kind.

MCP isn't part of this version.

## Scheduled tasks

Hermes runs scheduled tasks (cron jobs) in its gateway, which the app runs all the time next to the WebUI. You can create a task by asking the agent in a chat, in the WebUI's **Tasks** panel, or with `hermes cron create` in the terminal. A task either asks the agent, or runs a script from `scripts/` in the app's folder without the agent (`--no-agent`).

- **Tasks run once the setup is done.** Until the setup wizard has saved a provider, the gateway waits, and the log says `Cron jobs and messengers start once the setup wizard has saved a provider.` It doesn't start earlier, because the wizard can only save when there's no `config.yaml` yet.
- **Missed runs are caught up once.** If the app was stopped when a task was due, for a backup, an update or a restart, a recurring task runs once at the next start. A one-time task that is more than 2 minutes late is skipped.
- **A backup interrupts a running task.** A backup stops the app. The gateway gives a running task up to 30 seconds to finish, then it counts as failed.
- **Keys and models:** each run reads `.env` and `config.yaml` again, so a new API key or model applies to the next run without a restart.

### When a task fails

Every run, its output and its error are in the **Tasks** panel and in `hermes cron list`. The WebUI also shows a short message when a task finishes, while it's open.

The app sends a failure to Home Assistant as a persistent notification titled "Hermes Agent", once per incident, when the task's delivery leads there. An incident is one task failing with one error: if the task fails the same way again, no second notification follows (measured: one notification for two failed runs).

| Created | Delivery | Where output and failures go |
|---|---|---|
| In the **Tasks** panel, or with `hermes cron create` | **Local** (the default) | Nowhere. Only the Tasks panel shows them. |
| In the **Tasks** panel, or with `--deliver origin` | **Origin** | A Home Assistant notification for every output, and one per failure incident. A script that prints nothing sends no output. |
| By the agent in a WebUI chat | **Origin**, that chat | Nowhere. Hermes can't deliver to a WebUI chat yet ([hermes-agent#87932](https://github.com/NousResearch/hermes-agent/issues/87932)). The task shows `unknown platform 'webui'` as its delivery error. |
| Any way | `homeassistant` | A Home Assistant notification |

**So for a task the agent creates in a chat, ask it to send failures to Home Assistant**, which its tool calls `failure_deliver: homeassistant`. For an existing task, run `hermes cron edit <id> --failure-deliver homeassistant` in the terminal; `hermes cron list` shows the IDs. A script that sends its own notifications, through `notify.<service>`, isn't affected by any of this.

## Messengers

None is set up. Hermes can talk to you through Telegram, Discord, Slack, Matrix, Signal, Mattermost, WhatsApp, IRC, ntfy and others. They all connect outward from the app, so no port is opened.

**Setting one up:**
1. Turn on `enable_terminal`, restart the app, and type `/terminal` in a session.
2. Run `hermes gateway setup`, and follow it. It writes the bot token and the settings to `.env`. When it offers to restart the gateway at the end, answer no: that doesn't work in this app, and the next step does it.
3. Restart the app on its page in Home Assistant. The gateway reads `.env` only when it starts, and `hermes gateway restart` doesn't work in this app.

**Who may talk to the bot.** A messenger user gets the same tools as you in the WebUI: the Home Assistant tools, the agent's terminal, your files and the browser. So allow only yourself and people you trust, by their numeric user ID, for example `TELEGRAM_ALLOWED_USERS=123456789`. Without such a list, the bot answers nobody.

- **Never let everyone in.** `GATEWAY_ALLOW_ALL_USERS`, any `*_ALLOW_ALL_USERS=true`, `allow_all_users: true` in `config.yaml`, or a `*` in an `*_ALLOWED_USERS` list lets anyone who finds your bot control your home. The app warns about each of these at every start, by name.
- **`HERMES_YOLO_MODE`** turns off the prompts before risky commands, for messenger users too. The app warns about that as well.
- **Messages from people who aren't on the list get no answer.** The app sets this, so the bot doesn't send them pairing codes either. Add new users to the list instead. This setting lives in the app's managed configuration, which Hermes skips while your `config.yaml` is broken (for example after a bad hand edit), so in that state the bot can send pairing codes again. Fix `config.yaml` and restart the app.
- **The warning `No env user allowlists configured…`** appears at every start without any messenger, too. Hermes counts the gateway's own Home Assistant connection as a messaging platform, and that connection has no user list. You can ignore it; once a messenger has its `*_ALLOWED_USERS` list, it goes away.

**Not available in this version:**
- Everything that needs an inbound connection: Telegram's webhook mode (Telegram works with polling instead), the generic webhook, the API server, Microsoft Graph, LINE, the WhatsApp Cloud API, WeCom's callback mode, BlueBubbles, incoming A2A (agent-to-agent) calls, and receiving SMS. The app has no port that their servers could reach, so they can't work here. The app keeps their listeners off, or on the app's own loopback address (127.0.0.1), even when `.env` or `config.yaml` asks for another address such as `0.0.0.0`.
- Microsoft Teams, Feishu and DingTalk, because the libraries they need aren't in the image.

**Partly available:**
- **SMS (Twilio)** can send messages, for example a task's result, but can't receive any, because Twilio's webhook can't reach the app. Sending hasn't been tested in this app.
- **Email** works by polling: Hermes fetches new mail over IMAP and replies over SMTP, so no inbound port is needed. It needs the four `EMAIL_*` settings that `hermes gateway setup` asks for. Not tested end to end in this app.

**A messenger that can't connect doesn't stop the others.** With a wrong token, or WhatsApp turned on but not paired, the gateway logs an error and keeps running for the other messengers and for scheduled tasks.

Chats with a messenger run in the same Hermes profile, so the WebUI lists them too.

## Your data and backups

| Where | What | Reachable through file shares |
|---|---|---|
| `app_configs/<id>_hermes_webui/hermes/` (the Samba app also shows it under `addon_configs`) | `config.yaml`, `.env`, `SOUL.md`, chats, memory, skills, plugins, workspace | yes |
| The app's private data | Options, the Claude CLI's login | no |

**Your home directory isn't kept.** `~` is `/tmp/hermes-home`, so whatever the agent or you put there is gone after a restart. Keep files in the workspace.

**A backup stops the app for its duration.** Home Assistant stops the app, packs the data and starts it again, which keeps the database consistent. A running chat or cron job is interrupted, and the chat then shows "Response interrupted". On a Raspberry Pi 5 the app was down for about a minute (measured), most of it for packing the image.

Backups contain your data and your secrets, so please encrypt them. They hold `.env` with your API keys and messenger tokens, the values of `homeassistant_token` and `env_vars`, the app's Supervisor token in the backup's own `addon.json`, and the Claude login if you use it.

**Named pipes don't survive a stop.** The app removes them from its folders whenever it stops, because Home Assistant's restore can't copy them and would fail. They hold no data. After a crash, or if the app couldn't start, the app can't clean up: a backup taken right then could fail to restore, so make sure the app starts properly first.

Left out of backups, because they can be regenerated: `logs/`, `cache/`, `tools/`, `installs/` and `environments/` at the top of the `hermes` folder, the model catalog cache `models_dev_cache.*`, the debug dumps `sessions/request_dump_*` and full zips in `backups/`. Folders with those names further down, such as `workspace/proj/tools/`, stay in.

A fresh install has a payload of a few KB. Once the setup is done, Hermes copies its bundled skills into `skills/`, about 3.5 MB. Every backup also holds the image Home Assistant built on your device, about 1.3 GB. Home Assistant Cloud keeps one backup of under 5 GB, so that takes up over a quarter of it.

**Uninstalling keeps your files.** Home Assistant removes the app, its image and its private data, which are the options and the Claude login. `app_configs/<id>_hermes_webui/hermes/` stays, with your chats and with `.env` and your API keys in it. To remove them too, delete that folder through a file share.

If you install the app again on that folder, your chats are back and no setup wizard appears. A key that came from `env_vars` then stays in `.env` as if you had added it by hand, because the record of those names was part of the private data. Delete the line if you no longer want the key.

## Updates

Updates arrive only as new app versions in Home Assistant, because the container is recreated at every start, and because the agent and the WebUI have to match. Every app version is also a point you can back up and return to.

So these are off:
- `hermes update` answers `Updates are delivered as new app versions in Home Assistant.`
- The WebUI's update check and its update, extension, restart, stop and gateway controls. The gateway still runs; the app starts and stops it. To restart or stop, use the app's page in Home Assistant. The **Stop server** button in the WebUI's settings still says that the server stopped, but it keeps running, so just reload the page.
- Hermes' automatic installs and update checks. The app enforces these settings, and `hermes config set` answers that they're managed.

## Your own plugins

You can install plugins with `hermes plugins install <name>` in the terminal. They're your data, in `plugins/` in the app's folder. Before you do:

- This **downloads code from GitHub**, even though everything else in the app is fixed in its image.
- That code **runs next to the Home Assistant token**, with the same access as the agent.
- Its Python dependencies land in `installs/`, which backups leave out. **After a restore they're gone**, and the plugin has to be installed again.

The Home Assistant tools are part of the image. The empty folder `plugins/homeassistant` only stops a false warning, so please leave it there.

## Claude subscription

With `claude_subscription` on, Hermes can use your own Claude Pro or Max subscription as a model provider, through the Claude Code CLI as Anthropic publishes it. The CLI and the plugin `claude-subscription-directsdk` by Nous Research are in the image at fixed versions.

This app is an independent project. Anthropic didn't build it and doesn't endorse it.

**The feature is experimental, and turning it on is your own conscious decision.** Please read this section first.

### What it does

- **One process per request.** Every request starts one `claude` process, which costs latency and CPU on small devices. On a fast desktop processor, without a login, that process took about 0.2 s until it sent its request and peaked at about 200 MB of RAM. A Raspberry Pi takes longer; that hasn't been measured.
- **A fixed caller name.** The plugin always sets `CLAUDE_CODE_ENTRYPOINT=hermes-agent` and `CLAUDE_CODE_DISABLE_ARTIFACT=1`. Its README says that subscriptions meter the 5-hour window by that name, and that the CLI's default name in this mode draws it about twice as fast per token. Anthropic hasn't documented that weighting, and hasn't endorsed the relabelling.
- **A local relay.** For each request, the plugin points `ANTHROPIC_BASE_URL` at a relay on this device, on a random port. The relay forwards only the first Messages request, plus one retry after an expired login (HTTP 401), and rejects further ones locally. It also adjusts the request: it moves a date line, restores tool-result frames and asks for eager tool streaming. The CLI's authorization header passes through it in memory; the README says it's never logged or stored. No external service is involved.
- **Telemetry.** The plugin's setting `claude_code_telemetry` stays at its default, on, so the CLI sends its usage telemetry and fetches feature flags. With it off, the CLI skips the plan-limit fetch and the model list can get shorter.

The caller name and the relay have no setting.

**Scheduled tasks and messengers may not work with it.** The plugin has an open issue saying it fails on the gateway surface ([hermes-official-plugins#47](https://github.com/NousResearch/hermes-official-plugins/issues/47)), and scheduled agent tasks and messenger chats run in the gateway. So with this provider, they may fail, while chats in the WebUI work. This hasn't been tested in this app.

**This may carry a risk for your account.** Anthropic's Consumer Terms restrict automated access unless Anthropic permits it, and whether this plugin's way of running the CLI fits those terms isn't settled. In [`anthropics/claude-code#97074`](https://github.com/anthropics/claude-code/issues/97074) Anthropic hasn't commented, and a user warns that relabelling could count as getting around usage limits.

### Setting it up

1. Open the app. When the setup wizard appears, choose **Skip setup** (**Überspringen** in German), because this provider isn't in the wizard.
2. On the app's **Configuration** tab, turn on `claude_subscription` and `enable_terminal`, and restart the app.
3. Open the app again. In a session, type `/terminal`, and run `claude auth login`.
4. Open the URL it shows in your browser, and sign in.
5. Paste the code back into the terminal.
6. Run `hermes model`, and pick **Claude Subscription DirectSDK (Experimental)** near the end of the list, not **Anthropic**. The model list then starts on Sonnet, the recommended default, so Enter is enough, and Enter also keeps the reasoning level it asks about next. You can also do this in the WebUI, under **Settings > Preferences**: choose a model from that provider's group, then press **Save**. Until you save, nothing is configured, even though Sonnet already shows as selected there, and a chat answers `No LLM provider configured`.

At every start, the app's log says `Claude CLI logged in: yes` or `no`. That comes from the exit code of `claude auth status`, which only looks at the local login. The app logs no account details.

### Costs and limits

- **Limits:** your usage is shared with claude.ai, in 5-hour and weekly windows.
- **Usage credits:** on Pro, Fable and Opus with 1M context draw usage credits. All Fable and Opus entries in this provider's list use the 1M context, and the CLI doesn't ask first in this mode. So on Pro, stay on Sonnet or Haiku.
- **We recommend that you turn off extra usage in your Claude account**, so that a request can't cost more than your plan.
- **Fallback:** the app configures no fallback provider. If a Claude request fails, you see the error, and no other provider is called. If you add `fallback_model` to `config.yaml` yourself, Hermes moves to that provider when Claude fails, for example at a plan limit, and that provider bills you.

### Terms of service

You're responsible for your own account, so please read Anthropic's terms yourself. This notice isn't legal advice.

- [Claude Code: legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)
- [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
- [Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
- [Consumer Terms](https://www.anthropic.com/legal/consumer-terms)
- [Commercial Terms](https://www.anthropic.com/legal/commercial-terms)

The app's own code never asks for, reads or stores your credentials. Only the CLI writes them, during its own login. While a request runs, the plugin's relay passes the CLI's authorization header through in memory, as described above. The CLI is in the image even with this option off, but nothing starts it then.

### Logging out and removing your data

- **Log out:** run `claude auth logout` in the terminal.
- **Where the login lives:** in the app's private data, in `/data/claude`, which file shares can't reach.
- **Remove all of it:** after logging out, run `find /data/claude -mindepth 1 -delete` in the terminal. Uninstalling the app removes it too, but not the rest of your data. See [Your data and backups](#your-data-and-backups).
- **Backups:** the CLI's credentials file `.credentials.json` is part of every backup of this app. Please encrypt your backups, because whoever has one can use your login. Logging out doesn't change backups you already have. After you restore an older one, you may have to log in again.
- **Who can read it:** anything running as the app's runtime user, including the agent's `terminal` tool and any plugin you install. So a model you connect here could reach your login.

### Claude troubleshooting

**HTTP 400, "Third-party apps now draw from extra usage, not plan limits".** Users of Team and Enterprise plans have reported this with the plugin's fixed caller name ([plugin issue #296](https://github.com/NousResearch/hermes-official-plugins/issues/296)). It has no setting in this version. A Pro plan worked in our tests, most recently on 2026-11-11 with the plugin version in this app. This can change on Anthropic's side at any time.

**HTTP 401, "Claude Code is installed but has no usable login".** You aren't logged in, or the login has expired or been revoked. Run `claude auth login` in the terminal, even if the log says `yes`. That message also suggests `CLAUDE_CODE_OAUTH_TOKEN`, `claude setup-token` and the `claude_config_dir` setting. This app doesn't support them: `env_vars` refuses that name, and the login always lives in `/data/claude`.

**HTTP 429, or a message about a limit.** You've used up a window of your plan. Wait until it resets, or pick another provider.

**"refuses conflicting native auth/backend overrides", and a warning in the log.** The plugin won't run while one of these is set: `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` or `CLAUDE_CODE_USE_FOUNDRY`. The log names the ones it found. Remove them from `env_vars` or `.env`. The app never deletes them itself, because they may be yours for another provider. An `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_FOUNDRY_API_KEY` doesn't stop it: the plugin keeps them away from the Claude CLI, so these requests stay on your subscription.

**A warning says `config.yaml still selects the Claude provider`.** The option is off, but that provider is still selected, so chats fail with a provider error. Turn the option on, or pick another provider.

**A warning says `plugins/... is your own copy of the Claude plugin`.** You installed the plugin yourself, for example with `hermes plugins install`. The option doesn't control that copy, so remove the folder to go back to the tested one.

**"Login with Claude Code" in the wizard never finishes.** That's on purpose, whatever the option says. That card is a different feature, which would clear keys from `.env`. Use the steps above instead.

**`claude update` says updates are disabled.** New CLI versions arrive with new app versions.

## Troubleshooting

**The app doesn't start.** Open the app's **Log** tab. A line with `[hermes-init] ERROR` says what's wrong, usually a reserved name in `env_vars`.

**A warning says `.env sets the reserved name ...`.** Remove that line from `.env`. See [Options](#options).

**A warning says `.env sets ..., which lets anyone who finds your messenger bot use it`.** Replace that setting with a list of user IDs. See [Messengers](#messengers).

**Scheduled tasks don't run.** Check that the setup wizard is done: until then the log says that cron jobs start after the setup. A task created in a WebUI chat runs, but its result isn't delivered; see [When a task fails](#when-a-task-fails).

**The log shows `[hermes-gateway] ERROR: The gateway stopped with a configuration error (exit code 78)`.** A messenger setting stops the gateway at every start, usually a bot token that another program or another Hermes already uses. Fix or remove that messenger's lines in `.env` and restart the app. Until then, scheduled tasks don't run. The gateway tries again every 30 seconds.

**The wizard doesn't appear.** It appears only without a `config.yaml`. To run it again, stop the app, rename that file and start the app.

**A chat answers with an error about the provider.** Check the key and the model in the WebUI's settings. Failed calls leave a debug file in `sessions/request_dump_*`. It holds your conversation and the tool definitions, with keys masked.

**A custom endpoint answers 401.** The wizard stores a custom endpoint's key as `OPENAI_API_KEY`, which Hermes doesn't send to other hosts. Add the endpoint with its key to `custom_providers` in `config.yaml` instead, and select it in the WebUI's settings.

**`/terminal` stays empty.** Your browser couldn't load the terminal display from `cdn.jsdelivr.net`. See [Terminal](#terminal).

**Something hangs for 30 seconds right after you close the terminal, then fails.** This is a rare timing problem in the WebUI when a program was still running in the terminal. Repeat the action or reload the page. To avoid it, end the program before you close the terminal.

**The Home Assistant tools report an error.** The log names the access mode at every start. With `homeassistant_token`, a warning there means the token wasn't accepted.

**The WebUI hangs.** With the watchdog on, Home Assistant restarts the app after a few minutes. Otherwise, restart it on the app's page.

## Credits

Built on [hermes-ha-addon](https://github.com/WolframRavenwolf/hermes-ha-addon) by Wolfram Ravenwolf, [hermes-agent](https://github.com/NousResearch/hermes-agent) by Nous Research and [hermes-webui](https://github.com/nesquena/hermes-webui) by nesquena, all under the MIT licence.
