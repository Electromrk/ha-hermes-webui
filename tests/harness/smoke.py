#!/usr/bin/env python3
"""Smoke test of the app image in the harness. Needs Docker and a built image; needs no internet.

  smoke.py [--image IMAGE] [--only SECTION ...] [--keep]

Every check prints PASS or FAIL with what it measured, and any FAIL makes the exit code 1. The keys
and tokens used are fake. --keep leaves the harness running after the last section.
"""
import argparse
import ipaddress
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import (  # noqa: E402
    APP,
    APP_IP,
    HA,
    HOST_PORTS,
    INGRESS_PREFIX,
    LONG_LIVED_TOKEN,
    SUPERVISOR_TOKEN,
    Harness,
    container_state,
    docker,
    remove_container,
)
from webui_client import Client, Terminal, answer_text, run_chat, smuggled_request, token_gaps  # noqa: E402

FAKE_PROVIDER_KEY = "sk-harness-fake-provider-key-0123456789abcdef"
FAKE_ENV_SECRET = "harness-fake-env-value-7f3a"
DIRECT = f"http://{APP_IP}:8099"
MESSAGE_UPDATE = "Updates are delivered as new app versions in Home Assistant."

TERMINAL_PATHS = [
    "api/terminal/output", "api/terminal/start", "api//terminal/start", "api/terminal//start",
    "api/./terminal/start", "api/x/../terminal/start", "api/%74erminal/start", "api/terminal%2Fstart",
    "api%2Fterminal%2Fstart", "API/terminal/start", "api/TERMINAL/start", "Api/Terminal/Start",
    "API/TERMINAL/OUTPUT", "api/terminal", "api/terminal/", "api/terminal/start;x", "api/terminal/start/",
    "terminal/", "terminal", "TERMINAL/", "api/terminal/start?x=1",
]
DENIED_PATHS = [
    "api/updates/apply", "api/updates/force", "api/updates/clear_lock",
    "api/extensions/install", "api/extensions/uninstall", "api/extensions/registry",
    "api/health/restart", "api/gateway/start", "api/gateway/stop", "api/gateway/restart",
    "api/shutdown", "API/Shutdown", "api/shutdown;x",
    "API/UPDATES/APPLY", "Api/Health/Restart", "api/GATEWAY/stop", "api/updates/apply/",
    "api/updates/apply;x", "api/health/restart;x", "api/gateway/start;x", "api/extensions/install;x",
    "api//updates/apply", "api/%75pdates/apply",
]
# Must still reach the webui. A block that is too wide would break these.
NOT_DENIED_PATHS = ["api/updates/check", "api/health/agent", "api/auth/status", "api/settings"]
# Each of these answers without reading the body, or fails before it does, on some path of the webui.
SMUGGLING_CARRIERS = [
    ("OPTIONS", ""), ("GET", "favicon.ico"), ("GET", "api/auth/status"), ("GET", "static/nope.js"), ("GET", "health"),
    ("GET", "api/sessions"), ("HEAD", ""), ("POST", "favicon.ico"), ("POST", "nonexistent"), ("PUT", "nonexistent"),
    ("PATCH", "nonexistent"), ("DELETE", "nonexistent"),
]
REDIRECT_PATHS = ["", "login", "index.html", "session/abc", "settings", "static", "static/", "api", "health", "x/y"]

# Cron test scripts, under $HERMES_HOME/scripts. The probe prints nothing, so its job delivers nothing; it
# records when it ran and which token names its environment holds, never their values.
CRON_SCRIPTS = {
    "harness-probe.sh": "#!/bin/sh\nfor n in SUPERVISOR_TOKEN HASS_TOKEN HASS_URL HASSIO_TOKEN; do eval \"v=\\${$n:-}\"; "
                        "[ -n \"$v\" ] && printf '%s=set ' $n || printf '%s=unset ' $n; done >> /config/hermes/workspace/cron-probe.log; "
                        "date +%s >> /config/hermes/workspace/cron-probe.log\n",
    "harness-fail.sh": "#!/bin/sh\necho harness-cron-failure-output\nexit 3\n",
    "harness-slow.sh": "#!/bin/sh\ntouch /config/hermes/workspace/cron-slow.started\nsleep 25\n",
}
# A listener the app may have on an address other than loopback: nginx on the ingress port.
ALLOWED_LISTENERS = {"0.0.0.0:8099"}
# What a user might put into .env to open an inbound listener. Fake values; the app runs without network.
LISTENER_ATTEMPTS = {
    "WEBHOOK_ENABLED": "true", "WEBHOOK_PORT": "8644", "WEBHOOK_SECRET": "harness0123456789abcdef0123456789",
    "API_SERVER_KEY": "harness0123456789abcdef0123456789", "API_SERVER_HOST": "0.0.0.0", "API_SERVER_PORT": "8642",
    "MSGRAPH_WEBHOOK_ENABLED": "true", "MSGRAPH_WEBHOOK_PORT": "8646", "MSGRAPH_WEBHOOK_ALLOWED_SOURCE_CIDRS": "0.0.0.0/0",
    "MSGRAPH_WEBHOOK_CLIENT_STATE": "harness0123456789",
    "TELEGRAM_WEBHOOK_URL": "https://example.invalid/telegram", "TELEGRAM_WEBHOOK_SECRET": "harness0123456789abcdef0123456789",
    "TELEGRAM_WEBHOOK_HOST": "0.0.0.0", "TELEGRAM_WEBHOOK_PORT": "8443",
    "LINE_CHANNEL_ACCESS_TOKEN": "harness-fake-line-token", "LINE_CHANNEL_SECRET": "harness-fake-line-secret",
    "LINE_HOST": "0.0.0.0", "LINE_PORT": "8650",
    "WHATSAPP_CLOUD_ACCESS_TOKEN": "harness-fake-wa-token", "WHATSAPP_CLOUD_PHONE_NUMBER_ID": "123456",
    "WHATSAPP_CLOUD_VERIFY_TOKEN": "harness-fake", "WHATSAPP_CLOUD_APP_SECRET": "harness-fake-app-secret",
    "WHATSAPP_CLOUD_WEBHOOK_HOST": "0.0.0.0", "WHATSAPP_CLOUD_WEBHOOK_PORT": "8651",
    "WECOM_CALLBACK_CORP_ID": "harnesscorp", "WECOM_CALLBACK_CORP_SECRET": "harness-fake-secret",
    "WECOM_CALLBACK_AGENT_ID": "1000002", "WECOM_CALLBACK_TOKEN": "harnesstoken",
    "WECOM_CALLBACK_ENCODING_AES_KEY": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG", "WECOM_CALLBACK_HOST": "0.0.0.0",
    "WECOM_CALLBACK_PORT": "8652",
    # Loopback by default, but each of these took 0.0.0.0 from .env until the managed .env pinned its host.
    "A2A_HOST": "0.0.0.0", "A2A_PORT": "9900", "A2A_BEARER_TOKEN": "harness0123456789abcdef0123456789",
    "TWILIO_ACCOUNT_SID": "ACharness0123456789", "TWILIO_AUTH_TOKEN": "harness-fake-twilio-token",
    "TWILIO_PHONE_NUMBER": "+15550100", "SMS_WEBHOOK_URL": "https://example.invalid/sms",
    "SMS_WEBHOOK_HOST": "0.0.0.0", "SMS_WEBHOOK_PORT": "8080",
    "BLUEBUBBLES_SERVER_URL": "http://127.0.0.1:1234", "BLUEBUBBLES_PASSWORD": "harness-fake-bluebubbles",
    "BLUEBUBBLES_WEBHOOK_HOST": "0.0.0.0", "BLUEBUBBLES_WEBHOOK_PORT": "8653",
}
# Listeners from LISTENER_ATTEMPTS that start on loopback, which shows the attempt reached them.
EXPECTED_LOOPBACK = {"127.0.0.1:9900", "127.0.0.1:8080"}
FAKE_TELEGRAM = {"TELEGRAM_BOT_TOKEN": "123456789:AAharnessFakeTokenNotRealNotRealNotRe", "TELEGRAM_ALLOWED_USERS": "11111"}
OPEN_ACCESS = {"TELEGRAM_ALLOW_ALL_USERS": "true", "DISCORD_ALLOWED_USERS": "123,*", "HERMES_YOLO_MODE": "1",
               "GATEWAY_ALLOW_ALL_USERS": "yes"}


