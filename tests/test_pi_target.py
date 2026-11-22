"""The safeguards of ha-testbed.sh's device mode. Needs no Docker, no network and no device: ssh and docker are stand-ins."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import json
import os
import socketserver
import stat
import struct
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "testbed" / "ha-testbed.sh"
MARKER = "0123456789abcdef0123456789abcdef"

FAKE_SSH = r"""#!/bin/sh
# Records the call, then runs the remote script here, as the device would.
printf '%s\n' "$*" >> "$FAKE_LOG"
# Like OpenSSH, accept-new stores the key at key exchange, before authentication can fail.
known="" accept=""
for arg; do
  case "$arg" in
    UserKnownHostsFile=*) known="${arg#UserKnownHostsFile=}" ;;
    StrictHostKeyChecking=accept-new) accept=1 ;;
  esac
done
[ -z "$accept" ] || echo "test-device ssh-ed25519 AAAAfake" >> "$known"
[ -z "$FAKE_SSH_EXIT" ] || exit "$FAKE_SSH_EXIT"
for last; do :; done
exec sh -c "$last"
"""
FAKE_DOCKER = r"""#!/bin/sh
case "$*" in
  "ps --format {{.Names}}") echo hassio_cli ;;
  *"ha core info"*) echo '{"data": {"version": "2026.11.0"}}' ;;
  *"ha apps --raw-json"*) echo "{\"data\": {\"apps\": $FAKE_APPS}}" ;;
  *"ha backups --raw-json"*) echo "{\"data\": {\"backups\": $FAKE_BACKUPS}}" ;;
  *) echo "unexpected docker call: $*" >&2; exit 1 ;;
esac
"""
FAKE_UNAME = '#!/bin/sh\necho "${FAKE_MACHINE:-aarch64}"\n'
# docker on the device, with a Supervisor whose job for the app outlasts the ha CLI, as on the test Pi on 2026-11-20:
# the rebuild stops the app, the new image appears at once, the job goes on, and at its end the Supervisor starts
# the app again, because it had been running. Time passes by ha calls. FAKE_JOB_LISTED=0 hides the job from
# "ha jobs info", so that only the Supervisor's refusals show it.
FAKE_SUPERVISOR = r"""#!/usr/bin/env python3
import json, os, sys

path = os.environ["FAKE_SUPERVISOR"]
with open(path) as handle:
    sup = json.load(handle)
slug = "local_hermes_webui"
busy = "Error: Another job is running for job group app_local_hermes_webui"
arguments = sys.argv[1:]
out, err, code = "", "", 0
if arguments[:3] == ["exec", "hassio_cli", "ha"]:
    call = " ".join(arguments[3:])
    sup["calls"].append(call)
    if sup["job_left"] > 0:
        sup["job_left"] -= 1
        if sup["job_left"] == 0:
            sup["state"] = sup["state_after_job"]
    running = sup["job_left"] > 0
    if call == f"apps info {slug} --raw-json":
        out = json.dumps({"result": "ok", "data": {"version": sup["version"], "state": sup["state"]}})
    elif call == "jobs info --raw-json":
        # A finished backup job of the app stays listed, as on the device.
        jobs = [{"name": "backup_manager_partial_backup", "reference": "8e7e294e", "done": True, "child_jobs": [
            {"name": "backup_app_save", "reference": slug, "done": True, "child_jobs": []}]}]
        if running and os.environ.get("FAKE_JOB_LISTED", "1") == "1":
            jobs.insert(0, {"name": "app_manager_rebuild", "reference": slug, "done": False, "child_jobs": [
                {"name": "app_rebuild", "reference": slug, "done": False, "child_jobs": []}]})
        out = json.dumps({"result": "ok", "data": {"ignore_conditions": [], "jobs": jobs}})
    elif call == f"apps rebuild --force {slug}":
        if os.environ.get("FAKE_REBUILD") == "refuse":
            err, code = "Error: Can't rebuild an image-based app", 1
        else:
            sup.update(state_after_job=sup["state"], state="stopped", image="sha256:new", job_left=int(os.environ["FAKE_JOB_CALLS"]))
            err = ('Error: Post "http://supervisor/addons/local_hermes_webui/rebuild": '
                   "context deadline exceeded (Client.Timeout exceeded while awaiting headers)")
            code = 1
    elif call in (f"apps start {slug}", f"apps stop {slug}"):
        if running:
            sup["refused"].append(call)
            err, code = busy, 1
        elif os.environ.get("FAKE_START") == "fail":
            err, code = "Error: App local_hermes_webui can't start", 1
        else:
            sup["state"] = "started" if call.split()[1] == "start" else "stopped"
            out = "Command completed successfully."
    else:
        err, code = f"unexpected ha call: {call}", 1
