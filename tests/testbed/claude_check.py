#!/usr/bin/env python3
"""The Claude subscription switch on the testbed's real Supervisor, logged out.

  claude_check.py [--slug local_hermes_webui]

It turns the option on and off through the Supervisor and checks the switch, CLAUDE_CONFIG_DIR, the
logged-out error and that no other provider is called. Its checks need the setup wizard to be due, so
it starts by wiping the app's data and options, and ends the same way. It prints no cookies or tokens,
uses fake keys, and never logs in.

The app must be installed and started, and the model stub running: ha-testbed.sh model-stub start.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
from ingress_check import FAILED, FAKE_PROVIDER_KEY, STUB_URL, check, on_host, open_ingress, reset_app, set_options_and_restart, testbed, wait_healthy  # noqa: E402
from webui_client import Terminal, answer_text, run_chat  # noqa: E402

PROVIDER = "claude-subscription-directsdk-experimental"
PLUGIN_DIR = "/opt/hermes-plugins/claude-subscription-directsdk"
LINK = "/config/hermes/plugins/claude-subscription-directsdk"
FAKE_KEY = "testbed-fake-anthropic-value-90d7"
FAKE_URL = "http://127.0.0.1:9/testbed-fake-anthropic-url"
ON = {"enable_terminal": True, "claude_subscription": True, "env_vars": [], "log_level": "info"}
OFF = {"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info"}


def in_app(slug, script, user="hermes"):
    home = ["--env", "HOME=/tmp/hermes-home"] if user == "hermes" else []
    return on_host("docker", "exec", "--user", user, *home, f"app_{slug}", "sh", "-c", script)


def stub_calls():
    result = on_host("docker", "exec", "hermes-testbed-model-stub", "cat", "/tmp/model_stub.jsonl", timeout=60)
    return len([line for line in result.stdout.splitlines() if line.strip()])


def start_lines(slug):
    """The init lines of the latest start. The Supervisor's log spans earlier starts as well."""
    logs = testbed("ha", "apps", "logs", slug).stdout
    logs = re.sub(r"\x1b\[[0-9;]*m", "", logs)
    lines = [line for line in logs.splitlines() if line.startswith("[hermes-init]")]
    last = max((index for index, line in enumerate(lines) if "Home Assistant access mode" in line), default=0)
    return lines[last:], logs


def browser_post(client, path, body):
    page = client.raw("GET", "")[2]
    token = re.search(rb'csrfToken:"([^"]+)"', page)
    origin = f"{client.base.scheme}://{client.base.netloc}"
    status, _headers, raw = client.raw("POST", path, json.dumps(body).encode(),
                                       {"Content-Type": "application/json", "Origin": origin, "X-CSRF-Token": token.group(1).decode() if token else ""})
    return status, json.loads(raw)


def chat(client, message):
    _session, timeline = run_chat(client, message)
    errors = [data for _moment, event, data in timeline if event in ("apperror", "error")]
    return ("error", errors[0]) if errors else ("answer", answer_text(timeline))


def group_ids(client):
    status, models = client.request("GET", "api/models")
    return status, [group.get("provider_id") for group in models.get("groups", [])]


def section_switch_on(slug):
    _url, client, healthy = set_options_and_restart(slug, ON)
    check("on: restart healthy", healthy)
    lines, logs = start_lines(slug)
    check("on: the switch is logged", "[hermes-init] Claude subscription provider: on" in lines)
    check("on: the login state is logged", "[hermes-init] Claude CLI logged in: no" in lines)
    check("on: no account details in the log", not re.search(r"loggedIn|authMethod|\borgId\b|\bemail\b", logs))
    output = in_app(slug, f"readlink {LINK}; stat -c '%a %U %G' /data/claude; ls -A /data/claude | grep -c credentials").stdout.split("\n")
    check("on: the plugin is a symlink to the pinned copy", output[0] == PLUGIN_DIR, output[0])
    check("on: /data/claude is 0700 and the runtime user's", output[1] == "700 hermes hermes", output[1])
    check("on: no credentials file", output[2] == "0", output[2])
    output = in_app(slug, r"""for dir in /proc/[0-9]*; do
  if tr '\0' ' ' < "$dir/cmdline" 2>/dev/null | grep -q '^/opt/hermes/.venv/bin/python server.py'; then
    tr '\0' '\n' < "$dir/environ" | grep -E '^(CLAUDE_CONFIG_DIR=/data/claude|DISABLE_AUTOUPDATER=1|DISABLE_UPDATES=1)$'
  fi
done""").stdout.split()
    check("on: the webui has CLAUDE_CONFIG_DIR and the update switches", len(output) == 3, output)
    status, groups = group_ids(client)
    check("on: the provider is registered", status == 200 and PROVIDER in groups)
    return client


