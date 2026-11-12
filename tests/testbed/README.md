# HA testbed

A real Supervisor and a real Home Assistant Core in one privileged container on
this machine, driven headless by `ha-testbed.sh`. It is the official Home
Assistant devcontainer, started with `docker run` instead of VS Code.

- **`tests/harness/`** is the fast loop: a simulated Supervisor and Ingress.
- **`tests/testbed/`** is the real gate before a release: real install, options
  validation, Ingress, watchdog, backup and restore.

## Quick start

```bash
tests/testbed/ha-testbed.sh up        # create or start, wait for Core, onboard
tests/testbed/ha-testbed.sh status
tests/testbed/ha-testbed.sh down      # stop, keep all data
```

Open <http://localhost:7123> in a browser, including from Windows through WSL2
localhost forwarding. The test user is `hermes-test`; the password and the
long-lived token are in `tests/testbed/.local/credentials.json`.

## Commands

| Command | What it does |
|---|---|
| `up` | Create or start the container, run the bootstrap and `supervisor_run`, wait for Core, onboard |
| `down` | Stop the container, keep its volumes |
| `status` | Container, versions, health, Core API state |
| `reset [--yes]` | Delete the container, its three volumes and the stored credentials |
| `install` | `ha store reload`, then `ha apps install` |
| `rebuild` | `ha store reload`, then `ha apps rebuild --force`; the Supervisor refuses this when `config.yaml` has a new version |
| `update` | `ha store reload`, then `ha apps update`; for a new version in the app's folder |
| `start`, `stop` | `ha apps start`, `ha apps stop` |
| `logs [flags]` | `ha apps logs` |
| `backup [name]` | Partial backup of the app, then `backup-report` |
| `backup-report <slug>` | File sizes in a backup, with and without `image.tar` |
| `restore <slug>` | `ha backups restore --app` |
| `options` | Show the app's options, watchdog switch and state |
| `options '<json>' [--watchdog true\|false]` | Set options through the Supervisor API; invalid values make it exit non-zero |
| `ingress-url` | Create an Ingress session; print the URL and a curl cookie file |
| `model-stub start\|stop` | Run the harness's stub model on the `hassio` network at `172.30.33.250`, for chats without a paid key |
| `ha <args>` | Any `ha` CLI command |
| `api <METHOD> <PATH> [json]` | Core REST call with the long-lived token |
| `supervisor-api <METHOD> <ENDPOINT> [json]` | Supervisor API call through Core's WebSocket API |
| `ws <TYPE> [json]` | Any call to Core's WebSocket API, for example `persistent_notification/get`; the JSON object holds its other fields |
| `onboard` | Run or finish onboarding; `up` does this for you |

App commands act on `local_hermes_webui`. Set `HERMES_TESTBED_APP` to drive
another app, for example a throwaway fixture.

An automated Ingress check looks like this:

```bash
tests/testbed/ha-testbed.sh ingress-url
# URL: http://127.0.0.1:7123/api/hassio_ingress/<token>/
# Cookie file (for curl --cookie): tests/testbed/.local/ingress-cookies.txt
curl --cookie tests/testbed/.local/ingress-cookies.txt "<URL>"
```

## Checking the app through real Ingress

`ingress_check.py` needs the app installed and started, and the stub model running. It checks HTML and assets, frame headers, auth, the setup wizard, streaming, the Home Assistant tools against the real Core, the terminal on and off, denied paths and refused options. With `--reset` it ends by wiping the app's data, so the wizard is due again.

```bash
tests/testbed/ha-testbed.sh model-stub start
python3 tests/testbed/ingress_check.py --reset
tests/testbed/ha-testbed.sh model-stub stop
```

## Secrets

Everything secret stays in `tests/testbed/.local/` (mode 0700, files 0600):
`credentials.json` and `ingress-cookies.txt`. The script writes a `.gitignore`
containing `*` into that folder before it stores anything, because the testbed
may exist before the repository does. The script never prints these values.

These are test credentials for a test instance. Onboarding sets no location, turns
analytics off, and replaces the time zone the Supervisor detects from the machine's
IP address with `Etc/UTC`.

