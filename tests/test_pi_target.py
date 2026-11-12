"""The safeguards of ha-testbed.sh's device mode. Needs no Docker, no network and no device: ssh and docker are stand-ins."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import json
import os
import stat
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


if __name__ == "__main__":
    unittest.main()