def section_terminal(client):
    terminal = Terminal(client)
    status, _payload = terminal.start()
    check("terminal: starts through real Ingress", status == 200, f"HTTP {status}")
    if status != 200:
        return
    _code, output = terminal.run("command -v claude; claude auth status; echo rc=$?", wait=3)
    flat = output.replace(" ", "").replace("\n", "")
    check("terminal: the wrapper comes first", "/opt/hermes-app/bin/claude" in output)
    check("terminal: claude uses /data/claude", '"configDirectory":"/data/claude"' in flat)
    check("terminal: logged out, exit 1", '"loggedIn":false' in flat and "rc=1" in output)
    _code, output = terminal.run("env | grep -cE 'ANTHROPIC_|OAUTH|SUPERVISOR_|HASS'; claude auth login; true", wait=4)
    check("terminal: no credentials in the shell", re.search(r"^0$", output, re.M) is not None)
    check("terminal: the login is Anthropic's own flow", "claude.com/cai/oauth/authorize" in output and "Paste code" in output, "URL not shown here")
    client.request("POST", "api/terminal/input", {"session_id": terminal.session_id, "data": "\x03"})
    terminal.close()


def section_logged_out(slug, client):
    status, payload = browser_post(client, "api/onboarding/setup", {"provider": "custom", "model": "stub-model", "api_key": FAKE_PROVIDER_KEY, "base_url": STUB_URL})
    check("logged out: the other provider is set up", status == 200 and payload.get("completed") is True, f"HTTP {status}")
    kind, text = chat(client, "hello to the stub through real Ingress")
    before = stub_calls()
    check("logged out: the other provider answers", kind == "answer" and "chunk-1" in text and before > 0, f"{before} stub calls")
    status, payload = browser_post(client, "api/default-model", {"model": "sonnet", "provider": PROVIDER})
    check("logged out: the provider can be selected in the webui", status == 200 and payload.get("provider") == PROVIDER, payload)
    kind, text = chat(client, "hello to Claude, logged out")
    check("logged out: the chat fails with a clear error", kind == "error" and "claude auth login" in text and "401" in text, text[:120])
    after = stub_calls()
    check("logged out: no other provider is called", after == before, f"stub calls {before} before, {after} after")
    output = in_app(slug, "ls -A /data/claude | grep -c credentials; find /config/hermes -type f | xargs grep -l claudeAiOauth 2>/dev/null | wc -l").stdout.split()
    check("logged out: nothing wrote a login", output == ["0", "0"], output)


def section_restore(slug):
    """The Supervisor's restore leaves /data to root, which hid a real login from the CLI. Tests never log in, so a marker stands in."""
    marker = "/data/claude/restore-marker"
    # A named pipe would make this restore stop, and it would leave /config missing if the stop didn't remove it.
    in_app(slug, f"umask 077; echo before-backup > {marker}; mkfifo /data/claude/restore-pipe /config/hermes/workspace/restore-pipe")
    # A device needs much longer than the testbed for these, because each one carries the image.
    output = testbed("backup", "claude-restore-check", timeout=1800)
    found = re.search(r"Backup slug: (\S+)", output.stdout + output.stderr)
    check("restore: backup taken", found is not None)
    if not found:
        return
    in_app(slug, f"echo after-backup > {marker}")
    testbed("restore", found.group(1), timeout=1800)
    # The old content proves that the restore has really happened, and only the runtime user's read counts.
    state = []
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline and state[:2] != ["before-backup", "0"]:
        time.sleep(3)
        state = in_app(slug, f"cat {marker}; find /data/claude ! -user hermes | wc -l").stdout.split()
    check("restore: the runtime user can read its files again", state[:2] == ["before-backup", "0"], state)
    left = in_app(slug, "find /data/claude /config/hermes -type p | wc -l; test -e /config/hermes/config.yaml && echo config-kept").stdout.split()
    check("restore: no pipe came back, and /config is complete", left == ["0", "config-kept"], left)
    testbed("ha", "backups", "remove", found.group(1))
    _url, client = open_ingress()
    check("restore: healthy", wait_healthy(client))