elif arguments[:2] == ["image", "ls"]:
    out = sup["image"]
else:
    err, code = f"unexpected docker call: {' '.join(arguments)}", 1
with open(path, "w") as handle:
    json.dump(sup, handle)
if out:
    print(out)
if err:
    print(err, file=sys.stderr)
sys.exit(code)
"""
# The waits here are about order, not time.
FAKE_SLEEP = "#!/bin/sh\nexit 0\n"


class FakeCoreSocket(socketserver.BaseRequestHandler):
    """Core's WebSocket API, just enough for the store reload before a build."""

    def handle(self):
        self.buffer = b""
        while b"\r\n\r\n" not in self.buffer:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            self.buffer += chunk
        self.buffer = self.buffer.split(b"\r\n\r\n", 1)[1]
        self.request.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        self.send({"type": "auth_required"})
        if self.receive().get("type") != "auth":
            return
        self.send({"type": "auth_ok"})
        message = self.receive()
        self.send({"id": message["id"], "type": "result", "success": True, "result": {}})

    def read(self, count):
        while len(self.buffer) < count:
            chunk = self.request.recv(65536)
            if not chunk:
                raise ConnectionError
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def send(self, message):
        payload = json.dumps(message).encode()
        self.request.sendall(bytes([0x81, len(payload)]) + payload)

    def receive(self):
        _first, second = self.read(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self.read(2))[0]
        mask = self.read(4)
        return json.loads(bytes(byte ^ mask[index % 4] for index, byte in enumerate(self.read(length))))


