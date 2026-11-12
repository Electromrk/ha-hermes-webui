"""The Claude subscription switch and its guardrails (CLAUDE.md section 3). Needs no Docker and no network.

The init block runs for real in bash, cut out of hermes-init.sh, with a fake claude command. Everything
that needs the image or a Supervisor is in tests/harness/claude_smoke.py and tests/testbed/claude_check.py.
All keys here are fake.
"""

import contextlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "hermes_webui"
ROOTFS = APP / "rootfs"
LIB = ROOTFS / "opt/hermes-app/lib"
INIT = (ROOTFS / "etc/s6-overlay/scripts/hermes-init.sh").read_text(encoding="utf-8")
WRAPPER = ROOTFS / "opt/hermes-app/bin/claude"
DOCS = (APP / "DOCS.md").read_text(encoding="utf-8")
TRANSLATIONS = yaml.safe_load((APP / "translations/en.yaml").read_text(encoding="utf-8"))
MANIFEST = yaml.safe_load((APP / "config.yaml").read_text(encoding="utf-8"))
MANAGED = yaml.safe_load((APP / "managed-config.yaml").read_text(encoding="utf-8"))

sys.path.insert(0, str(LIB))
import env_sync  # noqa: E402

FAKE_VALUE = "fake-claude-test-value-a81f"
CONFLICTS = ["ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"]
# The plugin keeps these away from the CLI, so they don't stop it and need no warning.
DROPPED = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_API_KEY"]
PROVIDER = "claude-subscription-directsdk-experimental"
LINK = "plugins/claude-subscription-directsdk"


def clean_environment():
    return {key: value for key, value in os.environ.items() if key not in CONFLICTS}


def run_check(home, state, environment=None):
    """Returns (exit code, output lines) of env_sync.py claude-check."""
    output = io.StringIO()
    with mock.patch.dict(os.environ, {**clean_environment(), **(environment or {})}, clear=True):
        with contextlib.redirect_stdout(output):
            code = env_sync.main(["claude-check", "--state", state, "--home", str(home)])
    return code, output.getvalue().splitlines()


class Case(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name) / "hermes"
        (self.home / "plugins").mkdir(parents=True)

    def write_env(self, text, encoding="utf-8"):
        (self.home / ".env").write_bytes(text.encode(encoding))


