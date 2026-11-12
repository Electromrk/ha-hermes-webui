#!/usr/bin/env python3
"""Checks the installed app through the testbed's real Ingress, with a stub model.

  ingress_check.py [--slug local_hermes_webui] [--reset]

It sets up the stub provider, chats, calls the Home Assistant tools against the real Core, and turns
the terminal on and off, which restarts the app. --reset then wipes the app's data and options, so
the setup wizard is due again. It prints no cookies or tokens, and uses a fake provider key.

The model stub must be running: ha-testbed.sh model-stub start.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
from webui_client import Client, Terminal, answer_text, run_chat, smuggled_request, token_gaps  # noqa: E402

TESTBED = HERE / "ha-testbed.sh"
FAKE_PROVIDER_KEY = "sk-testbed-fake-provider-key-0123456789abcdef"
STUB_URL = "http://172.30.33.250:9999/v1"
FAILED = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{': ' + str(detail) if detail != '' else ''}", flush=True)
    if not ok:
        FAILED.append(name)
    return bool(ok)


def testbed(*arguments, timeout=300):
    return subprocess.run([str(TESTBED), *arguments], text=True, capture_output=True, timeout=timeout, check=False)


def on_host(*arguments, timeout=120):
    """Runs a command where the app's host is: the local testbed, or the device with HERMES_TESTBED_TARGET=pi."""
    return testbed("on-host", *arguments, timeout=timeout)


def open_ingress():
    """Creates an Ingress session. The session itself stays in the cookie file."""
    output = testbed("ingress-url").stdout
    match = re.search(r"^URL: (\S+)", output, re.M)
    if not match:
        raise SystemExit("No Ingress URL. Is the app installed and started?")
    cookies = []
    cookie_file = Path(testbed("local-dir").stdout.strip()) / "ingress-cookies.txt"
    for line in cookie_file.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 7:
            cookies.append(f"{parts[5]}={parts[6]}")
    return match.group(1), Client(match.group(1), cookie="; ".join(cookies))