## Container settings

The container matches `.devcontainer.json`, which comes from
`home-assistant/apps-example`, with these differences:

| Setting | Upstream example | Here | Why |
|---|---|---|---|
| Image | `5-apps` tag | `5-apps` pinned by digest | Reproducible test instance |
| Port binding | `7123`, `7357` on all interfaces | `127.0.0.1` only | A privileged test system must not be reachable from the LAN |
| Core's port | `7123→8123` | `7123→80` | See "Core listens on port 80" below |
| `SUPERVISOR_CHANNEL` | `beta` | `stable` | Tests run against what production runs |
| Volumes (script only) | anonymous | named `hermes-testbed-*` | `reset` can delete exactly these |

The script and VS Code do not share data. `.devcontainer.json` keeps upstream's
anonymous volumes, so opening the repo in VS Code creates a second, separate
instance. Both publish the same ports, so run only one at a time.

Each `up` runs `devcontainer_bootstrap` and then `supervisor_run` as the `vscode`
user, because neither the bind mount nor the Supervisor survives a container
restart. `supervisor_run` prunes and restarts containers, but only those of the
inner Docker daemon. The host's Docker socket is not mounted.

### Core listens on port 80

Under the Supervisor, a new install of Core 2026.11 defaults to port 80. Port 8123
only answers with a redirect to port 80 until onboarding completes. With upstream's
`7123→8123` mapping, that redirect leads to `http://localhost/`, where nothing is
published, so this testbed maps `7123→80`.

### The `addons/local` warning

`up` prints this on every start, and it is harmless:

```text
WARNING: Mounting /mnt/supervisor/apps/local/Hermes to /mnt/supervisor/addons/local/Hermes.
```

The `5-apps` image's bootstrap still expects the old `addons/local` folder, while
the Supervisor reads `apps/local`. The repo is mounted at `apps/local/<folder>`,
so the Supervisor sees it there. The bootstrap adds a second, unused mount at the
old path. The Supervisor's legacy-path migration leaves it alone, because
`apps/local` already exists. The `6-apps` image no longer does this.

### Slow store reloads

`ha store reload` pulls every remote store repository before it rereads the local
folder. When GitHub is slow, the CLI gives up after its 30-second client timeout and
reports an error, while the Supervisor finishes the reload in the background.
`install`, `rebuild` and `update` avoid this by calling the same API through Core's WebSocket
API with a 300-second timeout.

### App discovery

The Supervisor treats the repo as one store folder and looks for `config.*` at any
depth, skipping only hidden folders and folders named `rootfs`. A `config.yaml`
elsewhere in the repo, such as a test fixture under `tests/`, would be read as an
app definition. Keep such files out of the repo tree, or name them differently.

### Other Supervisor behaviour worth knowing

- `ha apps` lists the installed apps. There is no `ha apps list`.
- Supervisor API paths are still `/addons/<slug>/...`, even though the CLI says `apps`.
- `ha apps uninstall` keeps `/mnt/supervisor/app_configs/<slug>`.
- Inside a backup, the app's archive holds `config/`, `data/`, `./addon.json` and, for
  a local build, `./image.tar`.
- The sizes `backup-report` lists are uncompressed; the backup file itself is compressed.

## Limits

- **amd64 only.** Production is an aarch64 Pi-class device. This shows nothing
  about Pi performance, and arm64 needs its own emulated smoke test.
- **No AppArmor.** The WSL kernel has AppArmor off, so the Supervisor reports it as
  unsupported and neither its profile nor an app's profile is enforced. A profile
  problem would first appear on a real Home Assistant OS device.
- **No HAOS features:** no Samba share, no OS updates, no OS-level host management.
- **"Unsupported" warnings are expected** here. See `status` for the reasons.
- **Not persistent across `reset`.** `down` keeps data; `reset` deletes all of it.
- **`/tmp` is a tmpfs**, so `/tmp/supervisor_run.log` is lost on every stop.
- **No helper for installing a previous app version yet.** The update test needs
  one, but 0.1.0 has no previous version. Add it together with the second version.