class ConflictCheck(Case):
    def test_the_list_is_the_one_in_claude_md(self):
        self.assertEqual(list(env_sync.CLAUDE_CONFLICT_KEYS + env_sync.CLAUDE_CONFLICT_SWITCHES), CONFLICTS)

    def test_each_variable_in_env_is_named_without_its_value(self):
        for name in CONFLICTS:
            with self.subTest(name=name):
                self.write_env(f"{name}={FAKE_VALUE}\n")
                code, lines = run_check(self.home, "on")
                self.assertEqual(code, 0)
                self.assertEqual(len(lines), 1)
                self.assertIn(f"{name} (.env)", lines[0])
                self.assertNotIn(FAKE_VALUE, lines[0])
                self.assertTrue(lines[0].startswith("WARNING: "), lines[0])

    def test_each_variable_in_the_process_environment_is_named_without_its_value(self):
        self.write_env("")
        for name in CONFLICTS:
            with self.subTest(name=name):
                _code, lines = run_check(self.home, "on", {name: FAKE_VALUE})
                self.assertIn(f"{name} (environment)", lines[0])
                self.assertNotIn(FAKE_VALUE, lines[0])

    def test_all_are_named_at_once_and_the_warning_points_to_the_docs(self):
        self.write_env("CLAUDE_CODE_USE_VERTEX=1\n")
        _code, lines = run_check(self.home, "on", {"ANTHROPIC_BASE_URL": FAKE_VALUE})
        self.assertEqual(len(lines), 1)
        for expected in ("CLAUDE_CODE_USE_VERTEX (.env)", "ANTHROPIC_BASE_URL (environment)", "documentation"):
            self.assertIn(expected, lines[0])

    def test_what_the_plugin_drops_gets_no_warning(self):
        for name in DROPPED:
            with self.subTest(name=name):
                self.write_env(f"{name}={FAKE_VALUE}\n")
                self.assertEqual(run_check(self.home, "on", {name: FAKE_VALUE})[1], [])

    def test_the_forms_the_agents_parser_reads(self):
        for line in (f"export ANTHROPIC_BASE_URL={FAKE_VALUE}", f'ANTHROPIC_BASE_URL="{FAKE_VALUE}"',
                     f"ANTHROPIC_BASE_URL='{FAKE_VALUE}'", f"  ANTHROPIC_BASE_URL = {FAKE_VALUE}",
                     f"ANTHROPIC_BASE_URL={FAKE_VALUE} # note"):
            with self.subTest(line=line):
                self.write_env(line + "\n")
                self.assertIn("ANTHROPIC_BASE_URL", run_check(self.home, "on")[1][0])

    def test_what_the_plugin_wouldnt_refuse_gets_no_warning(self):
        for text in ("ANTHROPIC_BASE_URL=\n", 'ANTHROPIC_BASE_URL=""\n', "ANTHROPIC_BASE_URL= # later\n",
                     f"# ANTHROPIC_BASE_URL={FAKE_VALUE}\n", "CLAUDE_CODE_USE_BEDROCK=0\n", "CLAUDE_CODE_USE_VERTEX=false\n",
                     "CLAUDE_CODE_USE_FOUNDRY=OFF\n", "CLAUDE_CODE_USE_BEDROCK=no\n", f"ANTHROPIC_TOKEN={FAKE_VALUE}\n",
                     f"OPENAI_API_KEY={FAKE_VALUE}\n"):
            with self.subTest(text=text):
                self.write_env(text)
                self.assertEqual(run_check(self.home, "on")[1], [])

    def test_env_wins_over_the_process_environment_as_in_hermes(self):
        self.write_env("ANTHROPIC_BASE_URL=\n")
        self.assertEqual(run_check(self.home, "on", {"ANTHROPIC_BASE_URL": FAKE_VALUE})[1], [])

    def test_a_windows_editors_encoding_is_read(self):
        self.write_env(f"ANTHROPIC_BASE_URL={FAKE_VALUE}\n", "utf-16")
        self.assertIn("ANTHROPIC_BASE_URL", run_check(self.home, "on")[1][0])

    def test_nothing_is_changed_or_deleted(self):
        text = f"# mine\nANTHROPIC_BASE_URL={FAKE_VALUE}\nOTHER=1\n"
        self.write_env(text)
        before = os.stat(self.home / ".env").st_mtime_ns
        run_check(self.home, "on")
        self.assertEqual((self.home / ".env").read_text(encoding="utf-8"), text)
        self.assertEqual(os.stat(self.home / ".env").st_mtime_ns, before)

    def test_a_symlink_is_not_followed(self):
        target = self.home / "elsewhere"
        target.write_text(f"ANTHROPIC_BASE_URL={FAKE_VALUE}\n", encoding="utf-8")
        os.symlink(target, self.home / ".env")
        self.assertEqual(run_check(self.home, "on")[1], [])

    def test_no_env_file_is_fine(self):
        self.assertEqual(run_check(self.home, "on"), (0, []))

    def test_only_when_the_switch_is_on(self):
        self.write_env(f"ANTHROPIC_BASE_URL={FAKE_VALUE}\n")
        self.assertEqual(run_check(self.home, "off")[1], [])


class SwitchOffStillSelected(Case):
    def write_config(self, text):
        (self.home / "config.yaml").write_text(text, encoding="utf-8")

    def test_warns_when_config_yaml_selects_the_provider(self):
        for text in (f"model:\n  default: sonnet\n  provider: {PROVIDER}\n", f'model:\n  provider: "{PROVIDER}"\n',
                     f"provider: {PROVIDER}\n", f"model:\n  provider: {PROVIDER}  # mine\n",
                     f"fallback_model:\n  - provider: {PROVIDER}\n    model: sonnet\n"):
            with self.subTest(text=text):
                self.write_config(text)
                code, lines = run_check(self.home, "off")
                self.assertEqual(code, 0)
                self.assertEqual(len(lines), 1)
                self.assertIn("claude_subscription is off", lines[0])

    def test_no_warning_otherwise(self):
        for text in ("model:\n  provider: custom\n", f"# provider: {PROVIDER}\n", f"model:\n  provider: {PROVIDER}-other\n",
                     f"note: provider {PROVIDER}\n", ""):
            with self.subTest(text=text):
                self.write_config(text)
                self.assertEqual(run_check(self.home, "off")[1], [])

    def test_no_config_yaml_is_fine(self):
        self.assertEqual(run_check(self.home, "off"), (0, []))

    def test_no_warning_with_the_switch_on(self):
        self.write_config(f"model:\n  provider: {PROVIDER}\n")
        self.assertEqual(run_check(self.home, "on")[1], [])

    def test_config_yaml_is_only_read(self):
        text = f"model:\n  provider: {PROVIDER}\n"
        self.write_config(text)
        run_check(self.home, "off")
        self.assertEqual((self.home / "config.yaml").read_text(encoding="utf-8"), text)


