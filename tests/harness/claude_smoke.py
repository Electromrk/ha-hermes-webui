#!/usr/bin/env python3
"""The Claude subscription switch in the app image, logged out. Needs Docker and a built image.

  claude_smoke.py [--image IMAGE] [--only SECTION ...] [--keep]

Every check prints PASS or FAIL with what it measured, and any FAIL makes the exit code 1. All keys
and the credentials file here are fake. Nothing logs in, and the section that uses the fake
credentials file runs without a network, so no fake token can leave the machine.
"""
import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import (  # noqa: E402
    APP,
    HOST_PORTS,
    INGRESS_PREFIX,
    LABEL,
    SUPERVISOR_TOKEN,
    Harness,
    docker,
)
from smoke import FAKE_PROVIDER_KEY, Results  # noqa: E402
from webui_client import ANSI, Client, Terminal, answer_text, run_chat  # noqa: E402

PROVIDER = "claude-subscription-directsdk-experimental"
PROVIDER_LABEL = "Claude Subscription DirectSDK (Experimental)"
PLUGIN_DIR = "/opt/hermes-plugins/claude-subscription-directsdk"
LINK = "/config/hermes/plugins/claude-subscription-directsdk"
SWITCH_ON = {"claude_subscription": True, "enable_terminal": True}
FAKE_KEY = "harness-fake-anthropic-value-52c1"
FAKE_URL = "http://harness-fake-anthropic-url.invalid"
OFFLINE = "hermes-harness-offline"
# Fake on purpose, and it stays in a container without a network.
FAKE_CREDENTIALS = ('{"claudeAiOauth":{"accessToken":"harness-fake-access","refreshToken":"harness-fake-refresh",'
                    '"expiresAt":%d,"scopes":["user:inference","user:profile"],"subscriptionType":"pro"}}')

# Drives `hermes model` on a real pty, because the picker needs a terminal. Prints what it found.
PICKER_DRIVER = r"""
import fcntl, os, pty, re, select, struct, termios, time
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[=>()][0-9A-Za-z]?")
pid, fd = pty.fork()
if pid == 0:
    # Short on purpose: the list then scrolls, so each step repaints the row under the cursor.
    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 16, 160, 0, 0))
    os.environ.update(TERM="xterm", COLUMNS="160", LINES="16")
    os.execv("/opt/hermes-app/bin/hermes", ["hermes", "model"])
def read(seconds):
    chunks, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _w, _x = select.select([fd], [], [], 0.1)
        if ready:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data.decode("utf-8", "replace"))
    return ANSI.sub("", "".join(chunks))
screen = read(8)
found = False
for step in range(120):
    os.write(fd, b"\x1b[B")
    if re.search(r"→ \(.\) Claude Subscription", read(0.3)):
        found = True
        print(f"provider row: {step + 1} steps down")
        break
print(f"provider found: {found}")
os.write(fd, b"\r")
screen = read(20)
rows = re.findall(r"\(.\) (claude-[a-z0-9.\[\]-]+)", screen)
print("rows: " + " ".join(rows))
cursor = re.search(r"→ \(.\) (claude-[a-z0-9.\[\]-]+)", screen)
print("cursor: " + (cursor.group(1) if cursor else "?"))
os.write(fd, b"\r")
print("after: " + " ".join(read(6).split())[-160:])
"""