class Results:
    def __init__(self):
        self.failed = []
        self.passed = 0

    def check(self, name, ok, detail=""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{': ' + str(detail) if detail != '' else ''}", flush=True)
        if ok:
            self.passed += 1
        else:
            self.failed.append(name)
        return bool(ok)


class Smoke:
    def __init__(self, harness):
        self.harness = harness
        self.results = Results()
        self.check = self.results.check
        self.user = Client(f"http://127.0.0.1:{HOST_PORTS['user']}{INGRESS_PREFIX}")
        self.no_name = Client(f"http://127.0.0.1:{HOST_PORTS['no_name']}{INGRESS_PREFIX}")
        self.no_user = Client(f"http://127.0.0.1:{HOST_PORTS['no_user']}{INGRESS_PREFIX}")

    # ---- Helpers ----

    def as_hermes(self, script):
        return self.harness.app_exec("sh", "-c", script, user="hermes")

    def as_root(self, script):
        return self.harness.app_exec("sh", "-c", script)

    def direct_status(self, method, path, runner=None):
        """One raw request from the Supervisor's address, with a user header and an unmodified path."""
        runner = runner or self.harness.ingress_curl
        arguments = ["-s", "--path-as-is", "-m", "8", "-o", "/dev/null", "-w", "%{http_code} %{content_type}",
                     "-H", "X-Remote-User-Id: harness", "-X", method]
        if method == "POST":
            arguments += ["-H", "Content-Type: application/json", "-d", '{"session_id":"nonexistent0000"}']
        return runner(*arguments, f"{DIRECT}/{path}").stdout.strip()

    def headers_of(self, url, runner):
        output = runner("-s", "-m", "8", "-D", "-", "-o", "/dev/null", "-H", "X-Remote-User-Id: harness", url).stdout
        headers = {}
        for line in output.splitlines()[1:]:
            name, _separator, value = line.partition(":")
            headers.setdefault(name.strip().lower(), value.strip())
        return headers

    def memory_mb(self):
        output = docker("stats", "--no-stream", "--format", "{{.MemUsage}}", APP, check=False).stdout
        return output.split("/")[0].strip()

    def no_secret_in_logs(self, name, *secrets):
        logs = self.harness.logs()
        found = [secret[:12] + "..." for secret in secrets if secret in logs]
        self.check(name, not found, f"found {found}" if found else "")

    def env_file(self):
        return self.as_root("cat /config/hermes/.env").stdout

    def browser_post(self, path, body, origin=None):
        """A POST as a browser sends it: session cookie, CSRF token from the page, and an Origin."""
        page = self.user.raw("GET", "")[2]
        match = re.search(rb'csrfToken:"([^"]+)"', page)
        headers = {"Content-Type": "application/json",
                   "Origin": origin or f"http://127.0.0.1:{HOST_PORTS['user']}",
                   "X-CSRF-Token": match.group(1).decode() if match else ""}
        status, _headers, raw = self.user.raw("POST", path, json.dumps(body).encode(), headers)
        return status, json.loads(raw)

    def setup_provider(self):
        return self.browser_post("api/onboarding/setup", {"provider": "custom", "model": "stub-model", "api_key": FAKE_PROVIDER_KEY,
                                                          "base_url": "http://model-stub:9999/v1"})

    # ---- Sections ----

    def section_startup(self):
        started = time.monotonic()
        healthy = self.harness.up()
        self.check("start: app healthy through nginx", healthy, f"{time.monotonic() - started:.1f} s from a fresh start")
        if not healthy:
            print(self.harness.logs()[-3000:])
            raise SystemExit("The app did not become healthy; stopping here.")
        self.check("start: memory, idle", True, self.memory_mb())
        logs = self.harness.logs()
        self.check("start: HA access mode logged", "Home Assistant access mode: supervisor (http://supervisor/core)" in logs)
        self.check("start: terminal off logged", "Terminal: off" in logs)
        self.no_secret_in_logs("start: no token in the log", SUPERVISOR_TOKEN)

        status, payload = self.user.request("GET", "api/onboarding/status")
        system = payload.get("system", {}) if isinstance(payload, dict) else {}
        self.check("start: setup wizard is due, config.yaml absent",
                   status == 200 and payload.get("completed") is False and system.get("config_exists") is False,
                   f"HTTP {status}, setup_state {system.get('setup_state')}")
        self.check("start: agent imports fine", system.get("imports_ok") is True)
        self.check("gateway: waits for the setup wizard and says so",
                   not self.gateway_pids() and "Cron jobs and messengers start once the setup wizard has saved a provider." in logs,
                   f"{len(self.gateway_pids())} gateway processes")

        script = r"""
cd /config/hermes
stat -c '.env %a %U' .env
stat -c 'home %a %U' .
test -d plugins/homeassistant && echo "marker $(ls -A plugins/homeassistant | wc -l)"
grep -c '^[[:space:]]*\(export[[:space:]]*\)\?HASS_' .env || true
test -e config.yaml && echo config-present || echo config-absent
stat -c 'container_environment %a' /run/s6/container_environment
stat -c 'ha-access %a %U' /run/hermes-app/ha-access.env
stat -c 'run-dir %a %U' /run/hermes-app
stat -c 'claude %a %U' /data/claude
jq -c '{check_for_updates, ignore_agent_updates}' webui/settings.json
for name in installs tools environments cache/uv; do test -e "$name" && echo "present $name"; done
command -v ttyd || echo no-ttyd
true
"""
        output = self.as_root(script).stdout
        for expected in (".env 600 hermes", "home 700 hermes", "marker 0", "config-absent",
                         "container_environment 700", "ha-access 600 root", "run-dir 700 root", "claude 700 hermes",
                         '{"check_for_updates":false,"ignore_agent_updates":true}', "no-ttyd"):
            self.check(f"files: {expected}", expected in output)
        lines = output.splitlines()
        self.check("files: no HASS_ key in .env", "0" in lines)
        self.check("files: no package-manager folders", not [line for line in lines if line.startswith("present ")],
                   [line for line in lines if line.startswith("present ")] or "")

        denied = self.as_hermes(
            "for p in /config/x /data/x /opt/hermes/x /opt/hermes-webui/x /etc/hermes/x /opt/hermes-app/x; do "
            "( : > $p ) 2>/dev/null && echo writable:$p; done; "
            "cat /data/options.json /run/hermes-app/ha-access.env /run/s6/container_environment/SUPERVISOR_TOKEN "
            "2>/dev/null | wc -c; ( : > /config/hermes/.probe ) && rm /config/hermes/.probe && echo home-ok"
        ).stdout
        self.check("runtime user: writes only under HERMES_HOME", "writable:" not in denied and "home-ok" in denied, denied.split())
        self.check("runtime user: can't read options or token files", denied.splitlines()[0].strip() == "0")

        # Reading another uid's /proc/<pid>/environ needs CAP_SYS_PTRACE, which even root lacks here,
        # so each process is read as its own user. Only names are printed.
        script = r"""
for dir in /proc/[0-9]*; do
  cmd=$(tr '\0' ' ' < "$dir/cmdline" 2>/dev/null) || continue
  case "$cmd" in
    *"/python serve"[r]".py "|nginx:*|*"/usr/sbin/ngin"[x]" "*)
      names=$(tr '\0' '\n' < "$dir/environ" 2>/dev/null | sed 's/=.*//' | grep -E 'TOKEN|HASS' | tr '\n' ',')
      echo "$(stat -c %U "$dir") [$cmd] $names"
      ;;
  esac
done
"""
        webui = [line for line in self.as_hermes(script).stdout.splitlines() if "server.py" in line]
        self.check("processes: webui runs as hermes", webui and webui[0].startswith("hermes "), webui[:1])
        self.check("processes: webui has HASS_* and SUPERVISOR_TOKEN, not HASSIO_TOKEN",
                   webui and "HASS_TOKEN" in webui[0] and "HASS_URL" in webui[0]
                   and "SUPERVISOR_TOKEN" in webui[0] and "HASSIO_TOKEN" not in webui[0])
        output = self.as_hermes(r"""for dir in /proc/[0-9]*; do
  if tr '\0' ' ' < "$dir/cmdline" 2>/dev/null | grep -q '/python serve[r]\.py'; then
    tr '\0' '\n' < "$dir/environ" | grep -E '^(CLAUDE_CONFIG_DIR=/data/claude|DISABLE_AUTOUPDATER=1|DISABLE_UPDATES=1|HERMES_DISABLE_LAZY_INSTALLS=1|HERMES_WRITE_SAFE_ROOT=/config/hermes|HOME=/tmp/hermes-home|XDG_CACHE_HOME=/tmp/hermes-cache|UV_CACHE_DIR=/tmp/hermes-cache/uv|PIP_CACHE_DIR=/tmp/hermes-cache/pip|npm_config_cache=/tmp/hermes-cache/npm)$'
  fi
done""").stdout.split()
        self.check("processes: the webui has the image's environment", len(output) == 10, f"{len(output)} of 10")
        nginx = [line for line in self.as_root(script).stdout.splitlines() if "nginx" in line]
        self.check("processes: nginx master root, worker www-data, no tokens",
                   any(line.startswith("root ") for line in nginx) and any(line.startswith("www-data ") for line in nginx)
                   and not any("TOKEN" in line for line in nginx), nginx)
        output = self.as_hermes("for dir in /proc/[0-9]*; do tr '\\0' ' ' < $dir/cmdline 2>/dev/null | grep -qE '^nginx:|/usr/sbin/ngin[x] ' "
                                "&& { cat $dir/environ >/dev/null 2>&1 && echo readable || echo denied; }; done").stdout
        self.check("processes: the runtime user can't read nginx's environment", "denied" in output and "readable" not in output, output.split())

    def section_ingress(self):
        status, headers, body = self.user.raw("GET", "")
        html = body.decode("utf-8", "replace")
        self.check("ingress: HTML under the prefix", status == 200 and "text/html" in headers.get("content-type", ""),
                   f"HTTP {status}, {len(body)} bytes")
        links = sorted(set(re.findall(r'(?:href|src)="(?!https?:|//|data:|#|mailto:)([^"\'+]+)"', html)))
        rooted = [link for link in links if link.startswith("/")]
        self.check("ingress: no root-relative links in the HTML", not rooted, rooted[:5] or "")
        broken = []
        for link in links:
            code = self.user.raw("GET", link.split("#")[0])[0]
            if code != 200:
                broken.append((link, code))
        self.check("ingress: assets load under the prefix", links and not broken, f"{len(links)} links, broken {broken}")
        for asset in ("sw.js", "manifest.json", "manifest.webmanifest"):
            code = self.user.raw("GET", asset)[0]
            if code == 200:
                self.check(f"ingress: {asset} loads", True)

        outside = []
        for client_name, client in (("user", self.user), ("no_user", self.no_user)):
            for path in REDIRECT_PATHS:
                code, response_headers, _data = client.raw("GET", path)
                location = response_headers.get("location", "")
                if 300 <= code < 400 and not (location.startswith(INGRESS_PREFIX) or not re.match(r"^(/|[a-z]+://)", location)):
                    outside.append((client_name, path, code, location))
        self.check("ingress: redirects stay inside the prefix", not outside, outside or f"{len(REDIRECT_PATHS) * 2} requests")

        upstream = self.headers_of("http://127.0.0.1:8787/", self.app_curl)
        at_nginx = self.headers_of(f"{DIRECT}/", self.harness.ingress_curl)
        through = {key.lower(): value for key, value in self.user.raw("GET", "")[1].items()}
        self.check("frames: the webui still sends DENY", upstream.get("x-frame-options") == "DENY")
        self.check("frames: XFO hidden at nginx", "x-frame-options" not in at_nginx)
        self.check("frames: simulated Core adds SAMEORIGIN", through.get("x-frame-options") == "SAMEORIGIN")
        for header in ("content-security-policy", "content-security-policy-report-only"):
            source, result = upstream.get(header, ""), at_nginx.get(header, "")
            self.check(f"frames: {header} rewritten, rest byte-identical",
                       "frame-ancestors 'none'" in source and result == source.replace("frame-ancestors 'none'", "frame-ancestors 'self'"),
                       f"{len(result)} characters")

        for name, client in (("with a username", self.user), ("without a username", self.no_name)):
            status, payload = client.request("GET", "api/auth/status")
            self.check(f"auth: user {name} is logged in", status == 200 and payload.get("logged_in") is True
                       and payload.get("auth_type") == "trusted", f"HTTP {status}")
        code, response_headers, _data = self.no_user.raw("GET", "")
        self.check("auth: no header goes to the login page", code == 302 and "login" in response_headers.get("location", ""),
                   f"HTTP {code} {response_headers.get('location')}")
        self.check("auth: no header gets 401 from the API", self.no_user.request("GET", "api/sessions")[0] == 401)
        code = self.no_user.raw("GET", "api/sessions", headers={"X-Remote-User-Id": "forged"})[0]
        self.check("auth: a client's forged header is dropped by the proxy", code == 401, f"HTTP {code}")

    def app_curl(self, *arguments):
        return self.harness.app_exec("curl", *arguments)

    def section_access(self):
        for path in ("", "health"):
            code = self.direct_status("GET", path, self.harness.other_source_curl).split()[0]
            self.check(f"access: another address gets 403 for /{path}", code == "403", code)
        code = self.harness.other_source_curl("-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}",
                                              "-H", "X-Remote-User-Id: forged", f"{DIRECT}/api/sessions").stdout.strip()
        self.check("access: a forged header from another address gets 403", code == "403", code)
        code = self.harness.other_source_curl("-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}",
                                              f"http://{APP_IP}:8787/health").stdout.strip()
        self.check("access: the webui's own port is closed from outside", code == "000", code)
        output = self.harness.other_source_curl("-s", "-m", "5", "-X", "POST", "-H", "X-Remote-User-Id: forged",
                                                f"{DIRECT}/api/updates/apply").stdout
        self.check("access: another address gets only nginx's fixed answer on a blocked path", MESSAGE_UPDATE in output, output.strip())
        code = self.app_curl("-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", "http://127.0.0.1:8099/").stdout.strip()
        self.check("access: 127.0.0.1 gets 403 for /", code == "403", code)
        code = self.app_curl("-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", "http://127.0.0.1:8099/health").stdout.strip()
        self.check("access: 127.0.0.1 may read /health", code == "200", code)
        code = self.direct_status("GET", "health").split()[0]
        self.check("access: the Supervisor's address may read /health", code == "200", code)

    def section_denied(self):
        wrong = []
        for path in DENIED_PATHS:
            for method in ("GET", "POST"):
                code = self.direct_status(method, path).split()[0]
                if code != "403":
                    wrong.append((method, path, code))
        self.check("denied: update, extension, restart and gateway paths", not wrong, wrong or f"{len(DENIED_PATHS)} paths, GET and POST")
        code, payload = self.user.request("POST", "api/updates/apply", {})
        self.check("denied: JSON body with the update message", code == 403 and isinstance(payload, dict) and payload.get("error") == MESSAGE_UPDATE, f"HTTP {code}")
        wrong = [(path, self.direct_status("GET", path).split()[0]) for path in NOT_DENIED_PATHS]
        wrong = [item for item in wrong if item[1] not in ("200",)]
        self.check("denied: neighbouring paths still work", not wrong, wrong or NOT_DENIED_PATHS)
        status, payload = self.user.request("GET", "api/updates/check")
        self.check("updates: the check is off", status == 200 and isinstance(payload, dict) and payload.get("disabled") is True, payload)

        script = ("for form in 'hermes update' 'hermes --quiet update' '/opt/hermes/libexec/hermes update'; do "
                  "out=$($form 2>&1); echo \"$form|$?|$out\"; done; out=$(bash -lc 'hermes update' 2>&1); echo \"login|$?|$out\"")
        lines = self.as_hermes(script).stdout.splitlines()
        wrong = [line for line in lines if f"|1|{MESSAGE_UPDATE}" not in line]
        self.check("updates: hermes update is blocked in every shell", len(lines) == 4 and not wrong, wrong or "4 forms, exit 1")
        output = self.as_hermes("hermes --version 2>&1 | head -1; bash -lc 'command -v hermes'; env | grep -E '^(HOME|HERMES_HOME|HERMES_WRITE_SAFE_ROOT)='").stdout
        self.check("updates: other hermes commands still work", "/opt/hermes-app/bin/hermes" in output and "ermes" in output.splitlines()[0],
                   output.splitlines()[0] if output else "")
        self.check("env: HOME and the write root moved", "HOME=/tmp/hermes-home" in output and "HERMES_WRITE_SAFE_ROOT=/config/hermes" in output)
        output = self.as_hermes("hermes config set security.allow_lazy_installs true 2>&1; echo rc=$?").stdout
        self.check("managed: a user can't turn lazy installs on", "administrator" in output, output.strip().splitlines()[-1] if output else "")

    def section_terminal_off(self):
        wrong = []
        for path in TERMINAL_PATHS:
            for method in ("GET", "POST"):
                result = self.direct_status(method, path).split()
                if result[0] != "404":
                    wrong.append((method, path, result[0]))
        self.check("terminal off: all path variants refused", not wrong, wrong or f"{len(TERMINAL_PATHS)} paths, GET and POST")
        status, payload = self.user.request("POST", "api/session/new", {})
        session = payload["session"]["session_id"]
        status, payload = self.user.request("POST", "api/terminal/start", {"session_id": session})
        self.check("terminal off: JSON body names the option", status == 404 and "enable_terminal" in str(payload), f"HTTP {status}")

    def webui_lines(self, needle):
        return sum(needle in line for line in self.harness.logs().splitlines())

    def carry(self, method, path, body, *headers):
        """Sends one request straight to nginx from the Supervisor's address, as the Supervisor would."""
        return self.harness.ingress_curl("-s", "--path-as-is", "-m", "3", "-o", "/dev/null", "-w", "%{http_code}", "-X", method,
                                         "-H", "Content-Type: text/plain", *headers, "--data-binary", body.decode(), f"{DIRECT}/{path}").stdout.strip()

    def section_smuggling(self):
        """A body the webui doesn't read must never be parsed as a next request. It would pass every block."""
        settings = 'POST", "path": "/api/settings"'
        before = self.webui_lines(settings)
        codes = {}
        for number, (method, path) in enumerate(SMUGGLING_CARRIERS):
            body = smuggled_request("/api/settings", {"bot_name": f"SMUGGLED-{number}"})
            codes[f"{method} /{path}"] = self.carry(method, path, body)
        self.check("smuggling: the carriers themselves still answer", codes["OPTIONS /"] == "200", codes)
        for header in ("Connection: keep-alive", "CONNECTION: Keep-Alive", "Keep-Alive: timeout=60"):
            self.carry("OPTIONS", "", smuggled_request("/api/settings", {"bot_name": "SMUGGLED-keepalive"}), "-H", header)
        self.no_user.raw("OPTIONS", "", smuggled_request("/api/settings", {"bot_name": "SMUGGLED-no-user"}), {"Content-Type": "text/plain"})

        name = self.as_root("jq -r '.bot_name // \"unset\"' /config/hermes/webui/settings.json").stdout.strip()
        self.check("smuggling: settings stay untouched", "SMUGGLED" not in name, name)
        added = self.webui_lines(settings) - before
        self.check("smuggling: no smuggled request reached the webui", added == 0, f"{added} POST /api/settings lines")

        session = self.user.request("POST", "api/session/new", {})[1]["session"]["session_id"]
        for method, path in (("OPTIONS", ""), ("POST", "nonexistent")):
            self.carry(method, path, smuggled_request("/api/terminal/start", {"session_id": session}))
            self.carry(method, path, smuggled_request("/api/terminal/input", {"session_id": session, "data": "id > /config/hermes/workspace/smuggled.txt\n"}))
        self.no_user.raw("OPTIONS", "", smuggled_request("/api/terminal/start", {"session_id": session}), {"Content-Type": "text/plain"})
        time.sleep(1)
        output = self.as_root("test -e /config/hermes/workspace/smuggled.txt && echo file-written; pgrep -c -f '/bin/bash -[i]' || true").stdout.split()
        self.check("smuggling: enable_terminal off stays off", "file-written" not in output and output[-1:] == ["0"], output)
        self.check("smuggling: no terminal request reached the webui", self.webui_lines('"path": "/api/terminal/') == 0)
        self.as_root("rm -f /config/hermes/workspace/smuggled.txt")

    def section_chat(self):
        status, payload = self.browser_post("api/session/new", {}, origin="http://elsewhere.example")
        self.check("csrf: another Origin is refused", status == 403, f"HTTP {status}")
        status, payload = self.setup_provider()
        system = payload.get("system", {}) if isinstance(payload, dict) else {}
        self.check("wizard: the setup saves with a browser's Origin", status == 200 and payload.get("completed") is True
                   and system.get("chat_ready") is True, f"HTTP {status}, chat_ready {system.get('chat_ready')}")
        saved = time.monotonic()
        output = self.as_root("cd /config/hermes; stat -c '%a %U' .env config.yaml; sed -nE 's/^([A-Za-z_]+)=.*/\\1/p' .env").stdout
        self.check("wizard: the key lands in .env, 0600", "600 hermes" in output.splitlines()[0], output.split())
        started = self.wait_for(self.gateway_pids, 60)
        self.check("gateway: starts once config.yaml exists", started, f"{time.monotonic() - saved:.1f} s after the wizard saved")
        model = self.as_root("grep -A3 '^model:' /config/hermes/config.yaml").stdout
        self.check("gateway: the wizard's config.yaml is kept", "provider: custom" in model and "stub-model" in model, model.split()[:6])

        _session, timeline = run_chat(self.user, "hello through the simulator")
        count, gaps = token_gaps(timeline)
        self.check("sse: tokens arrive one by one, not in a burst",
                   count == 8 and gaps and min(gaps) > 0.3, f"{count} tokens, gaps {min(gaps or [0]):.2f}-{max(gaps or [0]):.2f} s at a 0.50 s stub delay")
        self.check("sse: the answer is complete", answer_text(timeline).split() == [f"chunk-{i}" for i in range(1, 9)])
        self.check("chat: memory, after a chat", True, self.memory_mb())

        call = {"calls": [{"name": "ha_list_entities", "arguments": {"domain": "sun"}}]}
        _session, timeline = run_chat(self.user, "CALL_TOOL tool_call " + json.dumps(call))
        text = answer_text(timeline)
        calls = [entry for entry in self.harness.stub_calls() if entry["path"] == "/api/states"]
        self.check("ha tools: a tool call reaches the Core API", "sun.sun" in text and calls and calls[-1]["authorized"]
                   and calls[-1]["route"] == "supervisor", f"answer {text[:70]!r}, calls {calls[-1:]}")
        calls = [entry for entry in self.harness.stub_calls() if entry["path"] == "/api/"]
        output = self.as_root("cd /config/hermes; grep -c '^HASS_' .env; ls -A plugins/homeassistant | wc -l").stdout.split()
        self.check("ha tools: no HASS_ key in .env after use", output[:1] == ["0"], output)
        self.no_secret_in_logs("chat: no key or token in the log", FAKE_PROVIDER_KEY, SUPERVISOR_TOKEN)

        _session, timeline = run_chat(self.user, "FAIL_HTTP 400 for the dump check")
        errors = [event for _moment, event, _data in timeline if event == "apperror"]
        dump = self.as_root(r"""cd /config/hermes
ls sessions/request_dump_*.json 2>/dev/null | wc -l
grep -l sk-harness-fake-provider-key-0123456789abcdef sessions/request_dump_*.json 2>/dev/null | wc -l
grep -rlF harness-fake-supervisor-token . 2>/dev/null | wc -l
""").stdout.split()
        self.check("dumps: a failed call writes a debug dump", errors and dump[0] != "0", f"{dump[0]} dump(s)")
        self.check("dumps: the provider key isn't in it", dump[1] == "0")
        self.check("dumps: the Supervisor token is nowhere in HERMES_HOME", dump[2] == "0")

    def section_uploads(self):
        status, payload = self.user.request("POST", "api/session/new", {})
        session = payload["session"]["session_id"]
        chunked = docker(
            "exec", "--interactive", "hermes-harness-ingress", "curl", "-s", "-m", "20", "-o", "/dev/null", "-w", "%{http_code}",
            "-H", "Host: 127.0.0.1", "-H", "Transfer-Encoding: chunked", "-H", "Content-Type: application/json",
            "--data-binary", "@-", f"http://127.0.0.1:8123{INGRESS_PREFIX}api/session/new",
            input_text="{}", check=False).stdout
        self.check("bodies: a chunked POST reaches the webui whole", chunked.strip() == "200", f"HTTP {chunked}")
        script = (
            f"head -c {{size}}M /dev/zero > /tmp/upload.bin; curl -s -m 60 -o /dev/null -w '%{{{{http_code}}}}' "
            f"-H 'Host: 127.0.0.1' -F session_id={session} -F file=@/tmp/upload.bin "
            f"http://127.0.0.1:8123{INGRESS_PREFIX}api/upload; rm -f /tmp/upload.bin"
        )
        for size, ok_codes in ((15, ("200",)), (30, ("413",))):
            code = docker("exec", "hermes-harness-ingress", "sh", "-c", script.format(size=size), check=False).stdout.strip()
            self.check(f"bodies: a {size} MB upload", code in ok_codes, f"HTTP {code}")
        output = self.as_root("find /config/hermes/webui/attachments -name upload.bin | wc -l; rm -rf /config/hermes/webui/attachments/*").stdout.strip()
        self.check("bodies: the upload lands in the webui's attachments", output == "1", output)

    def section_terminal_on(self):
        healthy = self.harness.restart({"enable_terminal": True})
        self.check("terminal on: restart healthy", healthy)
        self.check("terminal on: logged", "Terminal: on" in self.harness.logs())
        terminal = Terminal(self.user)
        status, payload = terminal.start()
        self.check("terminal on: starts through the simulator", status == 200, f"HTTP {status} {payload if status != 200 else ''}")
        if status != 200:
            return
        _code, output = terminal.run("echo typed-$((40+2)) uid-$(id -u) home-$HOME")
        self.check("terminal on: output streams back", "typed-42" in output and "uid-10000" in output and "home-/tmp/hermes-home" in output,
                   re.findall(r"typed-42 uid-\d+ home-\S+", output)[:1])
        _code, output = terminal.run("env | grep -cE 'HASS_|SUPERVISOR_|HASSIO_'; hermes update; echo rc=$?")
        self.check("terminal on: no tokens in the shell", re.search(r"^0$", output, re.M) is not None)
        self.check("terminal on: hermes update blocked", MESSAGE_UPDATE in output and "rc=1" in output)
        _code, output = terminal.run("hermes config path 2>&1 | head -2", until="/config/hermes")
        self.check("terminal on: hermes finds HERMES_HOME", "/config/hermes" in output, output.strip().splitlines()[-2:])
        self.check("terminal on: closes", terminal.close() == 200)
        result = self.direct_status("GET", "terminal/").split()
        self.check("terminal on: /terminal/ stays 404, JSON", result[0] == "404" and "json" in result[1], result)
        healthy = self.harness.restart({"enable_terminal": False})
        status = self.user.request("POST", "api/terminal/start", {"session_id": "x"})[0]
        self.check("terminal off again: refused", healthy and status == 404, f"HTTP {status}")

    def section_settings(self):
        self.user.request("POST", "api/settings", {"check_for_updates": True, "ignore_agent_updates": False, "bot_name": "KeptName"})
        self.harness.restart()
        output = self.as_root("jq -c '{check_for_updates, ignore_agent_updates, bot_name}' /config/hermes/webui/settings.json").stdout.strip()
        self.check("settings: a restart resets only the enforced keys",
                   output == '{"check_for_updates":false,"ignore_agent_updates":true,"bot_name":"KeptName"}', output)
        self.as_hermes("printf 'not json' > /config/hermes/webui/settings.json")
        self.harness.restart()
        output = self.as_root("cd /config/hermes/webui; jq -c '{check_for_updates}' settings.json; ls settings.json.invalid; stat -c '%U' settings.json").stdout.split()
        self.check("settings: a broken file doesn't stop the start", output[:1] == ['{"check_for_updates":false}'] and "hermes" in output, output)
        self.as_root("rm -f /config/hermes/webui/settings.json.invalid")
        self.as_root("touch /config/hermes/root-owned.txt; chown root:root /config/hermes/root-owned.txt")
        self.harness.restart()
        output = self.as_root("stat -c '%U' /config/hermes/root-owned.txt; rm -f /config/hermes/root-owned.txt").stdout.strip()
        self.check("files: root-owned files become usable at the next start", output == "hermes", output)

    def section_env_vars(self):
        self.as_hermes("printf '# my comment\\nMY_HAND_KEY=keep-me\\n' >> /config/hermes/.env")
        entries = [{"name": "HARNESS_KEY", "value": FAKE_ENV_SECRET}, {"name": "HARNESS_QUOTED", "value": 'a "b" \\c $d'}]
        healthy = self.harness.restart({"env_vars": entries})
        env = self.env_file()
        self.check("env_vars: restart healthy", healthy)
        self.check("env_vars: written to .env", f"HARNESS_KEY={FAKE_ENV_SECRET}" in env and 'HARNESS_QUOTED="a \\"b\\" \\\\c $d"' in env)
        self.check("env_vars: hand-added key and comment stay", "MY_HAND_KEY=keep-me" in env and "# my comment" in env)
        self.no_secret_in_logs("env_vars: the value isn't logged", FAKE_ENV_SECRET)
        code = self.as_root("stat -c '%a %U' /config/hermes/.env; stat -c '%a %U' /data/env_vars_names.json; cat /data/env_vars_names.json").stdout
        self.check("env_vars: marker holds names only, root 0600", "600 hermes" in code and "600 root" in code and FAKE_ENV_SECRET not in code, code.split())
        loaded = self.as_hermes("hermes config env-path 2>/dev/null | head -1; cd /opt/hermes && /opt/hermes/.venv/bin/python -c \""
                                "from hermes_cli.env_loader import load_hermes_dotenv; import os; load_hermes_dotenv(); print(os.environ.get('HARNESS_QUOTED'))\"").stdout
        self.check("env_vars: Hermes reads the quoted value back unchanged", 'a "b" \\c' in loaded, loaded.strip().splitlines()[-1:])

        healthy = self.harness.restart({"env_vars": [{"name": "HARNESS_KEY", "value": "second-value"}]})
        env = self.env_file()
        self.check("env_vars: an update replaces in place, once", healthy and env.count("HARNESS_KEY=") == 1 and "second-value" in env)
        self.check("env_vars: a removed name leaves .env", "HARNESS_QUOTED" not in env)
        self.harness.restart({"env_vars": []})
        env = self.env_file()
        self.check("env_vars: an empty list removes ours, keeps the user's", "HARNESS_KEY" not in env and "MY_HAND_KEY=keep-me" in env)
        self.check("env_vars: the provider key from the wizard stays", FAKE_PROVIDER_KEY in env)

        self.as_hermes("printf 'DISABLE_UPDATES=hand-made-flag\\nHASS_TOKEN=hand-made-value\\n' >> /config/hermes/.env")
        self.harness.restart({"env_vars": []})
        logs = self.harness.logs()
        self.check("reserved: names edited into .env get a warning", "reserved name DISABLE_UPDATES" in logs and "reserved name HASS_TOKEN" in logs)
        self.check("reserved: their values aren't logged", "hand-made-flag" not in logs and "hand-made-value" not in logs)
        env = self.env_file()
        self.check("reserved: nothing is deleted", "DISABLE_UPDATES=hand-made-flag" in env and "HASS_TOKEN=hand-made-value" in env)
        self.as_hermes("sed -i '/^DISABLE_UPDATES=/d;/^HASS_TOKEN=/d' /config/hermes/.env")

        for name in ("HASS_TOKEN", "HERMES_WEBUI_PASSWORD", "CLAUDE_CODE_OAUTH_TOKEN", "HOME", "WEBHOOK_ENABLED"):
            self.harness.stop_app()
            self.harness.start_app({"enable_terminal": False, "claude_subscription": False, "log_level": "info",
                                    "env_vars": [{"name": name, "value": FAKE_ENV_SECRET}]})
            deadline = time.monotonic() + 30
            while container_state(APP) == "running" and time.monotonic() < deadline:
                time.sleep(0.5)
            logs = self.harness.logs()
            state = container_state(APP)
            exit_code = docker("container", "inspect", "--format", "{{.State.ExitCode}}", APP).stdout.strip()
            self.check(f"reserved: env_vars {name} stops the start", state == "exited" and exit_code != "0" and f"'{name}' is reserved" in logs,
                       f"state {state}, exit {exit_code}")
            self.check(f"reserved: env_vars {name}, value not logged", FAKE_ENV_SECRET not in logs)
        self.harness.stop_app()
        self.check("restart after a rejected option", self.harness.start_app({"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info"}) is None and self.harness.wait_healthy())

    def section_ha_token(self):
        healthy = self.harness.restart({"homeassistant_token": LONG_LIVED_TOKEN})
        logs = self.harness.logs()
        self.check("ha token: port 8123 found", healthy and "Home Assistant access mode: long-lived-token (http://homeassistant:8123)" in logs)
        self.no_secret_in_logs("ha token: not in the log", LONG_LIVED_TOKEN)
        env = self.as_root("grep -cF harness-fake-long-lived-token /config/hermes/.env").stdout.strip()
        self.check("ha token: not in .env", env == "0", env)
        call = {"calls": [{"name": "ha_get_state", "arguments": {"entity_id": "sun.sun"}}]}
        _session, timeline = run_chat(self.user, "CALL_TOOL tool_call " + json.dumps(call))
        calls = [entry for entry in self.harness.stub_calls() if entry["path"] == "/api/states/sun.sun"]
        self.check("ha token: the tool uses it directly", "below_horizon" in answer_text(timeline) and calls and calls[-1]["route"] == "direct" and calls[-1]["authorized"], calls[-1:])
        # The bracket keeps this command's own line from matching.
        argv = self.as_root("cat /proc/*/cmdline 2>/dev/null | tr '\\0' '\\n' | grep -c 'harness-fake-long-lived-toke[n]'").stdout.strip()
        self.check("ha token: not on any command line", argv == "0", argv)

        remove_container(HA)
        docker("run", "--detach", "--name", HA, "--label", "io.hermes-webui.harness=1", "--network", "hermes-harness", "--ip", "172.30.32.3",
               "--network-alias", "supervisor", "--network-alias", "homeassistant",
               "--mount", f"type=bind,source={Path(__file__).resolve().parent / 'stubs'},target=/stubs,readonly",
               "--entrypoint", "/opt/hermes/.venv/bin/python", self.harness.image, "-I", "/stubs/ha_api_stub.py",
               "--ports", "80", "--direct-port", "80", "--supervisor-token", SUPERVISOR_TOKEN, "--long-lived-token", LONG_LIVED_TOKEN)
        healthy = self.harness.restart({"homeassistant_token": LONG_LIVED_TOKEN})
        self.check("ha token: port 80 found when 8123 is closed", healthy and "long-lived-token (http://homeassistant:80)" in self.harness.logs())
        remove_container(HA)
        self.harness.start_stubs()

        for label, value in (("wrong token", "harness-wrong-token"), ("token with a line break", "abc\ndef")):
            healthy = self.harness.restart({"homeassistant_token": value})
            logs = self.harness.logs()
            self.check(f"ha token: {label} falls back with a warning", healthy and "was not accepted" in logs and "access mode: supervisor" in logs)
            self.check(f"ha token: {label} isn't logged", value not in logs)
        self.harness.restart()

    def section_log_level(self):
        self.harness.restart({"log_level": "debug"})
        logs = self.harness.logs()
        managed = self.as_root("grep -E '^  level:' /etc/hermes/config.yaml").stdout.strip()
        self.check("log_level debug: reaches the managed layer", managed == "level: DEBUG", managed)
        self.check("log_level debug: reaches nginx", "error_log /dev/stderr info;" in self.as_root("cat /run/hermes-app/nginx-log.conf").stdout)
        self.check("log_level debug: init logs its details", "Startup checks done." in logs)
        _session, _timeline = run_chat(self.user, "hello for the debug log")
        count = self.as_root("grep -c ' DEBUG ' /config/hermes/logs/agent.log").stdout.strip()
        self.check("log_level debug: agent.log has DEBUG lines", count.isdigit() and int(count) > 0, count)
        output = self.as_hermes("hermes config set logging.level INFO 2>&1; echo rc=$?").stdout
        self.check("log_level: hermes config set answers that it's managed", "administrator" in output, output.strip().splitlines()[-1] if output else "")
        self.harness.restart({"log_level": "error"})
        logs = self.harness.logs()
        self.check("log_level error: routine init lines are gone", "Terminal: off" not in logs and "access mode" not in logs)
        managed = self.as_root("grep -E '^  level:' /etc/hermes/config.yaml").stdout.strip()
        self.check("log_level error: reaches the managed layer", managed == "level: ERROR", managed)
        self.harness.restart()

    def wait_exit(self, timeout=30):
        deadline = time.monotonic() + timeout
        while container_state(APP) == "running" and time.monotonic() < deadline:
            time.sleep(0.5)
        return container_state(APP)

    def section_root_files(self):
        """What runs as root takes nothing from paths the runtime user can write, nor follows links there."""
        output = self.as_root("printenv HOME; stat -c '%U %a' /root; stat -c '%U %a' /tmp/hermes-home").stdout.splitlines()
        self.check("root: its HOME is its own", output[:2] == ["/root", "root 700"], output)
        self.check("root: the runtime user keeps its HOME under /tmp", output[2:] == ["hermes 700"], output)

        self.as_hermes(r"""cd /tmp/hermes-home
printf 'url = "file:///run/hermes-app/ha-access.env"\noutput = "/tmp/hermes-home/loot"\n' > .curlrc
printf 'def broken(' > .jq
""")
        # s6 runs the webui's run script again, with its check as root.
        self.as_root("pkill -9 -f 'python serve[r].py'")
        time.sleep(4)
        self.check("curlrc: the webui comes back after a kill", self.harness.wait_healthy(60))
        loot = self.as_hermes("cat /tmp/hermes-home/loot 2>/dev/null | wc -c").stdout.strip()
        self.check("curlrc: the readiness check ignores it", loot == "0", f"{loot} bytes copied")

        # A plain restart keeps /tmp, so init's root tools meet the planted files too.
        self.harness.write_options({"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info",
                                    "homeassistant_token": LONG_LIVED_TOKEN})
        docker("restart", "--time", "60", APP, check=False, timeout=90)
        healthy = self.harness.wait_healthy(60)
        self.check("jq and curlrc: init ignores them after a restart", healthy, self.harness.logs()[-300:] if not healthy else "")
        self.check("curlrc: the token probe still works", "long-lived-token" in self.harness.logs())
        loot = self.as_hermes("cat /tmp/hermes-home/loot 2>/dev/null | wc -c").stdout.strip()
        self.check("curlrc: the token probe ignores it", loot == "0", f"{loot} bytes copied")

        # What the Supervisor's restore leaves: everything is root's, and the CLI's files are 0600.
        self.as_root("mkdir -m 0700 /data/claude/restored; echo kept > /data/claude/restored/file; chmod 0600 /data/claude/restored/file; "
                     "ln -s /etc/shadow /data/claude/restored/link")
        healthy = self.harness.restart()
        output = self.as_hermes("cat /data/claude/restored/file; find /data/claude ! -user hermes | wc -l; stat -c '%U' /etc/shadow; "
                                "stat -c '%a' /data/claude/restored /data/claude/restored/file").stdout.split()
        self.check("restore: root's files in /data/claude are the runtime user's again", healthy and output[:2] == ["kept", "0"], output)
        self.check("restore: a link there doesn't hand over its target", output[2:3] == ["root"], output)
        self.check("restore: their modes stay", output[3:] == ["700", "600"], output)
        self.as_root("rm -rf /data/claude/restored")

        # The Supervisor's restore stops at a pipe, and it would take the whole restore down with it.
        self.as_hermes("cd /config/hermes/workspace && mkfifo gate-pipe && echo kept > gate-file && mkfifo /data/claude/gate-pipe")
        self.harness.restart()
        output = self.as_hermes("cd /config/hermes/workspace; ls -A . | grep gate- ; ls -A /data/claude | grep -c gate-").stdout.split()
        self.check("stop: pipes go, and other files stay", output == ["gate-file", "0"], output)
        self.as_hermes("rm -f /config/hermes/workspace/gate-file")
        self.harness.restart()

        def helper(script):
            docker("run", "--rm", "--network", "none", "--mount", f"type=bind,source={self.harness.config},target=/config",
                   "--entrypoint", "/bin/sh", self.harness.image, "-c", script, check=False)

        self.harness.stop_app()
        helper("mv /config/hermes /config/hermes.real && ln -s /opt/hermes-app /config/hermes")
        self.harness.start_app({"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info"})
        state = self.wait_exit()
        logs = self.harness.logs()
        changed = docker("diff", APP, check=False).stdout
        self.check("link: a symlinked HERMES_HOME stops the start", state == "exited" and "/config/hermes is a symbolic link" in logs, f"state {state}")
        self.check("link: nothing happens to its target", "/opt/hermes-app" not in changed, [line for line in changed.splitlines() if "/opt/hermes-app" in line][:3])
        self.harness.stop_app()
        helper("rm -f /config/hermes && mv /config/hermes.real /config/hermes")
        self.check("link: starts again once the folder is back",
                   self.harness.start_app({"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info"}) is None and self.harness.wait_healthy())

        # The agent's own loader can't read a folder, so a warning alone would leave the webui crash-looping.
        self.as_hermes("cd /config/hermes && mv .env .env.keep && mkdir .env")
        self.harness.stop_app()
        self.harness.start_app({"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info"})
        state = self.wait_exit()
        logs = self.harness.logs()
        self.check("env folder: stops the start and says why", state == "exited" and "/config/hermes/.env isn't a file" in logs and "IsADirectoryError" not in logs, f"state {state}")
        self.harness.stop_app()
        helper("cd /config/hermes && rmdir .env && mv .env.keep .env")
        self.check("env folder: starts again once the file is back", self.harness.restart())

        # This link passes the check for a file, and the runtime user still can't read what it leads to.
        self.as_hermes("cd /config/hermes && mv .env .env.keep && ln -s /etc/shadow .env")
        self.harness.stop_app()
        self.harness.start_app({"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info"})
        state = self.wait_exit()
        logs = self.harness.logs()
        self.check("env link to an unreadable file: stops the start and says why", state == "exited" and "/config/hermes/.env can't be read" in logs and "PermissionError" not in logs, f"state {state}")
        self.harness.stop_app()
        helper("cd /config/hermes && rm -f .env && mv .env.keep .env")
        self.check("env link: starts again once the file is back", self.harness.restart())

    def section_payload(self):
        seed = r"""cd /config/hermes
mkdir -p workspace/proj/tools workspace/hermes/tools skills/foo/tools workspace/proj/cache workspace/logs tools/sub/dir cache/sub logs/sub installs/x/env environments/x backups/config
echo keep > workspace/proj/tools/keep-a.txt
echo keep > workspace/hermes/tools/keep-b.txt
echo keep > skills/foo/tools/keep-c.txt
echo keep > workspace/proj/cache/keep-d.txt
echo keep > workspace/logs/keep-e.txt
echo keep > backups/config/config.yaml.keep.1
echo drop > tools/sub/dir/drop-2.txt
echo drop > cache/sub/drop-4.txt
echo drop > logs/sub/drop-6.log
echo drop > installs/x/env/drop-7
echo drop > environments/x/drop-8
head -c 100000 /dev/zero > backups/pre-migration-2026.zip
head -c 2000000 /dev/zero > models_dev_cache.json
head -c 2000000 /dev/zero > models_dev_cache.json.corrupt
head -c 100000 /dev/zero > sessions/request_dump_x_y.json
"""
        self.as_hermes(seed)
        result = self.harness.payload()
        names = {name for name, _size in result["files"]}
        self.check("payload: after use, under the gate", result["gate_passed"], f"{result['total_bytes']} bytes, uncompressed, without image.tar")
        kept = ["workspace/proj/tools/keep-a.txt", "workspace/hermes/tools/keep-b.txt", "skills/foo/tools/keep-c.txt",
                "workspace/proj/cache/keep-d.txt", "workspace/logs/keep-e.txt", "backups/config/config.yaml.keep.1"]
        self.check("payload: look-alike folders stay", not [name for name in kept if f"config/hermes/{name}" not in names])
        dropped = [name for name in names if "drop-" in name or "models_dev_cache" in name or "request_dump" in name or name.endswith(".zip")]
        self.check("payload: excluded files are gone", not dropped, dropped or "")
        self.as_hermes("cd /config/hermes; rm -rf workspace/proj workspace/hermes workspace/logs skills/foo tools installs environments backups/pre-migration-2026.zip "
                       "backups/config/config.yaml.keep.1 models_dev_cache.json models_dev_cache.json.corrupt sessions/request_dump_x_y.json")

    def section_fresh_payload(self):
        result = self.harness.payload()
        self.check("payload: fresh install, under the gate", result["gate_passed"], f"{result['total_bytes']} bytes, uncompressed, without image.tar")

    def section_shutdown(self):
        started = time.monotonic()
        docker("stop", "--time", "60", APP, check=False, timeout=90)
        elapsed = time.monotonic() - started
        code = docker("container", "inspect", "--format", "{{.State.ExitCode}}", APP).stdout.strip()
        self.check("shutdown: a stop finishes well inside timeout 60", elapsed < 45, f"{elapsed:.1f} s, exit code {code}")
        docker("rm", "--force", "--volumes", APP, check=False)
        healthy = self.harness.start_app({"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info"}) is None and self.harness.wait_healthy()
        self.check("shutdown: starts again on the same data", healthy)
        status = self.user.request("GET", "api/sessions")
        self.check("shutdown: sessions survive", status[0] == 200 and len(status[1].get("sessions", [])) > 0, f"{len(status[1].get('sessions', []))} sessions")

    # ---- Gateway and cron ----

    def wait_for(self, predicate, timeout, interval=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(interval)
        return predicate()

    def gateway_pids(self):
        """The gateway's Python process. The run script's waiting shell holds the same words, so arguments count."""
        script = "for d in /proc/[0-9]*; do printf '%s ' ${d#/proc/}; tr '\\0' '\\001' < $d/cmdline 2>/dev/null; echo; done"
        pids = []
        for line in self.as_root(script).stdout.splitlines():
            pid, _space, raw = line.partition(" ")
            arguments = raw.split("\x01")
            # Under arm64 emulation, QEMU's own path can come first.
            python = [index for index, argument in enumerate(arguments[:3]) if argument.startswith("/opt/hermes/.venv/bin/python")]
            if python and "gateway" in arguments[python[0] + 1:-1] and arguments[arguments.index("gateway") + 1] == "run":
                pids.append(pid)
        return pids

    def local_api(self, path):
        """The webui's API from inside the app, as a trusted local peer. Works without a network too."""
        result = self.harness.app_exec("curl", "-q", "-s", "-m", "20", "-H", "X-Remote-User-Id: harness", f"http://127.0.0.1:8787/{path}")
        try:
            return json.loads(result.stdout)
        except ValueError:
            return {}

    def cron_jobs(self):
        payload = self.local_api("api/crons")
        jobs = payload.get("jobs", payload) if isinstance(payload, dict) else payload
        return {job.get("name"): job for job in jobs or [] if isinstance(job, dict)}

    def listeners(self):
        """Every listening TCP socket in the app's network namespace, as address:port."""
        found = set()
        for name in ("/proc/net/tcp", "/proc/net/tcp6"):
            for line in self.as_root(f"cat {name}").stdout.splitlines()[1:]:
                fields = line.split()
                if len(fields) < 4 or fields[3] != "0A":
                    continue
                address, port = fields[1].split(":")
                raw = bytes.fromhex(address)
                # The kernel prints each 32-bit word in host byte order (little-endian here).
                raw = b"".join(raw[index:index + 4][::-1] for index in range(0, len(raw), 4))
                found.add(f"{ipaddress.ip_address(raw)}:{int(port, 16)}")
        return found

    def outside_listeners(self):
        return sorted(item for item in self.listeners() if not ipaddress.ip_address(item.rsplit(":", 1)[0].strip("[]")).is_loopback
                      and item not in ALLOWED_LISTENERS)

    def gateway_environment_names(self):
        pids = self.gateway_pids()
        if not pids:
            return []
        return self.as_hermes(f"tr '\\0' '\\n' < /proc/{pids[0]}/environ | sed 's/=.*//'").stdout.split()

    def notifications(self, needle):
        """Notifications the app created at the HA stub whose text holds needle, by either lane."""
        return [call for call in self.harness.stub_calls()
                if call["path"] in ("/api/services/persistent_notification/create", "/api/services/notify/persistent_notification")
                and needle in str((call.get("data") or {}).get("message", ""))]

    def hermes_cli(self, *arguments):
        return self.harness.app_exec("/opt/hermes-app/bin/hermes", *arguments, user="hermes", timeout=120)

    def write_cron_scripts(self):
        for name, body in CRON_SCRIPTS.items():
            self.harness.app_exec("sh", "-c", f"mkdir -p /config/hermes/scripts && cat > /config/hermes/scripts/{name} && chmod 755 /config/hermes/scripts/{name}",
                                  user="hermes", input_text=body)

    def remove_jobs(self, *names):
        for name, job in self.cron_jobs().items():
            if not names or name in names:
                self.hermes_cli("cron", "remove", job["id"])

    def section_gateway(self):
        pids = self.gateway_pids()
        owner = self.as_root(f"stat -c %U /proc/{pids[0]}").stdout.strip() if pids else ""
        self.check("gateway: runs as hermes", owner == "hermes", owner)
        names = self.gateway_environment_names()
        wanted = {"HASS_TOKEN", "HASS_URL", "SUPERVISOR_TOKEN", "HASS_HOME_CHANNEL", "GATEWAY_ON_ALL_ADAPTERS_DOWN",
                  "HERMES_S6_SUPERVISED_CHILD", "HERMES_SUPERVISED_CHILD"}
        self.check("gateway: its environment", wanted <= set(names) and "HASSIO_TOKEN" not in names,
                   sorted(wanted - set(names)) or "HASSIO_TOKEN unset")
        home = self.as_hermes(f"tr '\\0' '\\n' < /proc/{pids[0]}/environ | grep -E '^(HOME|HASS_HOME_CHANNEL)='" if pids else "true").stdout.split()
        self.check("gateway: HOME and the home channel", home == ["HOME=/tmp/hermes-home", "HASS_HOME_CHANNEL=persistent_notification"]
                   or sorted(home) == ["HASS_HOME_CHANNEL=persistent_notification", "HOME=/tmp/hermes-home"], home)
        connected = self.wait_for(lambda: [call for call in self.harness.stub_calls() if call["path"] == "subscribe_events:state_changed"], 60)
        auth = [call for call in self.harness.stub_calls() if call["method"] == "WS" and call["path"] == "/api/websocket"]
        self.check("gateway: connects to HA's WebSocket API through the Supervisor", connected and auth and auth[-1]["authorized"]
                   and auth[-1]["route"] == "supervisor", f"{len(auth)} logins, subscribed: {bool(connected)}")
        status = self.wait_for(lambda: self.user.request("GET", "api/gateway/status")[1].get("running") is True, 60)
        self.check("gateway: the webui shows it running", status, self.user.request("GET", "api/gateway/status")[1])
        outside = self.outside_listeners()
        self.check("gateway: no listener beyond nginx's ingress port", not outside, outside or sorted(self.listeners()))
        self.check("gateway: no online message to Home Assistant", not self.notifications(""),
                   f"{len(self.notifications(''))} notifications")
        rss = self.as_root(f"grep VmRSS /proc/{pids[0]}/status" if pids else "true").stdout.split()
        self.check("gateway: memory after start", True, " ".join(rss[1:3]) or "unknown")

    def section_cron(self):
        self.write_cron_scripts()
        self.as_hermes("rm -f /config/hermes/workspace/cron-probe.log")
        notifications_before = len(self.notifications("harness-fail"))
        created = [
            self.hermes_cli("cron", "create", "* * * * *", "--name", "harness-probe", "--no-agent", "--script", "harness-probe.sh", "--deliver", "local"),
            self.hermes_cli("cron", "create", "* * * * *", "HARNESS_CRON_AGENT say hello", "--name", "harness-agent", "--deliver", "local"),
            self.hermes_cli("cron", "create", "* * * * *", "--name", "harness-fail", "--no-agent", "--script", "harness-fail.sh", "--deliver", "origin"),
        ]
        self.check("cron: three jobs created with the CLI", all(result.returncode == 0 for result in created),
                   [result.stderr.strip()[-120:] for result in created if result.returncode])
        # Created in a webui chat, so their origin is the webui session (#87932).
        for name, extra in (("harness-webui-origin", {}), ("harness-webui-alert", {"failure_deliver": "homeassistant"})):
            arguments = {"action": "create", "schedule": "* * * * *", "name": name, "prompt": "run the script", "script": "harness-fail.sh",
                         "no_agent": True, **extra}
            # In a WebUI chat the cron tool, cronjob_manage, sits behind the tool_call bridge.
            call = {"calls": [{"name": "cronjob_manage", "arguments": arguments}]}
            _session, timeline = run_chat(self.user, "CALL_TOOL tool_call " + json.dumps(call))
        jobs = self.cron_jobs()
        origin = (jobs.get("harness-webui-origin") or {}).get("origin") or {}
        self.check("cron: a job created in a webui chat has the webui as its origin", origin.get("platform") == "webui",
                   {"origin platform": origin.get("platform"), "jobs": sorted(jobs)})

        def probe_lines():
            lines = self.as_root("cat /config/hermes/workspace/cron-probe.log 2>/dev/null").stdout.splitlines()
            return lines if len(lines) >= 2 else None

        lines = self.wait_for(probe_lines, 200, 5) or []
        stamps = [int(line.split()[-1]) for line in lines if line.split() and line.split()[-1].isdigit()]
        gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:])]
        self.check("cron: a no_agent job fires by itself, every minute", len(stamps) >= 2 and all(40 <= gap <= 80 for gap in gaps),
                   f"{len(stamps)} runs, gaps {gaps} s")
        self.check("cron: its script gets SUPERVISOR_TOKEN, not HASS_TOKEN or HASSIO_TOKEN",
                   lines and "SUPERVISOR_TOKEN=set" in lines[-1] and "HASS_TOKEN=unset" in lines[-1] and "HASSIO_TOKEN=unset" in lines[-1],
                   " ".join(lines[-1].split()[:4]) if lines else "")
        prompts = self.wait_for(lambda: [entry for entry in self.harness.model_log()
                                         if "running as a scheduled cron job" in entry.get("last_text", "")], 120, 5)
        agent = self.wait_for(lambda: self.cron_jobs().get("harness-agent", {}).get("last_status") == "ok", 60, 5)
        self.check("cron: an agent job fires with the stub model", prompts and agent,
                   f"{len(prompts or [])} model calls, last_status {self.cron_jobs().get('harness-agent', {}).get('last_status')}")

        def failed_twice():
            job = self.cron_jobs().get("harness-fail", {})
            outputs = self.as_root(f"ls /config/hermes/cron/output/{job.get('id', 'x')} 2>/dev/null | wc -l").stdout.strip()
            return job.get("last_status") == "error" and outputs.isdigit() and int(outputs) >= 2

        twice = self.wait_for(failed_twice, 150, 5)
        alerts = self.notifications("harness-fail")[notifications_before:]
        self.check("cron: a failing job alerts Home Assistant, once", twice and len(alerts) == 1,
                   f"failed twice: {bool(twice)}, {len(alerts)} notification(s), lane {[alert['path'] for alert in alerts]}")
        jobs = self.cron_jobs()
        webui_job, alert_job = jobs.get("harness-webui-origin", {}), jobs.get("harness-webui-alert", {})
        self.check("cron: a webui-made job's alert to its origin fails upstream (#87932)",
                   "webui" in str(webui_job.get("last_delivery_error")) and not self.notifications("harness-webui-origin"),
                   {"last_status": webui_job.get("last_status"), "last_delivery_error": str(webui_job.get("last_delivery_error"))[:80]})
        self.check("cron: with failure_deliver homeassistant it reaches HA", len(self.notifications("harness-webui-alert")) == 1,
                   f"{len(self.notifications('harness-webui-alert'))} notification(s), last_status {alert_job.get('last_status')}")
        self.check("cron: the webui lists the failure", webui_job.get("last_status") == "error" and webui_job.get("last_error"),
                   str(webui_job.get("last_error"))[:80])
        leaked = self.as_root("grep -rlF harness-fake-supervisor-token /config/hermes 2>/dev/null | wc -l").stdout.strip()
        self.check("cron: the Supervisor token is nowhere in HERMES_HOME", leaked == "0", leaked)
        rss = self.as_root(f"grep VmRSS /proc/{self.gateway_pids()[0]}/status" if self.gateway_pids() else "true").stdout.split()
        self.check("cron: gateway memory after cron runs", True, " ".join(rss[1:3]) or "unknown")
        self.check("cron: container memory after cron runs", True, self.memory_mb())
        self.remove_jobs("harness-agent", "harness-fail", "harness-webui-origin", "harness-webui-alert")

    def section_gateway_restarts(self):
        notified = len(self.notifications(""))
        before = self.gateway_pids()
        self.as_root(f"kill -9 {before[0]}" if before else "true")
        after = self.wait_for(lambda: [pid for pid in self.gateway_pids() if pid not in before], 30)
        self.check("gateway: kill -9 recovers", after, f"{before} -> {after}")
        mark = int(time.time())
        fired = self.wait_for(lambda: [line for line in self.as_root("cat /config/hermes/workspace/cron-probe.log").stdout.splitlines()
                                       if line.split()[-1:] and line.split()[-1].isdigit() and int(line.split()[-1]) > mark], 130, 5)
        self.check("gateway: cron fires after the kill", fired, f"{len(fired or [])} run(s)")
        self.check("gateway: one gateway, its lock taken over", len(self.gateway_pids()) == 1, self.gateway_pids())

        # A stop while a job runs: the gateway drains it for up to 30 s, timeout-kill ends it at 45 s.
        self.hermes_cli("cron", "create", "* * * * *", "--name", "harness-slow", "--no-agent", "--script", "harness-slow.sh", "--deliver", "local")
        self.as_hermes("rm -f /config/hermes/workspace/cron-slow.started")
        running = self.wait_for(lambda: "yes" in self.as_root("test -e /config/hermes/workspace/cron-slow.started && echo yes").stdout, 130, 1)
        started = time.monotonic()
        docker("stop", "--time", "60", APP, check=False, timeout=90)
        elapsed = time.monotonic() - started
        code = docker("container", "inspect", "--format", "{{.State.ExitCode}}", APP).stdout.strip()
        self.check("gateway: a stop during a running job ends within timeout 60", running and elapsed < 60, f"{elapsed:.1f} s, exit code {code}")
        docker("rm", "--force", "--volumes", APP, check=False)
        healthy = self.harness.start_app({"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info"}) is None \
            and self.harness.wait_healthy()
        self.check("gateway: back after the stop", healthy and self.wait_for(self.gateway_pids, 60))
        self.remove_jobs("harness-slow")
        added = [str(call.get("data", {}).get("message", ""))[:60] for call in self.notifications("")[notified:]]
        self.check("gateway: no online or shutdown message to HA after a kill and a stop", not added, added or "none")

    def env_lines(self, values):
        return "".join(f"{name}={value}\n" for name, value in values.items())

    def section_gateway_isolated(self):
        """Fake messenger tokens and listener settings, with the app on no network at all."""
        self.as_hermes("cp /config/hermes/.env /config/hermes/.env.harness-keep")
        self.harness.app_exec("sh", "-c", "cat >> /config/hermes/.env", user="hermes",
                              input_text=self.env_lines({**FAKE_TELEGRAM, **LISTENER_ATTEMPTS, **OPEN_ACCESS}))
        mark = int(time.time())
        healthy = self.harness.restart(isolated=True)
        self.check("isolated: healthy with a fake Telegram token and listener settings in .env", healthy)
        logs = self.harness.logs()
        for name in ("WEBHOOK_ENABLED", "API_SERVER_KEY", "TELEGRAM_WEBHOOK_URL", "MSGRAPH_WEBHOOK_PORT", "LINE_HOST",
                     "A2A_HOST", "SMS_WEBHOOK_HOST", "BLUEBUBBLES_WEBHOOK_HOST"):
            self.check(f"isolated: .env's {name} gets a warning", f"reserved name {name}" in logs)
        for name in OPEN_ACCESS:
            self.check(f"isolated: .env's {name} gets an open-access warning", f".env sets {name}," in logs)
        secrets = [value for value in {**FAKE_TELEGRAM, **LISTENER_ATTEMPTS, **OPEN_ACCESS}.values() if len(value) > 8]
        self.check("isolated: no value from .env in the log", not [value for value in secrets if value in logs])
        self.wait_for(self.gateway_pids, 60)
        time.sleep(20)
        outside = self.outside_listeners()
        self.check("isolated: still no listener beyond nginx's port", not outside, outside or sorted(self.listeners()))
        # The adapters connect one after another; under arm64 emulation that takes well over 20 s.
        listening = self.wait_for(lambda: EXPECTED_LOOPBACK <= set(self.listeners()) and set(self.listeners()), 120, 5) or set(self.listeners())
        self.check("isolated: A2A and SMS started, on loopback only", EXPECTED_LOOPBACK <= listening,
                   sorted(EXPECTED_LOOPBACK - listening) or sorted(listening))
        outside = self.outside_listeners()
        self.check("isolated: no listener beyond nginx's port once the adapters are up", not outside, outside or sorted(listening))
        fired = self.wait_for(lambda: [line for line in self.as_root("cat /config/hermes/workspace/cron-probe.log").stdout.splitlines()
                                       if line.split()[-1:] and line.split()[-1].isdigit() and int(line.split()[-1]) > mark + 60], 160, 5)
        status = self.local_api("api/gateway/status")
        self.check("isolated: the gateway stays up, Telegram retrying", self.gateway_pids() and status.get("running") is True, status.get("running"))
        self.check("isolated: cron keeps firing with Telegram down", fired, f"{len(fired or [])} run(s) after the first minute")

        # Upstream #142969 reported exit 78 for an unpaired WhatsApp. Agent v0.22.7 parks an unusable
        # platform instead and keeps the gateway up for cron (gateway/run_startup.py, the all-failed case).
        self.as_hermes("cp /config/hermes/.env.harness-keep /config/hermes/.env && printf 'WHATSAPP_ENABLED=true\\n' >> /config/hermes/.env")
        mark = int(time.time())
        self.harness.restart(isolated=True)
        fired = self.wait_for(lambda: [line for line in self.as_root("cat /config/hermes/workspace/cron-probe.log").stdout.splitlines()
                                       if line.split()[-1:] and line.split()[-1].isdigit() and int(line.split()[-1]) > mark + 30], 160, 5)
        self.check("isolated: an unpaired WhatsApp leaves the gateway up, and cron fires",
                   fired and self.gateway_pids() and "exit code 78" not in self.harness.logs(), f"{len(fired or [])} run(s)")
        self.harness.stop_app()
        self.harness.start_app({"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info"}, isolated=True)
        self.harness.wait_local_healthy()
        self.as_hermes("mv /config/hermes/.env.harness-keep /config/hermes/.env")
        self.check("isolated: back on the network", self.harness.restart() and self.wait_for(self.gateway_pids, 60))

    def section_gateway_exit_78(self):
        """The real finish and run scripts under the real s6, with a stand-in run that ends with 78 once.

        Agent v0.22.7 exits 78 only for a token that another gateway holds while no platform connects, and
        the Home Assistant platform connects here, so a real 78 can't be produced in the harness."""
        directory = "/run/s6-rc/servicedirs/hermes-gateway"
        self.as_root(f"cp {directory}/run {directory}/run.real && printf '#!/bin/sh\\nif [ ! -e /run/hermes-app/once-78 ]; then "
                     f": > /run/hermes-app/once-78; exit 78; fi\\nexec {directory}/run.real\\n' > {directory}/run && chmod 755 {directory}/run")
        self.as_root(f"/command/s6-svc -r {directory}")
        error = self.wait_for(lambda: "configuration error (exit code 78)" in self.harness.logs(), 30, 1)
        paused = self.wait_for(lambda: "gateway-paused" not in self.as_root("ls /run/hermes-app").stdout
                               and "sleep 30" in self.as_root("ps -eo args").stdout, 20, 1)
        state = self.as_root(f"/command/s6-svstat {directory}").stdout.strip()
        self.check("exit 78 (simulated): an ERROR with the cause, a pause, and s6 keeps it up",
                   error and paused and state.startswith("up") and "want" not in state, state)
        began = time.monotonic()
        back = self.wait_for(self.gateway_pids, 90, 1)
        self.check("exit 78 (simulated): the gateway comes back after the pause", back, f"after {time.monotonic() - began:.0f} s more")
        self.as_root(f"rm -f /run/hermes-app/once-78 && /command/s6-svc -k {directory}")
        self.wait_for(lambda: "sleep 30" in self.as_root("ps -eo args").stdout, 20, 1)
        started = time.monotonic()
        docker("stop", "--time", "60", APP, check=False, timeout=90)
        self.check("exit 78 (simulated): a stop during the pause is quick", time.monotonic() - started < 15, f"{time.monotonic() - started:.1f} s")
        docker("rm", "--force", "--volumes", APP, check=False)
        healthy = self.harness.start_app({"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info"}) is None \
            and self.harness.wait_healthy()
        self.check("exit 78 (simulated): a fresh container starts normally", healthy and self.wait_for(self.gateway_pids, 60))
        self.remove_jobs()


# Order matters: each one builds on the state of the one before.
SECTIONS = ["startup", "fresh_payload", "ingress", "access", "denied", "terminal_off", "smuggling", "chat", "gateway", "cron",
            "gateway_restarts", "gateway_isolated", "gateway_exit_78", "uploads", "terminal_on", "settings", "env_vars", "ha_token", "log_level",
            "root_files", "payload", "shutdown"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=None)
    parser.add_argument("--only", nargs="*", choices=SECTIONS, help="run these sections on the running harness")
    parser.add_argument("--keep", action="store_true", help="leave the harness running")
    args = parser.parse_args()
    harness = Harness(args.image)
    smoke = Smoke(harness)
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