def wait_healthy(client, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, payload = client.request("GET", "api/auth/status")
            if status == 200 and isinstance(payload, dict):
                return True
        except OSError:
            pass
        time.sleep(1)
    return False


def set_options_and_restart(slug, options):
    result = testbed("options", json.dumps(options))
    check(f"options accepted: {sorted(options)}", result.returncode == 0, result.stderr.strip()[:200])
    testbed("ha", "apps", "restart", slug)
    url, client = open_ingress()
    return url, client, wait_healthy(client)


def section_ingress(url, client):
    status, headers, body = client.raw("GET", "")
    html = body.decode("utf-8", "replace")
    check("ingress: HTML through real Ingress", status == 200 and "text/html" in headers.get("content-type", ""), f"HTTP {status}")
    links = sorted(set(re.findall(r'(?:href|src)="(?!https?:|//|data:|#|mailto:)([^"\'+]+)"', html)))
    broken = [(link, code) for link, code in ((link, client.raw("GET", link)[0]) for link in links) if code != 200]
    check("ingress: assets load", links and not broken, f"{len(links)} links, broken {broken}")
    check("frames: Core's SAMEORIGIN, not the webui's DENY", headers.get("x-frame-options") == "SAMEORIGIN", headers.get("x-frame-options"))
    for header in ("content-security-policy", "content-security-policy-report-only"):
        value = headers.get(header, "")
        check(f"frames: {header} allows the panel", "frame-ancestors 'self'" in value and "frame-ancestors 'none'" not in value)
    status, payload = client.request("GET", "api/auth/status")
    check("auth: trusted header from the real Supervisor", status == 200 and payload.get("auth_type") == "trusted" and payload.get("logged_in") is True,
          f"HTTP {status}")
    status, payload = client.request("GET", "api/onboarding/status")
    system = payload.get("system", {})
    check("wizard: due on a fresh install", payload.get("completed") is False and system.get("setup_state") == "needs_provider", system.get("setup_state"))
    for path in ("api/updates/apply", "API/Gateway/Restart", "api/health/restart", "api/extensions/install", "api/shutdown"):
        code = client.request("POST", path, {})[0]
        check(f"denied: {path}", code == 403, f"HTTP {code}")
    status, payload = client.request("GET", "api/updates/check")
    check("updates: the check is off", isinstance(payload, dict) and payload.get("disabled") is True)


def section_terminal_off(client):
    status, payload = client.request("POST", "api/session/new", {})
    session = payload["session"]["session_id"]
    status, payload = client.request("POST", "api/terminal/start", {"session_id": session})
    check("terminal off: refused with the option's name", status == 404 and "enable_terminal" in str(payload), f"HTTP {status}")
    for path in ("API/TERMINAL/START", "api/terminal/start;x", "api/%74erminal/start", "terminal/"):
        code = client.raw("POST", path, b"{}", {"Content-Type": "application/json"})[0]
        check(f"terminal off: {path}", code == 404, f"HTTP {code}")


def app_container(slug, verb, *arguments):
    return on_host("docker", verb, f"app_{slug}", *arguments)


def webui_lines(slug, needle):
    result = app_container(slug, "logs")
    return sum(needle in line for line in (result.stdout + result.stderr).splitlines())


def section_smuggling(slug, client):
    """With a valid session: a body the webui doesn't read must never run as a next request."""
    settings = 'POST", "path": "/api/settings"'
    settings_before = webui_lines(slug, settings)
    terminal_before = webui_lines(slug, '"path": "/api/terminal/')
    text = {"Content-Type": "text/plain"}
    session = client.request("POST", "api/session/new", {})[1]["session"]["session_id"]
    codes = {}
    for number, (method, path) in enumerate((("OPTIONS", ""), ("POST", "nonexistent"), ("PUT", "nonexistent"),
                                             ("PATCH", "nonexistent"), ("DELETE", "nonexistent"), ("GET", "favicon.ico"))):
        body = smuggled_request("/api/settings", {"bot_name": f"SMUGGLED-{number}"})
        codes[f"{method} /{path}"] = client.raw(method, path, body, text)[0]
    check("smuggling: the OPTIONS carrier still answers", codes["OPTIONS /"] == 200, codes)
    client.raw("OPTIONS", "", smuggled_request("/api/terminal/start", {"session_id": session}), text)
    client.raw("OPTIONS", "", smuggled_request("/api/terminal/input", {"session_id": session, "data": "id > /config/hermes/workspace/smuggled.txt\n"}), text)
    time.sleep(1)
    output = app_container(slug, "exec", "sh", "-c",
                           "jq -r '.bot_name // \"unset\"' /config/hermes/webui/settings.json; "
                           "test -e /config/hermes/workspace/smuggled.txt && echo file-written; pgrep -c -f '/bin/bash -[i]' || true").stdout.split()
    check("smuggling: settings stay untouched", not any("SMUGGLED" in word for word in output), output[:1])
    check("smuggling: enable_terminal off stays off", "file-written" not in output and output[-1:] == ["0"], output[1:])
    added = (webui_lines(slug, settings) - settings_before, webui_lines(slug, '"path": "/api/terminal/') - terminal_before)
    check("smuggling: nothing reached the webui", added == (0, 0), f"{added[0]} settings lines, {added[1]} terminal lines")


def section_chat(client):
    page = client.raw("GET", "")[2]
    token = re.search(rb'csrfToken:"([^"]+)"', page)
    origin = f"{client.base.scheme}://{client.base.netloc}"
    status, _headers, raw = client.raw(
        "POST", "api/onboarding/setup",
        json.dumps({"provider": "custom", "model": "stub-model", "api_key": FAKE_PROVIDER_KEY, "base_url": STUB_URL}).encode(),
        {"Content-Type": "application/json", "Origin": origin, "X-CSRF-Token": token.group(1).decode() if token else ""},
    )
    payload = json.loads(raw)
    check("wizard: saves through real Ingress", status == 200 and payload.get("completed") is True and payload.get("system", {}).get("chat_ready") is True,
          f"HTTP {status}, chat_ready {payload.get('system', {}).get('chat_ready')}")

    _session, timeline = run_chat(client, "hello through real Ingress")
    count, gaps = token_gaps(timeline)
    check("sse: streams through real Ingress", count == 8 and gaps and min(gaps) > 0.3,
          f"{count} tokens, gaps {min(gaps or [0]):.2f}-{max(gaps or [0]):.2f} s at a 0.50 s stub delay")

    for label, tool, arguments, expected in (
        ("ha_list_entities", "ha_list_entities", {"domain": "sun"}, "sun.sun"),
        ("ha_get_state", "ha_get_state", {"entity_id": "sun.sun"}, "next_rising"),
        ("ha_list_services", "ha_list_services", {"domain": "homeassistant"}, "restart"),
    ):
        call = {"calls": [{"name": tool, "arguments": arguments}]}
        _session, timeline = run_chat(client, "CALL_TOOL tool_call " + json.dumps(call))
        text = answer_text(timeline)
        check(f"ha tools: {label} reaches the real Core", expected in text, text.strip()[:90])


def section_terminal_on(slug):
    url, client, healthy = set_options_and_restart(slug, {"enable_terminal": True, "claude_subscription": False, "env_vars": [], "log_level": "info"})
    check("terminal on: restart healthy", healthy)
    terminal = Terminal(client)
    status, payload = terminal.start()
    check("terminal on: starts through real Ingress", status == 200, f"HTTP {status}")
    if status == 200:
        _code, output = terminal.run("echo typed-$((40+2)) uid-$(id -u) home-$HOME")
        check("terminal on: output streams back", "typed-42" in output and "uid-10000" in output and "home-/tmp/hermes-home" in output)
        _code, output = terminal.run("env | grep -cE 'HASS_|SUPERVISOR_|HASSIO_'; hermes update; echo rc=$?")
        check("terminal on: no tokens in the shell", re.search(r"^0$", output, re.M) is not None)
        check("terminal on: hermes update blocked", "Updates are delivered as new app versions in Home Assistant." in output and "rc=1" in output)
        terminal.close()
    url, client, healthy = set_options_and_restart(slug, {"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info"})
    status = client.request("POST", "api/terminal/start", {"session_id": "x"})[0]
    check("terminal off again: refused", healthy and status == 404, f"HTTP {status}")
    return client


def section_invalid_options():
    for label, options in (
        ("an unknown log level", {"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "loud"}),
        ("a bad variable name", {"enable_terminal": False, "claude_subscription": False, "log_level": "info",
                                 "env_vars": [{"name": "lower-case", "value": "x"}]}),
    ):
        result = testbed("options", json.dumps(options))
        check(f"supervisor: refuses {label}", result.returncode != 0)
    base = {"enable_terminal": False, "claude_subscription": False, "log_level": "info"}
    # One name per family, and the plain names next to their prefixes. env_sync.py holds the whole list.
    for name in ("HASS_TOKEN", "HASS_URL", "SUPERVISOR_TOKEN", "HASSIO_TOKEN", "HERMES_HOME", "HERMES_HOME_OVERRIDE",
                 "HERMES_WEBUI_", "HERMES_WEBUI_PASSWORD", "HERMES_MANAGED", "HERMES_MANAGED_DIR", "HERMES_DASHBOARD_TOKEN",
                 "HERMES_CONFIG", "HERMES_CONFIG_PATH", "HERMES_ENV_PATH", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
                 "PATH", "HOME", "XDG_CACHE_HOME", "DISABLE_UPDATES", "HASS_HOME_CHANNEL", "HERMES_GATEWAY_LOCK_DIR",
                 "GATEWAY_RELAY_URL", "API_SERVER_KEY", "WEBHOOK_ENABLED", "TELEGRAM_WEBHOOK_URL", "LINE_HOST"):
        result = testbed("options", json.dumps({**base, "env_vars": [{"name": name, "value": "x"}]}))
        check(f"supervisor: refuses the reserved name {name}", result.returncode != 0)
    for name in ("OPENROUTER_API_KEY", "HERMES_CUSTOM_API_KEY", "MY_HERMES_HOME", "HOMEBREW", "PATHEXT", "HASS_SOMETHING_ELSE",
                 "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"):
        result = testbed("options", json.dumps({**base, "env_vars": [{"name": name, "value": "x"}]}))
        check(f"supervisor: takes the ordinary name {name}", result.returncode == 0, result.stderr.strip()[:200])
    result = testbed("options", json.dumps({**base, "env_vars": []}))
    check("supervisor: options back to the defaults", result.returncode == 0)


def reset_app(slug):
    """Back to a fresh install with default options, so that the wizard is due."""
    testbed("ha", "apps", "stop", slug)
    root = testbed("supervisor-root").stdout.strip()
    wiped = on_host("sh", "-c", f"rm -rf {root}/app_configs/{slug}/hermes {root}/apps/data/{slug}/claude "
                                f"{root}/apps/data/{slug}/env_vars_names.json")
    if wiped.returncode != 0:
        raise SystemExit(f"Couldn't reset the app: {wiped.stderr.strip()[:200]}")
    testbed("options", json.dumps({"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info"}))
    testbed("ha", "apps", "start", slug)
    _url, client = open_ingress()
    healthy = wait_healthy(client)
    status, payload = client.request("GET", "api/onboarding/status")
    check("reset: the wizard is due again", healthy and payload.get("system", {}).get("setup_state") == "needs_provider", payload.get("system", {}).get("setup_state"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", default="local_hermes_webui")
    parser.add_argument("--reset", action="store_true", help="wipe the app's data and options afterwards")
    parser.add_argument("--reset-only", action="store_true", help="only wipe the app's data and options")
    args = parser.parse_args()
    if args.reset_only:
        reset_app(args.slug)
        return 1 if FAILED else 0
    url, client = open_ingress()
    check("start: app answers through real Ingress", wait_healthy(client))
    section_ingress(url, client)
    section_terminal_off(client)
    section_smuggling(args.slug, client)
    section_chat(client)
    section_invalid_options()
    section_terminal_on(args.slug)
    if args.reset:
        reset_app(args.slug)
    print(f"\n{len(FAILED)} failed" if FAILED else "\nAll checks passed.")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