class ClaudeSmoke:
    def __init__(self, harness):
        self.harness = harness
        self.results = Results()
        self.check = self.results.check
        self.user = Client(f"http://127.0.0.1:{HOST_PORTS['user']}{INGRESS_PREFIX}")

    # ---- Helpers ----

    def as_hermes(self, script):
        return self.harness.app_exec("sh", "-c", script, user="hermes")

    def as_root(self, script):
        return self.harness.app_exec("sh", "-c", script)

    def run_python(self, code):
        """Runs code on the agent's Python, as the runtime user, with the agent's modules importable."""
        return self.harness.app_exec("sh", "-c", "cd /opt/hermes && HERMES_HOME=/config/hermes exec /opt/hermes/.venv/bin/python -",
                                     user="hermes", input_text=code)

    def browser_post(self, path, body):
        page = self.user.raw("GET", "")[2]
        match = re.search(rb'csrfToken:"([^"]+)"', page)
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{HOST_PORTS['user']}",
                   "X-CSRF-Token": match.group(1).decode() if match else ""}
        status, _headers, raw = self.user.raw("POST", path, json.dumps(body).encode(), headers)
        return status, json.loads(raw)

    def init_lines(self):
        return [line for line in self.harness.logs().splitlines() if line.startswith("[hermes-init]")]

    def group_ids(self):
        status, models = self.user.request("GET", "api/models")
        return status, [group.get("provider_id") for group in models.get("groups", [])], models

    def stub_count(self):
        return len(self.harness.model_log())

    def chat_error(self, message):
        """One chat turn. Returns the text of the error event, or the answer if there was no error."""
        _session, timeline = run_chat(self.user, message)
        errors = [data for _moment, event, data in timeline if event in ("apperror", "error")]
        return ("error", errors[0]) if errors else ("answer", answer_text(timeline))

    # ---- Sections ----

    def section_switch_on(self):
        started = time.monotonic()
        healthy = self.harness.up(SWITCH_ON)
        self.check("on: app healthy", healthy, f"{time.monotonic() - started:.1f} s from a fresh start")
        if not healthy:
            print(self.harness.logs()[-3000:])
            raise SystemExit("The app did not become healthy; stopping here.")
        lines = self.init_lines()
        self.check("on: the switch is logged", "[hermes-init] Claude subscription provider: on" in lines)
        self.check("on: the login state is logged", "[hermes-init] Claude CLI logged in: no" in lines)
        logs = self.harness.logs()
        self.check("on: no account details or token in the log", not re.search(r"loggedIn|authMethod|\borgId\b|\bemail\b", logs) and SUPERVISOR_TOKEN not in logs)
        self.check("on: no warning about conflicts", not any("refuses to run" in line for line in lines))

        output = self.as_root(f"readlink {LINK}; stat -c '%U %a' {LINK}; stat -c '%a %U %G' /data/claude; ls -A /data/claude | grep -c credentials; "
                              f"find /config -path '*/plugins/claude*' -type f | wc -l").stdout.split("\n")
        self.check("on: the plugin is a symlink to the pinned copy", output[0] == PLUGIN_DIR, output[0])
        self.check("on: the link belongs to the runtime user", output[1].startswith("hermes "), output[1])
        self.check("on: /data/claude is 0700 and the runtime user's", output[2] == "700 hermes hermes", output[2])
        self.check("on: the check created no credentials", output[3] == "0", output[3])
        self.check("on: no plugin code under /config", output[4] == "0", output[4])

        status, groups, models = self.group_ids()
        rows = next((group for group in models.get("groups", []) if group.get("provider_id") == PROVIDER), {}).get("models", [])
        self.check("on: the provider is registered", status == 200 and PROVIDER in groups, f"{len(rows)} models")
        status, payload = self.user.request("GET", "api/onboarding/status")
        self.check("on: the setup wizard is still due, config.yaml absent",
                   payload.get("completed") is False and payload.get("system", {}).get("config_exists") is False,
                   payload.get("system", {}).get("setup_state"))

        output = self.run_python("from hermes_cli.config import load_config\n"
                                 "c = load_config()\n"
                                 "print(c.get('fallback_model') or '-', c.get('fallback_providers') or '-')\n"
                                 "print(c['auth']['adopt_external_logins'], c.get('model'))\n").stdout.split("\n")
        self.check("on: no fallback is configured", output[0] == "- -", output[0])
        self.check("on: external logins stay off, and no model is set", output[1].startswith("False"), output[1])

    def section_terminal(self):
        terminal = Terminal(self.user)
        status, _payload = terminal.start()
        self.check("terminal: starts", status == 200, f"HTTP {status}")
        if status != 200:
            return
        _code, output = terminal.run("command -v claude; claude auth status; echo rc=$?", wait=3)
        self.check("terminal: the wrapper comes first", "/opt/hermes-app/bin/claude" in output)
        self.check("terminal: claude uses /data/claude", '"configDirectory":"/data/claude"' in output.replace(" ", "").replace("\n", ""))
        self.check("terminal: logged out, exit 1", '"loggedIn":false' in output.replace(" ", "").replace("\n", "") and "rc=1" in output)
        _code, output = terminal.run("claude update; echo rc=$?", wait=3)
        self.check("terminal: claude update is off", "disabled" in output.lower(), output.strip().splitlines()[-2:])
        _code, output = terminal.run("env | grep -cE 'ANTHROPIC_|OAUTH|SUPERVISOR_|HASS'; true", wait=1.5)
        self.check("terminal: no credentials in the shell", re.search(r"^0$", output, re.M) is not None)
        _code, output = terminal.run("bash -lc 'command -v claude'", wait=1.5)
        self.check("terminal: a login shell finds the wrapper too", "/opt/hermes-app/bin/claude" in output)

        # `hermes model` is the documented way to select the provider. Logged out, it starts the CLI's own login.
        self.user.request("POST", "api/terminal/input", {"session_id": terminal.session_id, "data": "hermes model\n"})
        time.sleep(6)
        seen = ""
        for _step in range(120):
            self.user.request("POST", "api/terminal/input", {"session_id": terminal.session_id, "data": "\x1b[B"})
            time.sleep(0.2)
            seen += ANSI.sub("", "".join(terminal.buffer))
            terminal.buffer.clear()
            if "Claude Subscription" in seen:
                break
        self.check("hermes model: lists the provider", PROVIDER_LABEL in seen)
        self.user.request("POST", "api/terminal/input", {"session_id": terminal.session_id, "data": "\r"})
        time.sleep(8)
        seen = ANSI.sub("", "".join(terminal.buffer)).replace("\r", "")
        terminal.buffer.clear()
        self.check("hermes model: logged out, it starts the official login", "auth login" in seen and "claude.com/cai/oauth/authorize" in seen, "URL not shown here")
        self.user.request("POST", "api/terminal/input", {"session_id": terminal.session_id, "data": "\x03"})
        time.sleep(2)
        terminal.buffer.clear()
        output = self.as_root("ls -A /data/claude | grep -c credentials; test -e /config/hermes/config.yaml && echo config || echo no-config").stdout.split()
        self.check("hermes model: an aborted login leaves nothing", output == ["0", "no-config"], output)
        terminal.close()

    def section_logged_out_chat(self):
        # A provider that would answer, with a key in .env. If anything fell back, this stub would see it.
        status, payload = self.browser_post("api/onboarding/setup", {"provider": "custom", "model": "stub-model", "api_key": FAKE_PROVIDER_KEY,
                                                                     "base_url": "http://model-stub:9999/v1"})
        self.check("logged out: the other provider is set up", status == 200 and payload.get("completed") is True, f"HTTP {status}")
        kind, text = self.chat_error("hello to the stub")
        before = self.stub_count()
        self.check("logged out: the other provider answers", kind == "answer" and "chunk-1" in text and before > 0, f"{before} stub calls")

        status, payload = self.browser_post("api/default-model", {"model": "sonnet", "provider": PROVIDER})
        self.check("logged out: the provider can be selected in the webui", status == 200 and payload.get("provider") == PROVIDER, payload)
        output = self.as_root("grep -E '^  (provider|default):' /config/hermes/config.yaml").stdout
        self.check("logged out: config.yaml holds what the user selected", f"provider: {PROVIDER}" in output and "default: sonnet" in output, output.split())

        kind, text = self.chat_error("hello to Claude, logged out")
        self.check("logged out: the chat fails with a clear error", kind == "error" and "claude auth login" in text and "401" in text, text[:120])
        after = self.stub_count()
        self.check("logged out: no other provider is called", after == before, f"stub calls {before} before, {after} after")
        status, payload = self.user.request("GET", "api/onboarding/status")
        self.check("logged out: no wizard over a selected provider", payload.get("completed") is True, payload.get("system", {}).get("setup_state"))
        env = self.as_root("grep -c harness-fake-provider /config/hermes/.env").stdout.strip()
        self.check("logged out: the other provider's key stays", env == "1", env)

        output = self.as_hermes("hermes config set model.default claude-haiku-5-5[1m] >/dev/null 2>&1; echo rc=$?; hermes config set model.default sonnet >/dev/null 2>&1; echo rc=$?").stdout.split()
        self.check("logged out: the alias can be set on the command line", output == ["rc=0", "rc=0"], output)

        # The documented exception: a fallback the user adds on purpose does bill that provider.
        self.as_hermes("cat >> /config/hermes/config.yaml <<'EOF'\n"
                       "fallback_model:\n  provider: custom\n  model: stub-model\n"
                       f"  base_url: http://model-stub:9999/v1\n  api_key: {FAKE_PROVIDER_KEY}\nEOF\n")
        self.harness.restart(SWITCH_ON)
        kind, text = self.chat_error("hello, with a fallback the user added")
        count = self.stub_count()
        self.check("fallback: one the user adds on purpose takes over", kind == "answer" and count > after, f"stub calls {after} before, {count} after")
        self.as_hermes("sed -i '/^fallback_model:/,$d' /config/hermes/config.yaml")
        self.harness.restart(SWITCH_ON)

    def section_conflicts(self):
        self.as_hermes("printf 'export CLAUDE_CODE_USE_VERTEX=\"1\"\\nANTHROPIC_AUTH_TOKEN=\\nCLAUDE_CODE_USE_BEDROCK=0\\n' >> /config/hermes/.env")
        healthy = self.harness.restart({**SWITCH_ON, "env_vars": [{"name": "ANTHROPIC_API_KEY", "value": FAKE_KEY}]},
                                       environment={"ANTHROPIC_BASE_URL": FAKE_URL})
        logs = self.harness.logs()
        warning = next((line for line in self.init_lines() if "refuses to run" in line), "")
        self.check("conflicts: a conflict doesn't stop the start", healthy)
        for expected in ("CLAUDE_CODE_USE_VERTEX (.env)", "ANTHROPIC_BASE_URL (environment)"):
            self.check(f"conflicts: names {expected}", expected in warning)
        self.check("conflicts: a dropped key and a switch at 0 aren't named", "ANTHROPIC_API_KEY" not in warning and "BEDROCK" not in warning, warning[:110])
        self.check("conflicts: the warning points to the documentation", "documentation" in warning)
        self.check("conflicts: no value in the log", FAKE_KEY not in logs and FAKE_URL not in logs)
        self.check("conflicts: the login check ignores the inherited key", "[hermes-init] Claude CLI logged in: no" in logs)
        env = self.as_root("cat /config/hermes/.env").stdout
        self.check("conflicts: nothing is deleted", f"ANTHROPIC_API_KEY={FAKE_KEY}" in env and 'CLAUDE_CODE_USE_VERTEX="1"' in env and "ANTHROPIC_AUTH_TOKEN=" in env)

        before = self.stub_count()
        kind, text = self.chat_error("hello to Claude, with conflicts")
        after = self.stub_count()
        self.check("conflicts: the plugin refuses with a clear message", kind == "error" and "ANTHROPIC_BASE_URL" in text, text[:110])
        self.check("conflicts: no other provider is called", after == before, f"stub calls {before} before, {after} after")
        self.check("conflicts: no value in the error", FAKE_KEY not in text)

        self.as_hermes("sed -i '/^export CLAUDE_CODE_USE_VERTEX=/d;/^ANTHROPIC_AUTH_TOKEN=/d;/^CLAUDE_CODE_USE_BEDROCK=/d' /config/hermes/.env")
        # An Anthropic key the user holds for another provider no longer stops this one, and it stays out of the CLI.
        healthy = self.harness.restart({**SWITCH_ON, "env_vars": [{"name": "ANTHROPIC_API_KEY", "value": FAKE_KEY}]})
        self.check("api key: no warning, and the app starts", healthy and not any("refuses to run" in line for line in self.init_lines()))
        before = self.stub_count()
        kind, text = self.chat_error("hello to Claude, with an Anthropic key set")
        after = self.stub_count()
        self.check("api key: the chat asks for the login, so the key wasn't used", kind == "error" and "claude auth login" in text and "401" in text, text[:110])
        self.check("api key: no other provider is called", after == before, f"stub calls {before} before, {after} after")
        self.check("api key: no value in the error", FAKE_KEY not in text)

        self.harness.restart(SWITCH_ON)
        env = self.as_root("cat /config/hermes/.env").stdout
        self.check("conflicts: a key from env_vars leaves with the option", "ANTHROPIC_API_KEY" not in env)
        self.check("conflicts: no warning once they're gone", not any("refuses to run" in line for line in self.init_lines()))

    def section_switch_off(self):
        healthy = self.harness.restart({"enable_terminal": True})
        lines = self.init_lines()
        self.check("off: app healthy", healthy)
        self.check("off: the switch is logged", "[hermes-init] Claude subscription provider: off" in lines)
        self.check("off: the login isn't checked", not any("logged in" in line for line in lines), [line for line in lines if "logged in" in line])
        self.check("off: warns that config.yaml still selects it", any("WARNING: config.yaml still selects the Claude provider" in line for line in lines))
        output = self.as_root(f"test -e {LINK} -o -L {LINK} && echo present || echo gone; grep -c 'provider: {PROVIDER}' /config/hermes/config.yaml; ls -A /opt/hermes-plugins").stdout.split()
        self.check("off: the link is gone", output[0] == "gone", output[0])
        self.check("off: the user's config.yaml is left alone", output[1] == "1", output[1])
        self.check("off: the image keeps the pinned copy", output[2:] == ["claude-subscription-directsdk"], output[2:])

        output = self.run_python("from hermes_cli.plugins import discover_plugins\n"
                                 "discover_plugins()\n"
                                 "from providers import list_providers\n"
                                 f"print(sum(p.name == {PROVIDER!r} for p in list_providers()), len(list_providers()))\n").stdout.split()
        self.check("off: the agent doesn't discover it", output[:1] == ["0"], output)

        before = self.stub_count()
        kind, text = self.chat_error("hello to Claude, switched off")
        after = self.stub_count()
        self.check("off: the chat fails", kind == "error", text[:110])
        self.check("off: no other provider is called", after == before, f"stub calls {before} before, {after} after")

        status, payload = self.browser_post("api/default-model", {"model": "stub-model", "provider": "custom"})
        self.harness.restart({"enable_terminal": True})
        self.check("off: no warning once another provider is selected", not any("still selects" in line for line in self.init_lines()))
        # Only now: the webui's model list always shows the provider that config.yaml selects.
        status, groups, _models = self.group_ids()
        self.check("off: the webui doesn't offer the provider", status == 200 and PROVIDER not in groups, groups)

        healthy = self.harness.restart(SWITCH_ON)
        output = self.as_root(f"readlink {LINK}").stdout.strip()
        self.check("on again: the link is back", healthy and output == PLUGIN_DIR, output)
        status, groups, _models = self.group_ids()
        self.check("on again: the provider is back", PROVIDER in groups)

    def section_own_copy(self):
        self.as_root(f"rm -f {LINK}")
        self.as_hermes(f"mkdir -p {LINK}; printf 'name: {PROVIDER}\\n' > {LINK}/plugin.yaml")
        for label, options in (("on", SWITCH_ON), ("off", {"enable_terminal": True})):
            self.harness.restart(options)
            lines = self.init_lines()
            output = self.as_root(f"test -L {LINK} && echo link || echo folder; ls -A {LINK}").stdout.split()
            self.check(f"own copy, {label}: the folder stays as it was", output == ["folder", "plugin.yaml"], output)
            self.check(f"own copy, {label}: warns", any("is your own copy of the Claude plugin" in line for line in lines))
        self.as_hermes(f"rm -rf {LINK}")
        self.harness.restart(SWITCH_ON)

    def section_payload(self):
        result = self.harness.payload()
        names = {name for name, _size in result["files"]}
        under = [name for name in names if "plugins/claude" in name or "hermes-plugins" in name]
        self.check("payload: with the switch on and after use, under the gate", result["gate_passed"], f"{result['total_bytes']} bytes, uncompressed, without image.tar")
        size = sum(size for name, size in result["files"] if "claude/" in name)
        self.check("payload: no plugin code in it", not [name for name in under if name.endswith(".py")], under)
        self.check("payload: the CLI's folder stays small", size < 200000, f"{size} bytes in the CLI's folder")

    def section_plugin_contract(self):
        """What this app relies on at the pinned plugin version. A re-pin that changes these must show up here."""
        code = rf"""
import sys
sys.path.insert(0, {PLUGIN_DIR!r})
from directsdk_setup import API_CREDENTIAL_ENV, BACKEND_OVERRIDES, BACKEND_SWITCHES, CALLER_IDENTITY
from model_catalog import ALIASES, PICKER_ROUTES, native_model
sys.path.insert(0, "/opt/hermes-app/lib")
import env_sync
print([key for key in BACKEND_OVERRIDES if key not in API_CREDENTIAL_ENV] == list(env_sync.CLAUDE_CONFLICT_KEYS), list(BACKEND_SWITCHES) == list(env_sync.CLAUDE_CONFLICT_SWITCHES))
print(CALLER_IDENTITY)
print(PICKER_ROUTES[0], native_model("sonnet"), ALIASES["sonnet"])
"""
        output = self.run_python(code).stdout.splitlines()
        self.check("contract: the conflict lists match the plugin's", output[:1] == ["True True"], output[:1])
        self.check("contract: the caller name DOCS describe", output[1:2] == ["{'CLAUDE_CODE_ENTRYPOINT': 'hermes-agent', 'CLAUDE_CODE_DISABLE_ARTIFACT': '1'}"], output[1:2])
        self.check("contract: the picker's first row is what the alias sonnet means", output[2:3] == ["claude-sonnet-5-5[1m] claude-sonnet-5-5[1m] claude-sonnet-5-5"], output[2:3])

        code = rf"""
import sys
from hermes_cli.models import merge_profile_catalog
from hermes_cli.auth_model_picker import _picker_order
sys.path.insert(0, {PLUGIN_DIR!r})
from model_catalog import PICKER_ROUTES
class Profile:
    fallback_models = PICKER_ROUTES
live = ["claude-opus-5-5[1m]", "claude-fable-5-1[1m]", "claude-sonnet-5-5[1m]"]
rows = merge_profile_catalog("x", Profile, live)
ordered, _sections, start = _picker_order(rows, "", None)
print(ordered[start])
ordered, _sections, start = _picker_order(rows, "some-other-providers-model", None)
print(ordered[start])
"""
        output = self.run_python(code).stdout.split()
        self.check("contract: the picker starts on Sonnet even if the account lists Opus first",
                   output == ["claude-sonnet-5-5[1m]", "claude-sonnet-5-5[1m]"], output)

    def section_fake_login(self):
        """A fake credentials file, in a container without a network: the yes path, restarts, and the picker."""
        state = self.harness.state / "offline"
        data, config = state / "data", state / "config"
        data.mkdir(parents=True, exist_ok=True)
        config.mkdir(parents=True, exist_ok=True)
        (data / "options.json").write_text(json.dumps({"enable_terminal": True, "claude_subscription": True, "env_vars": [], "log_level": "info"}))

        def start():
            docker("rm", "--force", "--volumes", OFFLINE, check=False)
            docker("run", "--detach", "--name", OFFLINE, "--label", LABEL, "--network", "none",
                   "--env", f"SUPERVISOR_TOKEN={SUPERVISOR_TOKEN}", "--env", f"HASSIO_TOKEN={SUPERVISOR_TOKEN}",
                   "--mount", f"type=bind,source={data},target=/data", "--mount", f"type=bind,source={config},target=/config", self.harness.image)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                logs = docker("logs", OFFLINE, check=False)
                text = logs.stdout + logs.stderr
                if "Terminal: on" in text:
                    return text
                time.sleep(0.5)
            return text

        def run(script, user="hermes", input_text=None):
            stdin = ["--interactive"] if input_text is not None else []
            home = ["--env", "HOME=/tmp/hermes-home"] if user == "hermes" else []
            return docker("exec", "--user", user, *home, *stdin, OFFLINE, "sh", "-c", script, check=False, input_text=input_text, timeout=180)

        def fingerprint():
            return run("sha256sum /data/claude/.credentials.json | cut -c1-16; stat -c '%a %U %Y' /data/claude/.credentials.json").stdout.split()

        try:
            logs = start()
            self.check("fake login: without a file, no", "[hermes-init] Claude CLI logged in: no" in logs)
            expires = (int(time.time()) + 86400 * 365) * 1000
            run("umask 077; cat > /data/claude/.credentials.json", input_text=FAKE_CREDENTIALS % expires)
            before = fingerprint()
            hash_of_file = hashlib.sha256((FAKE_CREDENTIALS % expires).encode()).hexdigest()[:16]
            self.check("fake login: the test file is what was written", before[0] == hash_of_file and before[1:3] == ["600", "hermes"], before[1:3])

            started = time.monotonic()
            logs = start()
            self.check("fake login: with a file the CLI accepts, yes", "[hermes-init] Claude CLI logged in: yes" in logs, f"start to log line {time.monotonic() - started:.1f} s")
            self.check("fake login: no account details in the log", not re.search(r"loggedIn|subscriptionType|harness-fake-access|harness-fake-refresh", logs))
            self.check("fake login: a restart leaves the file untouched", fingerprint() == before, fingerprint()[1:])
            logs = start()
            self.check("fake login: still yes after another restart", "Claude CLI logged in: yes" in logs and fingerprint() == before)
            self.check("fake login: the file is the runtime user's alone", fingerprint()[1:3] == ["600", "hermes"], fingerprint()[1:3])
            output = run("cat /config/hermes/plugins/../.env; ls -A /config/hermes").stdout
            self.check("fake login: nothing of it reaches /config", "harness-fake" not in output and "credentials" not in output)

            result = docker("exec", "--interactive", "--user", "hermes", "--env", "HOME=/tmp/hermes-home", OFFLINE, "/opt/hermes/.venv/bin/python", "-I", "-",
                            input_text=PICKER_DRIVER, check=False, timeout=180)
            lines = result.stdout.splitlines()
            print("  picker:", " | ".join(lines))
            config_text = run("cat /config/hermes/config.yaml 2>/dev/null").stdout
            self.check("picker: the provider is selectable", "provider found: True" in lines, next((line for line in lines if line.startswith("provider row")), ""))
            self.check("picker: Sonnet is the first row", any(line.startswith("rows: claude-sonnet-5-5[1m]") for line in lines))
            self.check("picker: the cursor starts on Sonnet", "cursor: claude-sonnet-5-5[1m]" in lines, next((line for line in lines if line.startswith("cursor")), ""))
            self.check("picker: Enter saves Sonnet and the provider", f"provider: {PROVIDER}" in config_text and "default: claude-sonnet-5-5[1m]" in config_text,
                       [line.strip() for line in config_text.splitlines() if line.strip().startswith(("provider:", "default:"))])

            output = run("claude auth logout >/dev/null 2>&1; echo rc=$?; ls -A /data/claude | grep -c credentials; find /data/claude -mindepth 1 -delete; echo rc=$?; ls -A /data/claude | wc -l").stdout.split()
            self.check("removal: claude auth logout removes the file", output[:2] == ["rc=0", "0"], output[:2])
            self.check("removal: the documented delete empties the folder", output[2:] == ["rc=0", "0"], output[2:])
            logs = start()
            self.check("removal: the next start says no", "[hermes-init] Claude CLI logged in: no" in logs)
        finally:
            docker("rm", "--force", "--volumes", OFFLINE, check=False)
            # Its files are uid 10000, so a container removes them.
            docker("run", "--rm", "--network", "none", "--mount", f"type=bind,source={self.harness.state},target=/state",
                   "--entrypoint", "/bin/rm", self.harness.image, "-rf", "/state/offline", check=False)


SECTIONS = ["switch_on", "terminal", "plugin_contract", "logged_out_chat", "conflicts", "switch_off", "own_copy", "payload", "fake_login"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=None)
    parser.add_argument("--only", nargs="*", choices=SECTIONS, help="run these sections on the running harness")
    parser.add_argument("--keep", action="store_true", help="leave the harness running")
    args = parser.parse_args()
    harness = Harness(args.image)
    smoke = ClaudeSmoke(harness)
    print(f"Image: {harness.image}")
    for name in args.only or SECTIONS:
        print(f"--- {name}", flush=True)
        try:
            getattr(smoke, f"section_{name}")()
        except Exception as error:  # A crash in one section must still report and clean up.
            smoke.check(f"{name}: section finished", False, f"{type(error).__name__}: {error}")
    if not args.keep and not args.only:
        harness.down()
    print(f"\n{smoke.results.passed} passed, {len(smoke.results.failed)} failed")
    for name in smoke.results.failed:
        print(f"  FAILED: {name}")
    return 1 if smoke.results.failed else 0


if __name__ == "__main__":
    sys.exit(main())
