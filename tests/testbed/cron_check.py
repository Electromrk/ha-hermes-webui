#!/usr/bin/env python3
"""Checks scheduled tasks on the testbed's real Supervisor: the gateway, a backup, a restore.

  cron_check.py [--slug local_hermes_webui] [--skip-backup] [--reset]

It finishes the setup wizard with the stub model if it is due, creates two no_agent tasks, and checks:
- a task fires by itself every minute, and its script gets SUPERVISOR_TOKEN but not HASS_TOKEN
- a failing task makes one persistent notification in the real Core, read through its WebSocket API
- the app opens no listener beyond nginx's ingress port
- a missed run is caught up once after the app was stopped
- the tasks still fire after a real cold backup and after a restore of it, with one catch-up run
- the backup payload stays under the gate's 5 MB, and the gateway's RAM

The backup it makes is removed again, and so are the tasks and its notifications. --reset then wipes the
app's data and options, as ingress_check.py --reset does. It prints no cookies or tokens.

The model stub must be running: ha-testbed.sh model-stub start.
"""
import argparse
import base64
import json
import re
import sys
import time

from ingress_check import FAILED, check, on_host, open_ingress, reset_app, section_chat, testbed, wait_healthy

SCRIPTS = {
    "testbed-probe.sh": "#!/bin/sh\nfor n in SUPERVISOR_TOKEN HASS_TOKEN HASS_URL HASSIO_TOKEN; do eval \"v=\\${$n:-}\"; "
                        "[ -n \"$v\" ] && printf '%s=set ' $n || printf '%s=unset ' $n; done >> /config/hermes/workspace/testbed-probe.log; "
                        "date +%s >> /config/hermes/workspace/testbed-probe.log\n",
    "testbed-fail.sh": "#!/bin/sh\necho testbed-cron-failure-output\nexit 3\n",
}
PROBE_LOG = "/config/hermes/workspace/testbed-probe.log"
BACKUP_LIMIT_BYTES = 5_000_000


def in_app(slug, script, user="hermes", stdin=None):
    """Runs a shell script in the app's container. on-host passes no stdin, so text for it travels as base64."""
    if stdin is not None:
        script = f"echo {base64.b64encode(stdin.encode()).decode()} | base64 -d | {{ {script}; }}"
    return on_host("docker", "exec", "--user", user, f"app_{slug}", "sh", "-c", script)


def hermes(slug, *arguments):
    quoted = " ".join("'" + argument.replace("'", "'\\''") + "'" for argument in arguments)
    return in_app(slug, f"HOME=/tmp/hermes-home /opt/hermes-app/bin/hermes {quoted}")


def probe_runs(slug):
    stamps = []
    for line in in_app(slug, f"cat {PROBE_LOG} 2>/dev/null").stdout.splitlines():
        words = line.split()
        if words and words[-1].isdigit():
            stamps.append((int(words[-1]), " ".join(words[:-1])))
    return stamps