class Core(BaseHTTPRequestHandler):
    onboarding = []

    def do_GET(self):
        status, body = (401, b"{}") if self.path == "/api/" else (200, json.dumps(Core.onboarding).encode())
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class Case(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.config = self.base / "config"
        self.data = self.base / "data"
        self.data.mkdir()
        self.key = self.base / "key"
        self.key.write_text("not a real key\n")
        self.log = self.base / "ssh.log"
        self.log.touch()
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        for name, text in (("ssh", FAKE_SSH), ("docker", FAKE_DOCKER), ("uname", FAKE_UNAME)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.environment = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(self.base), "HERMES_TESTBED_TARGET": "pi",
            "HERMES_PI_CONFIG_DIR": str(self.config), "HERMES_PI_DATA_ROOT": str(self.data), "HERMES_PI_KEY": str(self.key),
            "FAKE_LOG": str(self.log), "FAKE_APPS": "[]", "FAKE_BACKUPS": "[]", "HERMES_TESTBED_TIMEOUT_CORE": "20",
        }

    def run_script(self, *arguments, **extra):
        return subprocess.run(["bash", str(SCRIPT), *arguments], text=True, capture_output=True, check=False, timeout=60,
                              env={**self.environment, **extra})

    def enroll_state(self):
        self.config.mkdir(exist_ok=True)
        (self.config / "target.env").write_text(f"HERMES_PI_HOST=test-device\nHERMES_PI_URL=http://test-device:8123\nHERMES_PI_MARKER_ID={MARKER}\n")
        (self.data / "hermes-test-marker").write_text(MARKER)

    def ssh_calls(self):
        return self.log.read_text().splitlines()


class EveryCommand(Case):
    def test_a_command_runs_only_with_the_right_marker(self):
        self.enroll_state()
        canary = self.base / "canary"
        result = self.run_script("on-host", "touch", str(canary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(canary.exists())

    def test_a_wrong_marker_stops_the_command_before_it_runs(self):
        self.enroll_state()
        (self.data / "hermes-test-marker").write_text("another-device")
        canary = self.base / "canary"
        result = self.run_script("on-host", "touch", str(canary))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(canary.exists())

    def test_a_missing_marker_stops_the_command(self):
        self.enroll_state()
        (self.data / "hermes-test-marker").unlink()
        canary = self.base / "canary"
        self.assertNotEqual(self.run_script("on-host", "touch", str(canary)).returncode, 0)
        self.assertFalse(canary.exists())

    def test_the_host_key_is_always_checked_against_our_own_file(self):
        self.enroll_state()
        self.run_script("on-host", "true")
        calls = self.ssh_calls()
        self.assertTrue(calls)
        for call in calls:
            with self.subTest(call=call):
                self.assertIn("StrictHostKeyChecking=yes", call)
                self.assertIn(f"UserKnownHostsFile={self.config}/known_hosts", call)
                self.assertIn("GlobalKnownHostsFile=/dev/null", call)
                self.assertIn("-F /dev/null", call)
                self.assertIn("BatchMode=yes", call)
                self.assertIn("-p 22222", call)
                self.assertIn(f"-i {self.key}", call)
                self.assertIn("root@test-device", call)

    def test_arguments_arrive_unchanged(self):
        self.enroll_state()
        for text in ("it's", "a b  c", "$HOME", "`id`", "line one\nline two", '"quoted"', "semi;colon"):
            with self.subTest(text=text):
                result = self.run_script("on-host", "printf", "%s", text)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, text)

    def test_inherited_variables_loosen_nothing(self):
        self.enroll_state()
        (self.data / "hermes-test-marker").write_text("another-device")
        canary = self.base / "canary"
        result = self.run_script("on-host", "touch", str(canary), PI_ENROLLING="1", PI_HOST_KEY_POLICY="no")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(canary.exists())
        self.assertTrue(all("StrictHostKeyChecking=yes" in call for call in self.ssh_calls()))

    def test_a_malformed_marker_is_refused_before_connecting(self):
        self.enroll_state()
        (self.config / "target.env").write_text("HERMES_PI_HOST=test-device\nHERMES_PI_URL=http://test-device:8123\nHERMES_PI_MARKER_ID=\"x' ] || [ 'a\"\n")
        canary = self.base / "canary"
        result = self.run_script("on-host", "touch", str(canary))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("malformed", result.stderr)
        self.assertFalse(canary.exists())
        self.assertEqual(self.ssh_calls(), [])

    def test_without_enrolment_nothing_connects(self):
        result = self.run_script("on-host", "true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("isn't enrolled", result.stderr)
        self.assertEqual(self.ssh_calls(), [])

    def test_ssh_failure_reports_the_device_not_answering(self):
        self.enroll_state()
        result = self.run_script("on-host", "true", FAKE_SSH_EXIT="255")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("doesn't answer", result.stderr)


class LocalOnlyCommands(Case):
    def test_down_and_reset_never_run_on_a_device(self):
        self.enroll_state()
        for command in (["down"], ["reset", "--yes"]):
            with self.subTest(command=command):
                result = self.run_script(*command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("local testbed", result.stderr)
        self.assertEqual(self.ssh_calls(), [])

    def test_the_credentials_stay_outside_the_repository(self):
        result = self.run_script("local-dir")
        self.assertEqual(result.stdout.strip(), f"{self.config}/local")
        self.assertFalse(result.stdout.strip().startswith(str(ROOT)))

    def test_paths_follow_the_target(self):
        self.assertEqual(self.run_script("supervisor-root").stdout.strip(), f"{self.data}/supervisor")
        local = self.run_script("supervisor-root", HERMES_TESTBED_TARGET="local")
        self.assertEqual(local.stdout.strip(), "/mnt/supervisor")

    def test_an_unknown_target_is_refused(self):
        result = self.run_script("status", HERMES_TESTBED_TARGET="production")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.ssh_calls(), [])


class Enrolment(Case):
    def setUp(self):
        super().setUp()
        self.server = HTTPServer(("127.0.0.1", 0), Core)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        Core.onboarding = [{"step": "user", "done": False}, {"step": "core_config", "done": False}]

    def enroll(self, **extra):
        return self.run_script("pi-enroll", "test-device", "--test-ssd-is-in", HERMES_PI_PROBE_URLS=self.url, **extra)

    def assert_not_enrolled(self):
        self.assertFalse((self.data / "hermes-test-marker").exists())
        self.assertFalse((self.config / "target.env").exists())
        self.assertFalse((self.config / "known_hosts").exists(), "a refused system must leave no pinned key")

    def test_it_needs_the_explicit_confirmation(self):
        for arguments in (["pi-enroll"], ["pi-enroll", "test-device"], ["pi-enroll", "test-device", "--yes"]):
            with self.subTest(arguments=arguments):
                self.assertNotEqual(self.run_script(*arguments).returncode, 0)
        self.assertEqual(self.ssh_calls(), [])
        self.assert_not_enrolled()

    def test_a_fresh_system_is_enrolled(self):
        result = self.enroll()
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = (self.data / "hermes-test-marker").read_text()
        self.assertEqual(len(marker), 32)
        settings = self.config / "target.env"
        self.assertEqual(stat.S_IMODE(settings.stat().st_mode), 0o600)
        text = settings.read_text()
        self.assertIn(marker, text)
        self.assertIn(self.url, text)
        self.assertIn("take the USB stick out", result.stdout)
        self.assertIn("ssh-ed25519", (self.config / "known_hosts").read_text())
        self.assertFalse((self.config / "known_hosts.enrolling").exists())
        self.assertNotIn(marker, result.stdout + result.stderr)

    def test_first_contact_accepts_a_new_host_key_and_later_ones_do_not(self):
        self.assertEqual(self.enroll().returncode, 0)
        calls = self.ssh_calls()
        self.assertIn("StrictHostKeyChecking=accept-new", calls[0])
        self.assertIn(f"UserKnownHostsFile={self.config}/known_hosts.enrolling", calls[0])
        self.assertTrue(all("StrictHostKeyChecking=yes" in call for call in calls[1:]))

    def test_a_system_with_a_user_is_refused(self):
        Core.onboarding = [{"step": "user", "done": True}]
        result = self.enroll()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already has a user", result.stderr)
        self.assert_not_enrolled()

    def test_a_system_with_apps_is_refused(self):
        result = self.enroll(FAKE_APPS='[{"slug": "core_samba", "installed": true}]')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed apps", result.stderr)
        self.assert_not_enrolled()

    def test_a_system_with_backups_is_refused(self):
        result = self.enroll(FAKE_BACKUPS='[{"slug": "abc"}]')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has backups", result.stderr)
        self.assert_not_enrolled()

    def test_another_architecture_is_refused(self):
        result = self.enroll(FAKE_MACHINE="x86_64")
        self.assertNotEqual(result.returncode, 0)
        self.assert_not_enrolled()

    def test_a_refused_connection_explains_itself(self):
        result = self.enroll(FAKE_SSH_EXIT="255")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("your real system", result.stderr)
        self.assert_not_enrolled()

    def test_a_second_enrolment_is_refused(self):
        self.assertEqual(self.enroll().returncode, 0)
        before = (self.data / "hermes-test-marker").read_text()
        result = self.enroll()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Already enrolled", result.stderr)
        self.assertEqual((self.data / "hermes-test-marker").read_text(), before)


class BuildOutlastsTheCli(Case):
    """The rebuild on the test Pi on 2026-11-20: the ha CLI gave up after 10 minutes, the new image was there while the app was
    stopped, and pi-install went on although the Supervisor's job still held the app. Then start was refused with "Another job
    is running" for 300 s, and gave up 5 s before the Supervisor finished and started the app itself."""

    def setUp(self):
        super().setUp()
        self.enroll_state()
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeCoreSocket)
        server.daemon_threads = True
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        (self.config / "target.env").write_text(
            f"HERMES_PI_HOST=test-device\nHERMES_PI_URL=http://127.0.0.1:{server.server_address[1]}\nHERMES_PI_MARKER_ID={MARKER}\n")
        local = self.config / "local"
        local.mkdir(mode=0o700)
        (local / ".gitignore").write_text("*\n")
        (local / "credentials.json").write_text(json.dumps({"long_lived_token": "fake-token"}))
        bin_dir = self.base / "bin"
        for name, text in (("docker", FAKE_SUPERVISOR), ("sleep", FAKE_SLEEP)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        # pi-install rebuilds when the device holds the version of the last commit, as on 2026-11-20.
        committed = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:hermes_webui/config.yaml"],
                                   text=True, capture_output=True, check=True).stdout
        version = next(line.split(":", 1)[1].strip().strip('"') for line in committed.splitlines() if line.startswith("version:"))
        self.state_file = self.base / "supervisor.json"
        self.write_supervisor(version=version, state="started", image="sha256:old", job_left=0, state_after_job="started")
        self.environment.update(FAKE_SUPERVISOR=str(self.state_file), FAKE_JOB_CALLS="6", HERMES_TESTBED_TIMEOUT_BUILD="60")

    def write_supervisor(self, **fields):
        self.state_file.write_text(json.dumps({"calls": [], "refused": [], **fields}))

    def supervisor(self):
        return json.loads(self.state_file.read_text())

    def test_install_waits_until_the_supervisor_has_finished_its_job(self):
        result = self.run_script("pi-install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gave up after its time limit", result.stderr)
        self.assertIn("The image is new.", result.stdout)
        sup = self.supervisor()
        self.assertEqual(sup["job_left"], 0, "pi-install returned while the Supervisor's job still held the app")
        self.assertTrue(result.stdout.rstrip().endswith(" started"), result.stdout)
        self.assertNotIn("apps start local_hermes_webui", sup["calls"], "the Supervisor starts the app itself")
        # What pi-gate.sh does next.
        result = self.run_script("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.supervisor()["refused"], [])

    def test_start_takes_another_job_as_busy_and_waits_for_it(self):
        # The new image is there and the app stopped, while the job goes on unseen in the job list.
        self.write_supervisor(version="0.1.1", state="stopped", image="sha256:new", job_left=8, state_after_job="started")
        result = self.run_script("start", FAKE_JOB_LISTED="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        sup = self.supervisor()
        self.assertGreaterEqual(len(sup["refused"]), 2, sup["calls"])
        self.assertEqual(sup["job_left"], 0)
        self.assertEqual(sup["state"], "started")
        self.assertIn("still busy", result.stdout)

    def test_start_waits_for_a_listed_job_before_it_asks(self):
        self.write_supervisor(version="0.1.1", state="stopped", image="sha256:new", job_left=5, state_after_job="started")
        result = self.run_script("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.supervisor()["refused"], [])
        self.assertEqual(self.supervisor()["state"], "started")

    def test_start_stops_waiting_at_the_build_limit(self):
        self.write_supervisor(version="0.1.1", state="stopped", image="sha256:new", job_left=10**9, state_after_job="started")
        result = self.run_script("start", FAKE_JOB_LISTED="0", HERMES_TESTBED_TIMEOUT_BUILD="2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another job is running", result.stderr)

    def test_another_refusal_of_start_is_not_retried(self):
        self.write_supervisor(version="0.1.1", state="stopped", image="sha256:old", job_left=0, state_after_job="stopped")
        result = self.run_script("start", FAKE_START="fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("can't start", result.stderr)
        self.assertEqual(self.supervisor()["calls"].count("apps start local_hermes_webui"), 1)

    def test_a_refused_rebuild_still_stops_at_once(self):
        result = self.run_script("pi-install", FAKE_REBUILD="refuse")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("before the ha CLI's time limit", result.stderr)
        self.assertNotIn("Waiting", result.stderr)


if __name__ == "__main__":
    unittest.main()
