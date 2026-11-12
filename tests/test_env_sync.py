"""env_sync.py, as hermes-init calls it: validation, reserved names, and in-place updates of .env."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hermes_webui" / "rootfs" / "opt" / "hermes-app" / "lib" / "env_sync.py"
SPEC = importlib.util.spec_from_file_location("env_sync", SCRIPT)
env_sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(env_sync)

# CLAUDE.md section 2.6. Wildcards are listed with a sample name.
RESERVED_IN_CLAUDE_MD = [
    "HASS_TOKEN", "HASS_URL", "SUPERVISOR_TOKEN", "HASSIO_TOKEN", "HERMES_HOME", "HERMES_WEBUI_PASSWORD",
    "HERMES_WEBUI_ANYTHING", "HERMES_WRITE_SAFE_ROOT", "HERMES_RUNTIME_DIR", "HERMES_BUNDLED_PLUGINS",
    "HERMES_DISABLE_LAZY_INSTALLS", "HERMES_MANAGED", "HERMES_MANAGED_DIR", "HERMES_PYTHON", "HERMES_DASHBOARD",
    "HERMES_DASHBOARD_SESSION_TOKEN", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND", "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR", "DISABLE_AUTOUPDATER",
    "DISABLE_UPDATES", "XDG_CACHE_HOME", "UV_CACHE_DIR", "PIP_CACHE_DIR", "NPM_CONFIG_CACHE", "PATH", "HOME",
]
SECRET = "fake-value-must-not-appear-in-any-output"
MANAGED_ENV = ROOT / "hermes_webui" / "rootfs" / "etc" / "hermes" / ".env"
# The gateway's names (docs/architecture.md section 15), with a sample for each prefix.
RESERVED_FOR_THE_GATEWAY = [
    "HERMES_S6_SUPERVISED_CHILD", "HERMES_SUPERVISED_CHILD", "HERMES_GATEWAY_LOCK_DIR", "HERMES_GATEWAY_EXTERNAL_SUPERVISOR",
    "HERMES_ALLOW_ROOT_GATEWAY", "GATEWAY_MULTIPLEX_PROFILES", "GATEWAY_RELAY_URL", "GATEWAY_RELAY_SECRET",
    "GATEWAY_ON_ALL_ADAPTERS_DOWN", "HASS_HOME_CHANNEL", "API_SERVER_KEY", "API_SERVER_HOST", "WEBHOOK_ENABLED",
    "WEBHOOK_PORT", "WEBHOOK_SECRET", "MSGRAPH_WEBHOOK_ENABLED", "MSGRAPH_WEBHOOK_PORT", "TELEGRAM_WEBHOOK_URL",
    "TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_WEBHOOK_PORT", "TELEGRAM_WEBHOOK_HOST",
]


class ReservedNames(unittest.TestCase):
    def test_everything_claude_md_lists(self):
        for name in RESERVED_IN_CLAUDE_MD:
            with self.subTest(name=name):
                self.assertTrue(env_sync.is_reserved(name))

    def test_further_names_found_necessary(self):
        for name in ("XDG_RUNTIME_DIR", "HERMES_TUI_DIR", "HERMES_WEB_DIST", "HERMES_CONFIG", "HERMES_CONFIG_PATH",
                     "HERMES_ENV_PATH", "HERMES_HOME_OVERRIDE", "HERMES_IGNORE_USER_CONFIG", "npm_config_cache"):
            with self.subTest(name=name):
                self.assertTrue(env_sync.is_reserved(name))

    def test_the_gateways_names(self):
        for name in RESERVED_FOR_THE_GATEWAY:
            with self.subTest(name=name):
                self.assertTrue(env_sync.is_reserved(name))

    def test_every_name_the_managed_env_pins_is_reserved(self):
        # Otherwise the option would take a name and the managed layer would silently overrule it.
        names = env_sync.managed_env_names(MANAGED_ENV)
        self.assertGreaterEqual(len(names), 7)
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(env_sync.is_reserved(name))

    def test_ordinary_names_are_free(self):
        for name in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HERMES_CUSTOM_API_KEY", "TZ",
                     "MY_HERMES_HOME", "HOMEBREW", "PATHEXT", "HASS_SOMETHING_ELSE", "TELEGRAM_BOT_TOKEN",
                     "TELEGRAM_ALLOWED_USERS", "DISCORD_BOT_TOKEN", "GATEWAY_ALLOWED_USERS", "HERMES_YOLO_MODE"):
            with self.subTest(name=name):
                self.assertFalse(env_sync.is_reserved(name))


class Validation(unittest.TestCase):
    def errors(self, *entries):
        return env_sync.validate(list(entries))

    def test_good_entries(self):
        self.assertEqual(self.errors({"name": "OPENROUTER_API_KEY", "value": "sk-1"}, {"name": "_X", "value": ""}), [])

    def test_bad_names_are_refused_by_position_without_echoing_them(self):
        for name in ("lowercase", "1LEADING", "WITH-DASH", "WITH SPACE", "", None, 5, "SK-ABC123"):
            with self.subTest(name=name):
                errors = self.errors({"name": name, "value": "x"})
                self.assertEqual(len(errors), 1)
                self.assertIn("entry 1", errors[0])
                if isinstance(name, str) and name:
                    self.assertNotIn(name, errors[0])

    def test_reserved_names_are_refused_by_name(self):
        for name in RESERVED_IN_CLAUDE_MD:
            with self.subTest(name=name):
                errors = self.errors({"name": name, "value": SECRET})
                self.assertEqual(len(errors), 1)
                self.assertIn(f"'{name}' is reserved", errors[0])
                self.assertNotIn(SECRET, errors[0])

    def test_a_name_with_a_trailing_line_break_is_refused(self):
        for name in ("HASS_TOKEN\n", "OPENROUTER_API_KEY\n"):
            with self.subTest(name=name):
                self.assertEqual(len(env_sync.validate([{"name": name, "value": "x"}])), 1)

    def test_values_must_be_single_line_strings(self):
        for value in ("a\nb", "a\rb", "a\0b", None, 5, ["x"]):
            with self.subTest(value=value):
                self.assertEqual(len(self.errors({"name": "OK_NAME", "value": value})), 1)
        self.assertNotIn("a\nb", self.errors({"name": "OK_NAME", "value": "a\nb"})[0])


class Merge(unittest.TestCase):
    def test_upsert_in_place_keeps_everything_else(self):
        text = "# comment\nFIRST=1\n\nexport TARGET=old\nLAST=9\n"
        self.assertEqual(env_sync.merge(text, {"TARGET": "new"}, set()), "# comment\nFIRST=1\n\nTARGET=new\nLAST=9\n")

    def test_new_key_is_appended(self):
        self.assertEqual(env_sync.merge("A=1\n", {"B": "2"}, set()), "A=1\nB=2\n")

    def test_missing_final_newline_does_not_join_lines(self):
        self.assertEqual(env_sync.merge("A=1", {"B": "2"}, set()), "A=1\nB=2\n")
        self.assertEqual(env_sync.merge("A=1", {}, set()), "A=1")

    def test_duplicates_collapse_to_one(self):
        self.assertEqual(env_sync.merge("A=1\nA=2\nB=3\n", {"A": "x"}, set()), "A=x\nB=3\n")

    def test_names_that_left_the_option_are_removed(self):
        self.assertEqual(env_sync.merge("KEEP=1\nOLD=2\nexport OLD2=3\n", {}, {"OLD", "OLD2"}), "KEEP=1\n")

    def test_hand_added_keys_stay(self):
        self.assertEqual(env_sync.merge("HAND=1\n", {"MINE": "2"}, {"OTHER"}), "HAND=1\nMINE=2\n")

    def test_commented_keys_are_not_touched(self):
        self.assertEqual(env_sync.merge("# A=old\n", {"A": "new"}, {"B"}), "# A=old\nA=new\n")

    def test_line_endings_are_kept(self):
        self.assertEqual(env_sync.merge("A=1\r\nB=2\r\n", {"A": "x"}, set()), "A=x\r\nB=2\r\n")

    def test_nothing_changes_when_nothing_is_wanted(self):
        text = "# c\nA=1\n\nB=2\n"
        self.assertEqual(env_sync.merge(text, {}, set()), text)


class Formatting(unittest.TestCase):
    def test_plain_values_stay_unquoted(self):
        self.assertEqual(env_sync.format_line("A", "sk-abc_123/x.y:z@h+="), "A=sk-abc_123/x.y:z@h+=\n")
        self.assertEqual(env_sync.format_line("A", ""), "A=\n")

    def test_other_values_are_quoted_and_escaped(self):
        self.assertEqual(env_sync.format_line("A", 'a b"c\\d'), 'A="a b\\"c\\\\d"\n')
        self.assertEqual(env_sync.format_line("A", "x #y"), 'A="x #y"\n')
        self.assertEqual(env_sync.format_line("A", "$HOME"), 'A="$HOME"\n')


class Apply(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env = self.root / "hermes" / ".env"
        self.env.parent.mkdir()
        self.options = self.root / "options.json"
        self.marker = self.root / "env_vars_names.json"

    def apply(self, entries, managed=None):
        self.options.write_text(json.dumps({"env_vars": entries}), encoding="utf-8")
        output = io.StringIO()
        extra = ["--managed-env", str(managed or self.root / "no-managed-env")]
        with contextlib.redirect_stdout(output):
            status = env_sync.main(["apply", "--options", str(self.options), "--env", str(self.env), "--marker", str(self.marker), *extra])
        return status, output.getvalue()

    def test_a_name_the_managed_layer_pins_is_named_as_without_effect(self):
        self.env.write_text(f"WEBHOOK_ENABLED={SECRET}\nHASS_TOKEN={SECRET}\n", encoding="utf-8")
        _status, output = self.apply([], managed=MANAGED_ENV)
        self.assertIn("reserved name WEBHOOK_ENABLED, which has no effect", output)
        self.assertIn("reserved name HASS_TOKEN, which overrides this app's value", output)
        self.assertNotIn(SECRET, output)

    def test_a_listener_setting_is_named_as_without_effect(self):
        names = ("WEBHOOK_PORT", "API_SERVER_HOST", "TELEGRAM_WEBHOOK_HOST", "MSGRAPH_WEBHOOK_PORT")
        self.env.write_text("".join(f"{name}={SECRET}\n" for name in names) + f"A2A_HOST={SECRET}\n", encoding="utf-8")
        _status, output = self.apply([], managed=MANAGED_ENV)
        for name in names:
            with self.subTest(name=name):
                self.assertIn(f"reserved name {name}, which has no effect: this app keeps that listener off", output)
        self.assertIn("reserved name A2A_HOST, which has no effect: this app's own value wins", output)
        self.assertNotIn("overrides this app's value", output)
        self.assertNotIn(SECRET, output)

    def test_open_access_settings_warn_by_name_only(self):
        self.env.write_text(
            f"TELEGRAM_ALLOW_ALL_USERS=true\nGATEWAY_ALLOW_ALL_USERS=yes\nDISCORD_ALLOWED_USERS=12,*\nHERMES_YOLO_MODE=1\n"
            f"SLACK_ALLOW_ALL_USERS=false\nSIGNAL_ALLOWED_USERS=+491701234567\nMATRIX_ALLOW_ALL_USERS=\n"
            f"TELEGRAM_BOT_TOKEN={SECRET}\n", encoding="utf-8")
        status, output = self.apply([])
        self.assertEqual(status, 0)
        for name in ("TELEGRAM_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS", "DISCORD_ALLOWED_USERS"):
            self.assertIn(f".env sets {name}, which lets anyone", output)
        self.assertIn(".env sets HERMES_YOLO_MODE, so the agent runs risky commands", output)
        for name in ("SLACK_ALLOW_ALL_USERS", "SIGNAL_ALLOWED_USERS", "MATRIX_ALLOW_ALL_USERS", "TELEGRAM_BOT_TOKEN"):
            self.assertNotIn(name, output)
        self.assertNotIn(SECRET, output)
        self.assertNotIn("+49170", output)

    def test_allow_all_users_in_config_yaml_warns(self):
        self.env.write_text("", encoding="utf-8")
        (self.env.parent / "config.yaml").write_text("model:\n  provider: x\nallow_all_users: true\n", encoding="utf-8")
        _status, output = self.apply([])
        self.assertIn("config.yaml sets allow_all_users", output)
        (self.env.parent / "config.yaml").write_text("allow_all_users: false\n", encoding="utf-8")
        _status, output = self.apply([])
        self.assertNotIn("allow_all_users", output)

    def test_values_never_reach_the_output_and_the_marker_holds_names(self):
        self.env.write_text("", encoding="utf-8")
        status, output = self.apply([{"name": "KEY_A", "value": SECRET}])
        self.assertEqual(status, 0)
        self.assertNotIn(SECRET, output)
        self.assertIn(f"KEY_A={SECRET}", self.env.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8")), ["KEY_A"])
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)

    def test_ownership_across_runs(self):
        self.env.write_text("HAND=keep\n", encoding="utf-8")
        self.apply([{"name": "KEY_A", "value": "1"}, {"name": "KEY_B", "value": "2"}])
        self.apply([{"name": "KEY_B", "value": "3"}])
        self.assertEqual(self.env.read_text(encoding="utf-8"), "HAND=keep\nKEY_B=3\n")
        self.apply([])
        self.assertEqual(self.env.read_text(encoding="utf-8"), "HAND=keep\n")
        self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8")), [])

    def test_a_reserved_name_in_env_warns_by_name_and_stays(self):
        self.env.write_text(f"HERMES_HOME=/x/{SECRET}\nexport HASS_TOKEN={SECRET}\nFINE=1\n", encoding="utf-8")
        status, output = self.apply([])
        self.assertEqual(status, 0)
        self.assertIn("reserved name HERMES_HOME", output)
        self.assertIn("reserved name HASS_TOKEN", output)
        self.assertNotIn("FINE", output)
        self.assertNotIn(SECRET, output)
        self.assertIn(f"HASS_TOKEN={SECRET}", self.env.read_text(encoding="utf-8"))

    def test_a_refused_option_changes_nothing(self):
        self.env.write_text("HAND=keep\n", encoding="utf-8")
        status, output = self.apply([{"name": "HASS_TOKEN", "value": SECRET}])
        self.assertEqual(status, 1)
        self.assertNotIn(SECRET, output)
        self.assertEqual(self.env.read_text(encoding="utf-8"), "HAND=keep\n")
        self.assertFalse(self.marker.exists())

    def test_a_symlink_is_never_followed(self):
        target = self.root / "root-only"
        target.write_text("SECRET_IN_TARGET=1\n", encoding="utf-8")
        self.env.symlink_to(target)
        status, output = self.apply([{"name": "KEY_A", "value": "1"}])
        self.assertEqual(status, 0)
        self.assertIn("not a regular file", output)
        self.assertTrue(self.env.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "SECRET_IN_TARGET=1\n")
        self.assertFalse(self.marker.exists())

    def test_a_folder_named_env_only_warns(self):
        self.env.mkdir()
        status, output = self.apply([{"name": "KEY_A", "value": "1"}])
        self.assertEqual(status, 0)
        self.assertIn("not a regular file", output)
        self.assertTrue(self.env.is_dir())
        self.assertFalse(self.marker.exists())

    def test_a_marker_folder_only_warns(self):
        self.env.write_text("A=1\n", encoding="utf-8")
        self.marker.mkdir()
        status, output = self.apply([])
        self.assertEqual(status, 0)
        self.assertIn("marker can't be saved", output)
        self.assertTrue(self.marker.is_dir())
        self.assertEqual(self.env.read_text(encoding="utf-8"), "A=1\n")

    def test_a_bad_marker_only_warns(self):
        self.env.write_text("A=1\n", encoding="utf-8")
        self.marker.write_text("not json", encoding="utf-8")
        status, output = self.apply([])
        self.assertEqual(status, 0)
        self.assertIn("marker can't be read", output)
        self.assertEqual(self.env.read_text(encoding="utf-8"), "A=1\n")

    def test_files_from_windows_editors(self):
        self.env.write_bytes(b"\xef\xbb\xbfFIRST=1\r\nOTHER=2\r\n")
        self.apply([{"name": "FIRST", "value": "x"}])
        self.assertEqual(self.env.read_bytes(), b"FIRST=x\r\nOTHER=2\r\n")
        self.env.write_bytes("A=1\r\n".encode("utf-16"))
        self.apply([{"name": "B", "value": "2"}])
        self.assertEqual(self.env.read_bytes(), b"A=1\r\nB=2\n")

    def test_bytes_that_are_not_utf8_survive(self):
        self.env.write_bytes(b"LATIN=caf\xe9\n")
        status, _output = self.apply([{"name": "B", "value": "2"}])
        self.assertEqual(status, 0)
        self.assertEqual(self.env.read_bytes(), b"LATIN=caf\xe9\nB=2\n")

    def test_an_unchanged_file_is_not_rewritten(self):
        self.env.write_text("A=1\n", encoding="utf-8")
        os.chmod(self.env, 0o600)
        before = self.env.stat().st_ino
        self.apply([])
        self.assertEqual(self.env.stat().st_ino, before)

    def test_a_wrong_mode_is_corrected(self):
        self.env.write_text("A=1\n", encoding="utf-8")
        os.chmod(self.env, 0o644)
        self.apply([])
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)


class CommandLine(unittest.TestCase):
    def test_check_exit_codes_with_the_standard_library_only(self):
        with tempfile.TemporaryDirectory() as directory:
            options = Path(directory, "options.json")
            options.write_text(json.dumps({"env_vars": [{"name": "OK_NAME", "value": "1"}]}), encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", str(SCRIPT), "check", "--options", str(options)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            options.write_text(json.dumps({"env_vars": [{"name": "HOME", "value": SECRET}]}), encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", str(SCRIPT), "check", "--options", str(options)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("ERROR: env_vars: 'HOME' is reserved", result.stdout)
            self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_a_broken_options_file_is_reported_without_its_content(self):
        with tempfile.TemporaryDirectory() as directory:
            options = Path(directory, "options.json")
            options.write_text(SECRET, encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", str(SCRIPT), "check", "--options", str(options)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(SECRET, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
