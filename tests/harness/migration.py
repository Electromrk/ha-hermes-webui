#!/usr/bin/env python3
"""Migration test (CLAUDE.md section 6): the previous version's image creates data, then the new image
starts on the same /data and /config, as an app update in Home Assistant does. Needs Docker and both
images; needs no internet. The keys used are fake.

  migration.py --old local/hermes_webui:0.1.0-amd64 [--new IMAGE] [--keep]

--new defaults to the image of the version in config.yaml. Every check prints PASS or FAIL with what it
measured, and any FAIL makes the exit code 1. --keep leaves the harness running on the new image.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Harness, default_image  # noqa: E402
from smoke import Smoke  # noqa: E402
from webui_client import answer_text, run_chat  # noqa: E402

OPTIONS = {"enable_terminal": True, "claude_subscription": True, "log_level": "info",
           "env_vars": [{"name": "MIGRATION_OPTION_KEY", "value": "from-the-option"}]}

# Files a user makes or edits, which an update must leave byte for byte.
USER_FILES = {
    "/config/hermes/workspace/migration/notes.md": "# Notes\n\nWritten by the old version.\n",
    "/config/hermes/memories/migration-memory.md": "The user's lights are in the living room.\n",
    "/config/hermes/skills/migration-skill/SKILL.md": "---\nname: migration-skill\ndescription: A user's own skill.\n---\n\nSay hello.\n",
    "/config/hermes/scripts/migration-cron.sh": "#!/bin/sh\ndate +%s >> /config/hermes/workspace/migration/cron.log\n",
}
CLAUDE_MARKER = "/data/claude/migration-marker"

# Everything under these changes on its own while the app runs, so only its owners are compared.
VOLATILE = ("/config/hermes/logs/", "/config/hermes/cache/", "/config/hermes/state.db", "/config/hermes/sessions/",
            "/config/hermes/webui/", "/config/hermes/cron/", "/config/hermes/workspace/migration/cron.log",
            "/config/hermes/gateway", "/config/hermes/channel_directory.json", "/config/hermes/.skills_prompt_snapshot.json",
            "/data/claude/")

INVENTORY = r"""
find /config /data -xdev \( -type f -o -type d -o -type l \) | sort | while read -r path; do
  if [ -f "$path" ] && [ ! -L "$path" ]; then sum=$(sha256sum "$path" | cut -c1-16); else sum=-; fi
  printf '%s\t%s\t%s\t%s\n' "$path" "$(stat -c '%a %U:%G %F' "$path")" "$sum" "$(readlink "$path" 2>/dev/null)"