def wait_for(predicate, timeout, interval=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def notifications(needle):
    result = testbed("ws", "persistent_notification/get")
    try:
        items = json.loads(result.stdout)
    except ValueError:
        return []
    return [item for item in items if item.get("title") == "Hermes Agent" and needle in item.get("message", "")]


def gateway_pid(slug):
    script = ("for d in /proc/[0-9]*; do a=$(tr '\\0' '\\001' < $d/cmdline 2>/dev/null); "
              "case \"$a\" in /opt/hermes/.venv/bin/python*gateway$(printf '\\001')run*) echo ${d#/proc/};; esac; done")
    pids = in_app(slug, script, user="root").stdout.split()
    return pids[0] if pids else None


def listeners(slug):
    """Listening TCP sockets in the app, other than loopback and nginx's 0.0.0.0:8099."""
    outside = []
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in in_app(slug, f"cat {name}", user="root").stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            address, port = fields[1].split(":")
            loopback = address in ("0100007F", "00000000000000000000000001000000") or address.endswith("7F") and len(address) == 8
            if not loopback and f"{address}:{int(port, 16)}" != "00000000:8099":
                outside.append(f"{address}:{int(port, 16)}")
    return outside


def remove_tasks(slug):
    """Removes every task, so a run that stopped halfway leaves no second probe behind."""
    for job in sorted(set(re.findall(r"\b([0-9a-f]{12})\b", hermes(slug, "cron", "list", "--all").stdout))):
        hermes(slug, "cron", "remove", job)


def runs_after(slug, moment):
    return [stamp for stamp, _flags in probe_runs(slug) if stamp > moment]


def catch_up_once(slug, label, started, missed):
    """Two or more missed runs collapse into one at the start. A run per missed minute would come back to
    back, seconds apart; one catch-up and the next regular run are at least 15 s apart."""
    first = wait_for(lambda: runs_after(slug, started), 150)
    if not first:
        return check(f"{label}: a run after the start", False, "none within 150 s")
    time.sleep(max(0, first[0] + 90 - time.time()))
    window = [stamp for stamp in runs_after(slug, started) if stamp <= first[0] + 90]
    gaps = [later - earlier for earlier, later in zip(window, window[1:])]
    return check(f"{label}: {missed} missed runs are caught up once", 1 <= len(window) <= 2 and all(gap >= 15 for gap in gaps),
                 f"{len(window)} run(s) in the 90 s from the first, gaps {gaps} s, first {first[0] - started} s after the start")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", default="local_hermes_webui")
    parser.add_argument("--skip-backup", action="store_true", help="leave out the backup and restore")
    parser.add_argument("--reset", action="store_true", help="wipe the app's data and options afterwards")
    args = parser.parse_args()
    slug = args.slug

    _url, client = open_ingress()
    check("start: app answers through real Ingress", wait_healthy(client))
    status, payload = client.request("GET", "api/onboarding/status")
    if payload.get("system", {}).get("setup_state") == "needs_provider":
        check("gateway: waits while the wizard is due", gateway_pid(slug) is None)
        section_chat(client)
    pid = wait_for(lambda: gateway_pid(slug), 60, 2)
    check("gateway: runs after the setup", pid is not None, pid)
    status = client.request("GET", "api/gateway/status")[1]
    check("gateway: the webui shows it running", status.get("running") is True, {key: status.get(key) for key in ("running", "health")})
    check("gateway: no listener beyond nginx's ingress port", not listeners(slug), listeners(slug) or "")

    remove_tasks(slug)
    for name, body in SCRIPTS.items():
        in_app(slug, f"mkdir -p /config/hermes/scripts && cat > /config/hermes/scripts/{name} && chmod 755 /config/hermes/scripts/{name}",
               stdin=body)
    in_app(slug, f"rm -f {PROBE_LOG}")
    before = len(notifications("testbed-fail"))
    created = [hermes(slug, "cron", "create", "* * * * *", "--name", "testbed-probe", "--no-agent", "--script", "testbed-probe.sh",
                      "--deliver", "local"),
               hermes(slug, "cron", "create", "* * * * *", "--name", "testbed-fail", "--no-agent", "--script", "testbed-fail.sh",
                      "--deliver", "origin")]
    check("cron: two tasks created", all(result.returncode == 0 for result in created),
          [result.stderr.strip()[-160:] for result in created if result.returncode])

    runs = wait_for(lambda: probe_runs(slug) if len(probe_runs(slug)) >= 2 else None, 200) or []
    gaps = [later - earlier for (earlier, _a), (later, _b) in zip(runs, runs[1:])]
    check("cron: a no_agent task fires by itself through the real app", len(runs) >= 2 and all(40 <= gap <= 80 for gap in gaps),
          f"{len(runs)} runs, gaps {gaps} s")
    flags = runs[-1][1] if runs else ""
    check("cron: its script gets SUPERVISOR_TOKEN, not HASS_TOKEN or HASSIO_TOKEN",
          "SUPERVISOR_TOKEN=set" in flags and "HASS_TOKEN=unset" in flags and "HASSIO_TOKEN=unset" in flags, flags)
    # docker exec doesn't carry the token, so this takes the one a script gets, from the gateway's process.
    # It reaches curl through stdin, never its command line, and nothing prints it.
    token_works = in_app(slug, f"export $(tr '\\0' '\\n' < /proc/{pid}/environ | grep '^SUPERVISOR_TOKEN='); "
                               "printf 'header = \"Authorization: Bearer %s\"\\n' \"$SUPERVISOR_TOKEN\" | "
                               "curl -q -s -K - -o /dev/null -w '%{http_code}' http://supervisor/core/api/states/sun.sun").stdout.strip() if pid else ""
    check("cron: that token reaches the Core API at http://supervisor/core/api", token_works == "200", f"HTTP {token_works}")

    alerts = wait_for(lambda: notifications("testbed-fail")[before:] or None, 200) or []
    time.sleep(70)
    alerts = notifications("testbed-fail")[before:]
    check("cron: a failing task makes one notification in the real Core", len(alerts) == 1,
          f"{len(alerts)} after two or more failed runs; title {alerts[0].get('title') if alerts else None}")

    rss = in_app(slug, f"grep VmRSS /proc/{pid}/status", user="root").stdout.split() if pid else []
    check("gateway: memory, measured", True, " ".join(rss[1:3]) or "unknown")
    memory = on_host("docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", f"app_{slug}").stdout.strip()
    check("app: container memory, measured", True, memory)

    testbed("ha", "apps", "stop", slug)
    time.sleep(150)
    started = int(time.time())
    testbed("ha", "apps", "start", slug)
    catch_up_once(slug, "after a stop of 150 s", started, "2")

    if not args.skip_backup:
        moment = int(time.time())
        output = testbed("backup", f"cron-check-{time.strftime('%Y%m%d-%H%M%S')}", timeout=900)
        backup_slug = (re.search(r"Backup slug: (\S+)", output.stdout) or [None, None])[1]
        payload_bytes = re.search(r"payload without image.tar (\d+) bytes", output.stdout)
        check("backup: a real cold backup", output.returncode == 0 and backup_slug, output.stderr.strip()[-200:])
        check("backup: payload under 5 MB with the gateway's files", payload_bytes and int(payload_bytes.group(1)) < BACKUP_LIMIT_BYTES,
              f"{payload_bytes.group(1) if payload_bytes else '?'} bytes without image.tar")
        fired = wait_for(lambda: runs_after(slug, moment + 5), 150)
        check("backup: tasks fire again after the backup", fired, f"{len(fired or [])} run(s)")
        if backup_slug:
            testbed("ha", "apps", "stop", slug)
            time.sleep(70)
            restored = int(time.time())
            result = testbed("restore", backup_slug, timeout=900)
            check("restore: the backup restores", result.returncode == 0, result.stderr.strip()[-200:])
            healthy = wait_for(lambda: gateway_pid(slug), 180, 3)
            check("restore: the gateway runs again", healthy)
            jobs = hermes(slug, "cron", "list").stdout
            check("restore: the tasks are back", "testbed-probe" in jobs and "testbed-fail" in jobs)
            catch_up_once(slug, "restore", restored, "the")
            testbed("ha", "backups", "remove", backup_slug)
    unrelated = [item.get("message", "")[:60] for item in notifications("") if "testbed-fail" not in item.get("message", "")]
    check("gateway: no online or shutdown notification after stops, backup and restore", not unrelated, unrelated or "none")

    remove_tasks(slug)
    in_app(slug, f"rm -f {PROBE_LOG} " + " ".join(f"/config/hermes/scripts/{name}" for name in SCRIPTS))
    for item in notifications(""):
        testbed("api", "POST", "/api/services/persistent_notification/dismiss", json.dumps({"notification_id": item["notification_id"]}))
    if args.reset:
        reset_app(slug)
    print(f"\n{len(FAILED)} failed" if FAILED else "\nAll checks passed.")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