## Claude check

`cron_check.py` checks scheduled tasks through the real app: it finishes the wizard with the stub model if due, then checks that a `no_agent` task fires by itself every minute, that its script gets `SUPERVISOR_TOKEN` (and that the token reaches `http://supervisor/core/api`) but not `HASS_TOKEN`, that a failing task makes one persistent notification in the real Core (read with `ws persistent_notification/get`), that the app opens no listener beyond nginx's port, and that missed runs are caught up once after a stop, a real cold backup and a restore of it. It measures the backup payload and the gateway's RAM, removes its backup, tasks and notifications, and with `--reset` wipes the app's data. It took 12 minutes in the local testbed (measured). `gate.sh` and `pi-gate.sh` run it.

```bash
tests/testbed/ha-testbed.sh model-stub start
python3 tests/testbed/cron_check.py --reset
```

`claude_check.py` checks the opt-in Claude subscription feature through the real Supervisor, logged out, with fake values only.

## Testing on a real device

`HERMES_TESTBED_TARGET=pi` points the same scripts at a separate test SSD with a fresh Home Assistant OS, over its debug SSH on port 22222. Production stays untouched, because it never gets the key.

Everything that identifies the device stays outside the repository, because the repository is mounted into the privileged testbed:

| What | Where |
|---|---|
| Address, Core URL and marker | `~/.config/hermes-pi/target.env` |
| Pinned host key | `~/.config/hermes-pi/known_hosts` |
| The test user's credentials and cookies | `~/.config/hermes-pi/local/` |
| SSH key | `~/.ssh/hermes-pi-test/id_ed25519` |
| Results | `~/.local/state/hermes-gate/pi-latest` |

**Safeguards:**

- `pi-enroll` needs `--test-ssd-is-in`. It refuses a system that already has a Core user, an installed app or a backup, or that isn't `aarch64`.
- It writes a random marker to `/mnt/data/hermes-test-marker` and pins the host key. The key is kept only after every check has passed, so a refused system leaves nothing pinned. From then on, every command checks both in the same connection and runs nothing otherwise.
- `ssh` runs without this machine's `ssh_config` and global known hosts, and the safeguards ignore inherited environment variables.
- `down` and `reset` are refused. `pi-deploy` copies the last commit and warns if the working tree differs.
- `pi-install` rebuilds the app when it's already installed, so a second run tests the newly copied files. It stops at once when the Supervisor refuses or the build fails before the ha CLI's own time limit.

**If the stick was ever in your real system while it started:** Home Assistant OS has then copied the key, and root SSH on port 22222 stays open there after the stick is out. To close it, put a `CONFIG` stick without `authorized_keys` into the real system and restart it once, which removes the key (read in the Home Assistant OS sources, not tried). Then delete `~/.config/hermes-pi`, so that nothing is pinned to the wrong device.

**Steps, once the test SSD and the `CONFIG` stick are in the device and it has started:**

```bash
export HERMES_TESTBED_TARGET=pi
tests/testbed/ha-testbed.sh pi-enroll <address> --test-ssd-is-in
# Take the stick out, and keep it away from production.
tests/testbed/ha-testbed.sh ha host reboot    # Confirms the key survives without the stick.
tests/testbed/pi-gate.sh
```

`pi-gate.sh` onboards the device, reads its facts, copies and builds the app, resets it to a fresh install (signing a hand-made Claude login out first), measures the build, the RAM (idle, during a chat with the stub model, and after it), a backup with its downtime, and a restore, and runs `ingress_check.py`, `claude_check.py` and `cron_check.py`. A real chat with a real model stays a manual step, because it needs the user's own login.

**When the SSD is reused,** delete `~/.config/hermes-pi` after installing it fresh.

**Checked without a device:** `tests/test_pi_target.py` tests the safeguards with stand-ins for `ssh` and `docker`, and `pi-gate.sh` passed against the local testbed through a stand-in for `ssh`. Real SSH to a real HAOS, its paths and its shell are first tried on the device.