class OwnCopies(Case):
    def add_plugin(self, folder, name):
        path = self.home / "plugins" / folder
        path.mkdir()
        (path / "plugin.yaml").write_text(f"name: {name}\nkind: model-provider\n", encoding="utf-8")

    def test_warns_in_both_states_and_leaves_it(self):
        self.add_plugin("my-claude", PROVIDER)
        for state in ("on", "off"):
            with self.subTest(state=state):
                lines = run_check(self.home, state)[1]
                self.assertEqual(len(lines), 1)
                self.assertIn("plugins/my-claude is your own copy", lines[0])
        self.assertTrue((self.home / "plugins/my-claude/plugin.yaml").exists())

    def test_other_plugins_and_our_symlink_are_silent(self):
        self.add_plugin("spotify", "spotify")
        (self.home / "plugins/empty").mkdir()
        pinned = self.home / "pinned"
        pinned.mkdir()
        (pinned / "plugin.yaml").write_text(f"name: {PROVIDER}\n", encoding="utf-8")
        os.symlink(pinned, self.home / LINK)
        self.assertEqual(run_check(self.home, "on")[1], [])


class InitBlock(Case):
    """Runs the claude-provider block of hermes-init.sh in bash, with stubs for what the script defines."""

    SEEN = "printf '%s\\n' \"$@\" > \"$FAKE_DIR/arguments\"; env > \"$FAKE_DIR/environment\"; "
    PREAMBLE = r"""
set -euo pipefail
as_hermes() { "$@"; }
log_debug() { printf 'DEBUG %s\n' "$*"; }
log_info() { printf 'INFO %s\n' "$*"; }
log_warning() { printf 'WARNING %s\n' "$*"; }
opt() { printf '%s\n' "$CLAUDE_SWITCH"; }
env_sync() {
  local output line
  output="$("$REAL_PYTHON" -I "$REAL_ENV_SYNC" "$@" 2>&1)" || return 1
  while IFS= read -r line; do
    case "$line" in
      "") ;;
      WARNING:*) log_warning "${line#WARNING: }" ;;
      *) log_info "$line" ;;
    esac
  done <<<"$output"
}
"""

    def block(self, seconds=None):
        text = INIT.split("# BEGIN claude-provider")[1].split("# END claude-provider")[0]
        if seconds is not None:
            text, count = re.subn(r"claude_status_seconds=\d+", f"claude_status_seconds={seconds}", text)
            self.assertEqual(count, 1)
        return text

    def fake_claude(self, body):
        self.app = self.home.parent / "app"
        self.fake_dir = self.home.parent / "fake"
        (self.app / "bin").mkdir(parents=True)
        self.fake_dir.mkdir()
        script = self.app / "bin/claude"
        script.write_text(f'#!/bin/sh\nFAKE_DIR="{self.fake_dir}"\n{self.SEEN}{body}\n', encoding="utf-8")
        os.chmod(script, 0o755)

    def run_block(self, switch, seconds=None, environment=None):
        script = self.PREAMBLE + f'HOME_DIR="{self.home}"\nAPP_DIR="{self.app}"\n' + self.block(seconds)
        with_env = {**clean_environment(), "CLAUDE_SWITCH": switch, "REAL_PYTHON": sys.executable,
                    "REAL_ENV_SYNC": str(LIB / "env_sync.py"), **(environment or {})}
        return subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False, env=with_env, timeout=60)

    def test_on_links_the_pinned_copy_and_reports_yes(self):
        self.fake_claude("exit 0")
        result = self.run_block("true")
        self.assertEqual(result.returncode, 0, result.stderr)
        link = self.home / LINK
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "/opt/hermes-plugins/claude-subscription-directsdk")
        self.assertIn("INFO Claude subscription provider: on", result.stdout)
        self.assertIn("INFO Claude CLI logged in: yes", result.stdout)

    def test_the_login_check_is_the_exit_code_of_auth_status(self):
        self.fake_claude("exit 1")
        result = self.run_block("true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INFO Claude CLI logged in: no", result.stdout)
        self.assertNotIn("logged in: yes", result.stdout)
        self.assertEqual((self.fake_dir / "arguments").read_text().split(), ["auth", "status"])

    def test_a_credentials_file_alone_doesnt_count(self):
        self.fake_claude("exit 1")
        (self.home.parent / ".credentials.json").write_text("{}")
        self.assertIn("logged in: no", self.run_block("true").stdout)

    def test_the_commands_output_reaches_no_log(self):
        self.fake_claude('echo \'{"email":"someone@example.invalid","orgName":"Some Org"}\'; echo token-text >&2; exit 0')
        result = self.run_block("true")
        for leak in ("someone@example.invalid", "Some Org", "token-text"):
            self.assertNotIn(leak, result.stdout + result.stderr)
        self.assertIn("logged in: yes", result.stdout)

    def test_a_failure_counts_as_no_and_doesnt_stop_the_start(self):
        for body in ("exit 2", "exit 127", "kill -9 $$"):
            with self.subTest(body=body):
                self.setUp()
                self.fake_claude(body)
                result = self.run_block("true")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("logged in: no", result.stdout)

    def test_a_missing_command_counts_as_no(self):
        self.fake_claude("exit 0")
        (self.app / "bin/claude").unlink()
        result = self.run_block("true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("logged in: no", result.stdout)

    def test_a_hang_is_bounded_and_counts_as_no(self):
        self.fake_claude("exec sleep 600")
        started = time.monotonic()
        result = self.run_block("true", seconds=1)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING The Claude CLI didn't answer the login check within 1 s", result.stdout)
        self.assertIn("logged in: no", result.stdout)

    def test_a_signal_ignoring_hang_is_killed_too(self):
        self.fake_claude("trap '' TERM; while :; do sleep 1; done")
        started = time.monotonic()
        result = self.run_block("true", seconds=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("logged in: no", result.stdout)

    def test_the_timeout_in_the_block_is_short(self):
        seconds = int(re.search(r"claude_status_seconds=(\d+)", self.block()).group(1))
        self.assertLessEqual(seconds, 15)

    def test_no_inherited_variable_reaches_the_cli(self):
        self.fake_claude("exit 0")
        self.run_block("true", environment={"SUPERVISOR_TOKEN": FAKE_VALUE, "HASSIO_TOKEN": FAKE_VALUE,
                                            "ANTHROPIC_API_KEY": FAKE_VALUE, "CLAUDE_CODE_OAUTH_TOKEN": FAKE_VALUE})
        seen = (self.fake_dir / "environment").read_text()
        self.assertNotIn(FAKE_VALUE, seen)
        names = {line.split("=", 1)[0] for line in seen.splitlines()}
        self.assertLessEqual({"USER", "LOGNAME", "HOME", "PATH", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"}, names)
        self.assertIn("USER=hermes", seen)

    def test_on_names_the_conflicts_after_the_switch_line(self):
        self.fake_claude("exit 1")
        self.write_env(f"ANTHROPIC_BASE_URL={FAKE_VALUE}\n")
        result = self.run_block("true")
        self.assertRegex(result.stdout, r"provider: on\nWARNING .*ANTHROPIC_BASE_URL \(\.env\)")
        self.assertNotIn(FAKE_VALUE, result.stdout + result.stderr)
        self.assertIn(FAKE_VALUE, (self.home / ".env").read_text())

    def test_a_stale_link_is_repointed(self):
        self.fake_claude("exit 1")
        os.symlink("/opt/old-location", self.home / LINK)
        self.run_block("true")
        self.assertEqual(os.readlink(self.home / LINK), "/opt/hermes-plugins/claude-subscription-directsdk")

    def test_starting_twice_changes_nothing(self):
        self.fake_claude("exit 1")
        self.run_block("true")
        self.run_block("true")
        self.assertEqual(sorted(path.name for path in (self.home / "plugins").iterdir()), ["claude-subscription-directsdk"])

    def test_off_removes_the_link_and_never_runs_the_cli(self):
        self.fake_claude("exit 0")
        self.run_block("true")
        (self.fake_dir / "arguments").unlink()
        result = self.run_block("false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.lexists(self.home / LINK))
        self.assertIn("INFO Claude subscription provider: off", result.stdout)
        self.assertNotIn("logged in", result.stdout)
        self.assertFalse((self.fake_dir / "arguments").exists())

    def test_off_with_nothing_there(self):
        self.fake_claude("exit 0")
        result = self.run_block("false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.lexists(self.home / LINK))

    def test_off_warns_when_the_provider_is_still_selected(self):
        self.fake_claude("exit 0")
        (self.home / "config.yaml").write_text(f"model:\n  provider: {PROVIDER}\n", encoding="utf-8")
        self.assertRegex(self.run_block("false").stdout, r"provider: off\nWARNING config\.yaml still selects")

    def test_a_missing_or_odd_option_means_off(self):
        self.fake_claude("exit 0")
        for value in ("null", "", "yes"):
            with self.subTest(value=value):
                self.assertIn("provider: off", self.run_block(value).stdout)

    def test_a_real_folder_with_the_links_name_stays_in_both_states(self):
        self.fake_claude("exit 1")
        folder = self.home / LINK
        folder.mkdir()
        (folder / "plugin.yaml").write_text(f"name: {PROVIDER}\n", encoding="utf-8")
        for switch in ("true", "false"):
            with self.subTest(switch=switch):
                result = self.run_block(switch)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(folder.is_symlink())
                self.assertEqual(sorted(path.name for path in folder.iterdir()), ["plugin.yaml"])
                self.assertIn("is your own copy", result.stdout)

    def test_a_failing_check_doesnt_stop_the_start(self):
        self.fake_claude("exit 1")
        script = self.PREAMBLE + 'env_sync() { return 1; }\n' + f'HOME_DIR="{self.home}"\nAPP_DIR="{self.app}"\n' + self.block()
        result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False,
                                env={**clean_environment(), "CLAUDE_SWITCH": "true"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("logged in: no", result.stdout)


class Wrapper(unittest.TestCase):
    TEXT = WRAPPER.read_text(encoding="utf-8")

    def run_wrapper(self, *arguments, environment=None):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Path(directory, "recorder")
            recorder.write_text('#!/bin/sh\nenv\necho "arguments:$*"\n')
            os.chmod(recorder, 0o755)
            copy = Path(directory, "wrapper")
            copy.write_text(re.sub(r"exec /opt/claude-code/\S+", f"exec {recorder}", self.TEXT))
            os.chmod(copy, 0o755)
            env = {key: value for key, value in os.environ.items() if key not in ("USER", "LOGNAME", "CLAUDE_CONFIG_DIR")}
            return subprocess.run([str(copy), *arguments], capture_output=True, text=True, check=False,
                                  env={**env, **(environment or {})}).stdout.splitlines()

    def test_sets_what_the_terminals_allowlist_drops(self):
        lines = self.run_wrapper("auth", "status")
        for expected in ("CLAUDE_CONFIG_DIR=/data/claude", "DISABLE_AUTOUPDATER=1", "DISABLE_UPDATES=1",
                         "USER=hermes", "LOGNAME=hermes", "arguments:auth status"):
            self.assertIn(expected, lines)

    def test_a_different_login_folder_cant_be_passed_through(self):
        self.assertIn("CLAUDE_CONFIG_DIR=/data/claude", self.run_wrapper(environment={"CLAUDE_CONFIG_DIR": "/config/hermes/x"}))

    def test_keeps_an_existing_identity(self):
        self.assertIn("USER=someone", self.run_wrapper(environment={"USER": "someone"}))

    def test_runs_the_unmodified_binary_and_nothing_else(self):
        code = [line for line in self.TEXT.splitlines() if line.strip() and not line.startswith("#")]
        self.assertEqual(code[-1], 'exec /opt/claude-code/node_modules/.bin/claude "$@"')
        for line in code[:-1]:
            self.assertTrue(line.startswith("export "), line)

    def test_is_on_path_before_everything_else(self):
        dockerfile = (APP / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("PATH=/opt/hermes-app/bin:", dockerfile)


class TokenPaths(unittest.TestCase):
    def test_no_option_can_carry_a_claude_credential(self):
        self.assertEqual(set(MANIFEST["schema"]), {"enable_terminal", "claude_subscription", "homeassistant_token", "env_vars", "log_level"})
        self.assertIs(MANIFEST["options"]["claude_subscription"], False)
        self.assertEqual(MANIFEST["schema"]["claude_subscription"], "bool")

    def test_the_oauth_token_is_refused_in_env_vars(self):
        errors = env_sync.validate([{"name": "CLAUDE_CODE_OAUTH_TOKEN", "value": FAKE_VALUE}])
        self.assertEqual(len(errors), 1)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", errors[0])
        self.assertNotIn(FAKE_VALUE, errors[0])

    def test_conflict_names_stay_available_to_the_user(self):
        # The native Anthropic provider needs its key, so these warn instead of being refused.
        self.assertEqual(env_sync.validate([{"name": name, "value": "x"} for name in CONFLICTS + DROPPED]), [])

    def test_nothing_in_the_image_touches_the_credentials_file(self):
        offenders = [path for path in ROOTFS.rglob("*") if path.is_file() and ".credentials" in path.read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(offenders, [])

    def test_the_login_is_not_seeded_or_listed_for_a_copy(self):
        code = "\n".join(line for line in INIT.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"(cp|mv|install|tee)\s[^\n]*/data/claude/")
        self.assertIn("install -d -o hermes -g hermes -m 0700", code)
        self.assertIn("/data/claude", code)


class FallbackAndManagedKeys(unittest.TestCase):
    def test_external_logins_are_off_in_both_switch_states(self):
        self.assertIs(MANAGED["auth"]["adopt_external_logins"], False)
        self.assertNotIn("adopt_external_logins", INIT)

    def test_no_fallback_or_model_is_configured_for_the_user(self):
        text = (APP / "managed-config.yaml").read_text(encoding="utf-8")
        for key in ("fallback", "model", "provider"):
            self.assertNotRegex(text, rf"(?m)^\s*{key}\b")
        self.assertNotIn("fallback", INIT)
        self.assertNotIn("model.default", INIT)


class Documentation(unittest.TestCase):
    SECTION = DOCS.split("## Claude subscription")[1].split("\n## ")[0]

    PHRASES = {
        "experimental": "experimental",
        "one process per request": "one `claude` process",
        "the fixed caller name": "`CLAUDE_CODE_ENTRYPOINT=hermes-agent`",
        "the local relay": "local relay",
        "the header in memory": "in memory",
        "account risk": "risk for your account",
        "own decision": "your own conscious decision",
        "not endorsed": "doesn't endorse",
        "telemetry": "`claude_code_telemetry`",
        "login command": "`claude auth login`",
        "logout command": "`claude auth logout`",
        "the wizard's skip": "Skip setup",
        "the provider's name": "Claude Subscription DirectSDK (Experimental)",
        "shared limits": "shared with claude.ai",
        "the windows": "5-hour",
        "usage credits": "usage credits",
        "the extra usage advice": "turn off extra usage",
        "fallback": "`fallback_model`",
        "credentials in backups": "`.credentials.json`",
        "encrypt": "encrypt",
        "unsupported token": "`CLAUDE_CODE_OAUTH_TOKEN`",
        "unsupported setup-token": "`claude setup-token`",
        "plan limit": "429",
        "not logged in": "401",
        "conflicts": "`ANTHROPIC_BASE_URL`",
        "the card": "Login with Claude Code",
    }
    LINKS = ("https://code.claude.com/docs/en/legal-and-compliance", "https://code.claude.com/docs/en/agent-sdk/overview",
             "https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan",
             "https://www.anthropic.com/legal/consumer-terms", "https://www.anthropic.com/legal/commercial-terms")

    def test_the_placeholder_is_gone(self):
        self.assertNotIn("Placeholder", DOCS)

    def test_the_section_says_what_it_must(self):
        for label, phrase in self.PHRASES.items():
            with self.subTest(label=label):
                self.assertIn(phrase, self.SECTION)

    def test_every_policy_page_is_linked(self):
        for link in self.LINKS:
            with self.subTest(link=link):
                self.assertIn(link, self.SECTION)

    def test_no_logo_and_no_look_of_endorsement(self):
        self.assertNotRegex(self.SECTION, r"!\[")
        for phrase in ("official Anthropic", "by Anthropic for", "powered by", "Powered by", "partner"):
            self.assertNotIn(phrase, self.SECTION)

    def test_the_option_says_the_same_in_short(self):
        option = TRANSLATIONS["configuration"]["claude_subscription"]
        description = " ".join(option["description"].split())
        for phrase in ("experimental", "caller name", "relay", "risk", "your own decision", "not made or endorsed by Anthropic"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, description)
        self.assertLess(len(description), 600)
        self.assertNotRegex(option["name"], r"Claude Code|Anthropic")


if __name__ == "__main__":
    unittest.main()