def section_conflict(slug):
    _url, client, healthy = set_options_and_restart(slug, {**ON, "env_vars": [{"name": "ANTHROPIC_BASE_URL", "value": FAKE_URL}]})
    lines, logs = start_lines(slug)
    warning = next((line for line in lines if "refuses to run" in line), "")
    check("conflict: the start goes on, and the variable is named", healthy and "ANTHROPIC_BASE_URL (.env)" in warning, warning[:110])
    check("conflict: no value in the log", FAKE_URL not in logs)
    before = stub_calls()
    kind, text = chat(client, "hello to Claude, with a conflict")
    after = stub_calls()
    check("conflict: the plugin refuses with a clear message", kind == "error" and "ANTHROPIC_BASE_URL" in text, text[:110])
    check("conflict: no other provider is called", after == before, f"stub calls {before} before, {after} after")

    # An Anthropic key the user holds for another provider no longer stops this one, and it stays out of the CLI.
    _url, client, healthy = set_options_and_restart(slug, {**ON, "env_vars": [{"name": "ANTHROPIC_API_KEY", "value": FAKE_KEY}]})
    lines, logs = start_lines(slug)
    check("api key: no warning, and the app starts", healthy and not any("refuses to run" in line for line in lines))
    check("api key: no value in the log", FAKE_KEY not in logs)
    before = stub_calls()
    kind, text = chat(client, "hello to Claude, with an Anthropic key set")
    after = stub_calls()
    check("api key: the chat asks for the login, so the key wasn't used", kind == "error" and "claude auth login" in text and "401" in text, text[:110])
    check("api key: no other provider is called", after == before, f"stub calls {before} before, {after} after")


def section_reserved_name(slug):
    """The real Supervisor's schema refuses this name. tests/harness/smoke.py covers the app's own check at start."""
    result = testbed("options", json.dumps({**OFF, "env_vars": [{"name": "CLAUDE_CODE_OAUTH_TOKEN", "value": FAKE_KEY}]}))
    check("reserved: the Supervisor refuses the name", result.returncode != 0)
    state = json.loads(testbed("options").stdout or "{}").get("state")
    check("reserved: the app keeps running", state == "started", f"state {state}")
    _lines, logs = start_lines(slug)
    check("reserved: the value isn't logged", FAKE_KEY not in logs)
    result = testbed("options", json.dumps({**OFF, "claude_subscription": "maybe"}))
    check("supervisor: refuses a non-boolean switch", result.returncode != 0)


def section_switch_off(slug):
    _url, client, healthy = set_options_and_restart(slug, {**OFF, "enable_terminal": True})
    check("off: restart healthy", healthy)
    lines, _logs = start_lines(slug)
    check("off: the switch is logged", "[hermes-init] Claude subscription provider: off" in lines)
    check("off: the login isn't checked", not any("logged in" in line for line in lines))
    check("off: warns that config.yaml still selects it", any("config.yaml still selects the Claude provider" in line for line in lines))
    output = in_app(slug, f"test -e {LINK} -o -L {LINK} && echo present || echo gone").stdout.strip()
    check("off: the link is gone", output == "gone", output)
    before = stub_calls()
    kind, text = chat(client, "hello to Claude, switched off")
    after = stub_calls()
    check("off: the chat fails", kind == "error", text[:110])
    check("off: no other provider is called", after == before, f"stub calls {before} before, {after} after")
    browser_post(client, "api/default-model", {"model": "stub-model", "provider": "custom"})
    _url, client, healthy = set_options_and_restart(slug, {**OFF, "enable_terminal": True})
    status, groups = group_ids(client)
    check("off: the webui doesn't offer the provider once another is selected", healthy and PROVIDER not in groups, groups)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", default="local_hermes_webui")
    args = parser.parse_args()
    if stub_calls() is None:
        raise SystemExit("The model stub isn't running.")
    open_ingress()
    reset_app(args.slug)
    try:
        client = section_switch_on(args.slug)
        section_terminal(client)
        section_logged_out(args.slug, client)
        section_restore(args.slug)
        section_conflict(args.slug)
        section_reserved_name(args.slug)
        section_switch_off(args.slug)
    except Exception as error:  # A crash must still report and put the app back.
        check("section finished", False, f"{type(error).__name__}: {error}")
    finally:
        reset_app(args.slug)
    print(f"\n{len(FAILED)} failed" if FAILED else "\nAll checks passed.")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