done
"""

STATE_DB = r"""
import json, sqlite3
db = sqlite3.connect("file:/config/hermes/state.db?mode=ro", uri=True)
tables = [row[0] for row in db.execute("select name from sqlite_master where type='table'")]
counts = {name: db.execute(f'select count(*) from "{name}"').fetchone()[0] for name in tables if name in ("sessions", "messages")}
version = db.execute("pragma user_version").fetchone()[0]
print(json.dumps({"counts": counts, "user_version": version, "tables": sorted(tables)}))
"""


class Migration(Smoke):
    def inventory(self):
        entries = {}
        for line in self.as_root(INVENTORY).stdout.splitlines():
            path, kind, digest, target = (line.split("\t") + ["", "", ""])[:4]
            entries[path] = {"kind": kind, "sha": digest, "target": target}
        return entries

    def state_db(self):
        result = self.harness.app_exec("/opt/hermes/.venv/bin/python", "-I", "-c", STATE_DB, user="hermes")
        try:
            return json.loads(result.stdout)
        except ValueError:
            return {"error": result.stderr.strip()[-200:]}

    def sessions(self):
        status, payload = self.user.request("GET", "api/sessions")
        found = {}
        for session in (payload.get("sessions", []) if status == 200 else []):
            sid = session.get("session_id")
            detail = self.user.request("GET", f"api/session?session_id={sid}&messages=1")[1]
            messages = (detail.get("session") or detail).get("messages") or []
            found[sid] = {"title": session.get("title"), "messages": len(messages),
                          "texts": [str(message.get("content"))[:60] for message in messages]}
        return found

    def env_names(self):
        return sorted(line.split("=", 1)[0] for line in self.env_file().splitlines() if "=" in line and not line.startswith("#"))

    def cron_runs(self):
        lines = self.as_root("cat /config/hermes/workspace/migration/cron.log 2>/dev/null").stdout.split()
        return [int(value) for value in lines if value.isdigit()]

    def versions(self):
        script = ("/usr/local/bin/claude --version 2>/dev/null || claude --version; "
                  "sed -n 's/^version: *//p' /config/hermes/plugins/claude-subscription-directsdk/plugin.yaml; "
                  "readlink /config/hermes/plugins/claude-subscription-directsdk")
        return self.as_hermes(script).stdout.split("\n")

    # ---- Phase 1: the old version makes data ----

    def make_data(self):
        started = time.monotonic()
        self.check("old: app healthy", self.harness.up(OPTIONS), f"{self.harness.image}, {time.monotonic() - started:.0f} s")
        status, payload = self.setup_provider()
        self.check("old: setup wizard saves the stub provider", status == 200, f"HTTP {status} {str(payload)[:80]}")
        for path, body in USER_FILES.items():
            mode = "755" if path.endswith(".sh") else "644"
            self.harness.app_exec("sh", "-c", f'mkdir -p "$(dirname {path})" && cat > {path} && chmod {mode} {path}',
                                  user="hermes", input_text=body)
        self.harness.app_exec("sh", "-c", f"cat > {CLAUDE_MARKER}", user="hermes", input_text="not a credential\n")
        self.as_hermes("printf 'MIGRATION_HAND_KEY=added-by-hand\\n' >> /config/hermes/.env")

        first, timeline = run_chat(self.user, "first chat, first turn")
        _same, second_turn = run_chat(self.user, "first chat, second turn", first)
        _second, third = run_chat(self.user, "second chat")
        answers = [answer_text(item) for item in (timeline, second_turn, third)]
        self.check("old: three chat turns in two sessions", all(answers), [answer[:20] for answer in answers])
        self.first_session = first

        created = self.hermes_cli("cron", "create", "* * * * *", "--name", "migration-cron", "--no-agent",
                                  "--script", "migration-cron.sh", "--deliver", "local")
        self.check("old: a cron job made with the CLI", created.returncode == 0, created.stderr.strip()[-120:])
        runs = self.wait_for(self.cron_runs, 150, 5)
        self.check("old: the cron job fires", runs, f"{len(runs or [])} runs")

    # ---- Phase 2: the new version starts on the same data ----

    def update(self, new_image):
        before = {"inventory": self.inventory(), "db": self.state_db(), "sessions": self.sessions(),
                  "env": self.env_names(), "jobs": sorted(self.cron_jobs()), "versions": self.versions()}
        print(f"Before: state.db {before['db']}, {len(before['sessions'])} sessions, CLI and plugin {before['versions']}", flush=True)

        self.harness = Harness(new_image)
        switched = time.time()
        started = time.monotonic()
        healthy = self.harness.restart(OPTIONS)
        self.check("new: app healthy on the old data", healthy, f"{new_image}, {time.monotonic() - started:.0f} s")
        if not healthy:
            print(self.harness.logs()[-3000:])
            return
        logs = self.harness.logs()
        bad = [line for line in logs.splitlines() if any(word in line for word in ("Traceback", "ERROR", "CRITICAL"))]
        self.check("new: no errors in the start log", not bad, bad[:3] or "")

        status, onboarding = self.user.request("GET", "api/onboarding/status")
        self.check("new: the setup wizard is not due again", status == 200 and onboarding.get("completed") is True,
                   (onboarding.get("system") or {}).get("setup_state"))
        versions = self.versions()
        self.check("new: the new CLI and plugin run on the old data", versions != before["versions"] and any("0.3.5" in line for line in versions),
                   f"{before['versions'][:2]} -> {versions[:2]}")

        db = self.state_db()
        self.check("new: state.db keeps every session and message", db.get("counts") == before["db"].get("counts"),
                   f"{before['db'].get('counts')} -> {db.get('counts')}, user_version {before['db'].get('user_version')} -> {db.get('user_version')}")
        sessions = self.sessions()
        self.check("new: the webui lists the same sessions with their messages", sessions == before["sessions"],
                   {sid: (value["title"], value["messages"]) for sid, value in sessions.items()})
        _sid, timeline = run_chat(self.user, "first chat, after the update", self.first_session)
        after_turn = self.sessions().get(self.first_session, {})
        self.check("new: an old session goes on", answer_text(timeline) and after_turn.get("messages", 0) > before["sessions"][self.first_session]["messages"],
                   f"{before['sessions'][self.first_session]['messages']} -> {after_turn.get('messages')} messages")
        _sid, timeline = run_chat(self.user, "a new chat after the update")
        self.check("new: a new session works", answer_text(timeline), answer_text(timeline)[:20])

        inventory = self.inventory()
        changed = [path for path in USER_FILES if inventory.get(path) != before["inventory"].get(path)]
        self.check("new: the user's files are unchanged, modes included", not changed, changed or f"{len(USER_FILES)} files")
        self.check("new: config.yaml unchanged", inventory.get("/config/hermes/config.yaml") == before["inventory"].get("/config/hermes/config.yaml"))
        self.check("new: the marker in /data/claude stays", inventory.get(CLAUDE_MARKER) == before["inventory"].get(CLAUDE_MARKER)
                   and inventory.get("/data/claude", {}).get("kind", "").startswith("700 hermes"), inventory.get("/data/claude", {}).get("kind"))
        removed = [path for path in before["inventory"] if path not in inventory and not path.startswith(VOLATILE)]
        self.check("new: no file or folder is gone", not removed, removed[:5] or "")
        moved = [f"{path}: {before['inventory'][path]['kind']} -> {inventory[path]['kind']}" for path in before["inventory"]
                 if path in inventory and before["inventory"][path]["kind"] != inventory[path]["kind"]]
        self.check("new: no mode or owner changed", not moved, moved[:5] or "")
        content = [path for path in before["inventory"] if path in inventory and not path.startswith(VOLATILE)
                   and before["inventory"][path]["sha"] != inventory[path]["sha"]]
        self.check("new: no other file changed outside the running state", True, content or "none")
        env = self.env_names()
        self.check("new: .env keeps its keys, the hand-made one included",
                   env == before["env"] and "MIGRATION_HAND_KEY" in env and "MIGRATION_OPTION_KEY" in env,
                   sorted(set(before["env"]) ^ set(env)) or f"{len(env)} keys")
        modes = self.as_root("stat -c '%a %U' /config/hermes/.env /config/hermes").stdout.split("\n")
        self.check("new: .env and HERMES_HOME modes", modes[:2] == ["600 hermes", "700 hermes"], modes[:2])
        settings = self.as_root("jq -c '{check_for_updates, ignore_agent_updates}' /config/hermes/webui/settings.json").stdout.strip()
        self.check("new: update settings still enforced", settings == '{"check_for_updates":false,"ignore_agent_updates":true}', settings)

        self.check("new: the cron job is still there", sorted(self.cron_jobs()) == before["jobs"], sorted(self.cron_jobs()))
        fired = self.wait_for(lambda: [run for run in self.cron_runs() if run > switched], 150, 5)
        self.check("new: the cron job fires after the update", fired, f"{len(fired or [])} runs since the switch")
        self.check("new: memory", True, self.memory_mb())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--old", required=True, help="the previous version's image")
    parser.add_argument("--new", default=None, help="the new version's image (default: the version in config.yaml)")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    new_image = args.new or default_image()
    test = Migration(Harness(args.old))
    try:
        test.make_data()
        test.update(new_image)
    finally:
        if not args.keep:
            Harness(new_image).down()
    results = test.results
    print(f"\n{results.passed} passed, {len(results.failed)} failed")
    for name in results.failed:
        print(f"  FAIL: {name}")
    return 1 if results.failed else 0


if __name__ == "__main__":
    sys.exit(main())
