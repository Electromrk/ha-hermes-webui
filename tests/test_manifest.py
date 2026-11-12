"""The app manifest against CLAUDE.md section 2.6 and the Supervisor's configuration reference."""

import importlib.util
import os
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "hermes_webui"
TEXT = (APP / "config.yaml").read_text(encoding="utf-8")
MANIFEST = yaml.safe_load(TEXT)
TRANSLATIONS = yaml.safe_load((APP / "translations" / "en.yaml").read_text(encoding="utf-8"))
DOCS = (APP / "DOCS.md").read_text(encoding="utf-8")

# https://contributors.home-assistant.io/apps/reference/configuration/, read 2026-11-06.
# The Supervisor ignores unknown keys silently, so a typo here would simply do nothing.
REFERENCE_KEYS = {
    "name", "version", "slug", "description", "arch", "url", "machine", "homeassistant", "startup", "boot", "stage",
    "init", "timeout", "breaking_versions", "environment", "legacy", "stdin", "tmpfs", "image", "options", "schema",
    "ports", "ports_description", "host_network", "webui", "watchdog", "map", "ingress", "ingress_port",
    "ingress_entry", "ingress_stream", "panel_icon", "panel_title", "panel_admin", "devices", "uart", "usb", "gpio",
    "udev", "devicetree", "video", "audio", "kernel_modules", "full_access", "privileged", "apparmor", "host_dbus",
    "host_pid", "host_uts", "host_ipc", "realtime", "journald", "ulimits", "homeassistant_api", "hassio_api",
    "hassio_role", "auth_api", "docker_api", "services", "discovery", "backup", "backup_pre", "backup_post",
    "backup_exclude",
}


class Identity(unittest.TestCase):
    def test_every_key_is_in_the_reference(self):
        self.assertEqual(set(MANIFEST) - REFERENCE_KEYS, set())

    def test_identity_and_runtime(self):
        self.assertEqual(MANIFEST["name"], "Hermes Agent (WebUI)")
        self.assertEqual(MANIFEST["slug"], "hermes_webui")
        self.assertRegex(MANIFEST["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(MANIFEST["arch"], ["amd64", "aarch64"])
        self.assertIs(MANIFEST["init"], False)
        self.assertEqual(MANIFEST["startup"], "application")
        self.assertEqual(MANIFEST["boot"], "auto")
        self.assertEqual(MANIFEST["timeout"], 60)

    def test_every_device_builds_the_app_itself(self):
        self.assertNotIn("image", MANIFEST)
        self.assertNotIn("ghcr.io", TEXT)


class IngressAndAccess(unittest.TestCase):
    def test_ingress_and_panel(self):
        self.assertIs(MANIFEST["ingress"], True)
        self.assertEqual(MANIFEST["ingress_port"], 8099)
        self.assertIs(MANIFEST["ingress_stream"], True)
        self.assertIs(MANIFEST["panel_admin"], True)
        self.assertTrue(MANIFEST["panel_icon"].startswith("mdi:"))
        self.assertEqual(MANIFEST["panel_title"], "Hermes Agent")

    def test_watchdog_points_at_nginx_health(self):
        self.assertEqual(MANIFEST["watchdog"], f"http://[HOST]:[PORT:{MANIFEST['ingress_port']}]/health")

    def test_no_host_ports_or_extra_rights(self):
        for key in ("ports", "host_network", "webui", "hassio_api", "auth_api", "docker_api", "full_access",
                    "privileged", "host_pid", "apparmor"):
            self.assertNotIn(key, MANIFEST)

    def test_home_assistant_and_storage(self):
        self.assertIs(MANIFEST["homeassistant_api"], True)
        self.assertEqual(MANIFEST["map"], [{"type": "app_config", "read_only": False}])

    def test_nginx_listens_on_the_ingress_port(self):
        nginx = (APP / "rootfs/opt/hermes-app/nginx/nginx.conf").read_text(encoding="utf-8")
        self.assertIn(f"listen {MANIFEST['ingress_port']} default_server;", nginx)


class Options(unittest.TestCase):
    NAMES = {"enable_terminal", "claude_subscription", "homeassistant_token", "env_vars", "log_level"}

    def test_defaults(self):
        self.assertEqual(MANIFEST["options"], {"enable_terminal": False, "claude_subscription": False, "env_vars": [],
                                               "log_level": "info"})

    def test_schema(self):
        schema = MANIFEST["schema"]
        self.assertEqual(set(schema), self.NAMES)
        self.assertEqual(schema["enable_terminal"], "bool")
        self.assertEqual(schema["claude_subscription"], "bool")
        self.assertEqual(schema["homeassistant_token"], "password?")
        self.assertEqual(schema["log_level"], "list(debug|info|warning|error)")
        self.assertEqual(set(schema["env_vars"][0]), {"name", "value"})
        self.assertEqual(schema["env_vars"][0]["value"], "str")

    def env_name_pattern(self):
        element = MANIFEST["schema"]["env_vars"][0]["name"]
        self.assertTrue(element.startswith("match(") and element.endswith(")"), element)
        return element[len("match("):-1]

    def env_sync(self):
        spec = importlib.util.spec_from_file_location("env_sync", APP / "rootfs/opt/hermes-app/lib/env_sync.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_schema_refuses_what_the_start_refuses(self):
        # The Supervisor applies this as voluptuous.Match, which is re.match on the value.
        pattern = re.compile(self.env_name_pattern())
        env_sync = self.env_sync()
        names = [name for name in env_sync.RESERVED_NAMES if name.isupper()]
        names += [prefix + "X" for prefix in env_sync.RESERVED_PREFIXES] + list(env_sync.RESERVED_PREFIXES)
        self.assertGreater(len(names), 30)
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(env_sync.is_reserved(name))
                self.assertIsNone(pattern.match(name))

    def test_schema_lists_exactly_the_reserved_names(self):
        pattern = self.env_name_pattern()
        prefixes, names = re.fullmatch(r"\^\(\?!\(\?:([^)]*)\)\|\(\?:([^)]*)\)\$\)\[A-Z_\]\[A-Z0-9_\]\*\\Z", pattern).groups()
        env_sync = self.env_sync()
        self.assertEqual(set(prefixes.split("|")), set(env_sync.RESERVED_PREFIXES))
        self.assertEqual(set(names.split("|")), {name for name in env_sync.RESERVED_NAMES if name.isupper()})

    def test_schema_still_takes_ordinary_names(self):
        pattern = re.compile(self.env_name_pattern())
        for name in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "HERMES_CUSTOM_API_KEY", "MY_HERMES_HOME", "HOMEBREW",
                     "PATHEXT", "HASS_SOMETHING_ELSE", "_PRIVATE", "TZ"):
            with self.subTest(name=name):
                self.assertIsNotNone(pattern.match(name))
        for name in ("lower", "A-B", "1A", "", "A B", "OPENROUTER_API_KEY\n", "HASS_TOKEN\n"):
            with self.subTest(name=name):
                self.assertIsNone(pattern.match(name))

    def test_documentation_names_how_the_refusal_works(self):
        self.assertIn("are refused when you save the option", DOCS)

    def test_removed_options_stay_gone(self):
        for name in ("proto_ttyd", "git_url", "git_ref", "auto_update", "access_password", "profiles"):
            self.assertNotIn(name, MANIFEST["options"])
            self.assertNotIn(name, MANIFEST["schema"])

    def test_every_option_is_translated(self):
        configuration = TRANSLATIONS["configuration"]
        self.assertEqual(set(configuration), self.NAMES)
        for name, entry in configuration.items():
            with self.subTest(name=name):
                self.assertTrue(entry["name"].strip())
                self.assertTrue(entry["description"].strip())

    def test_every_option_is_documented(self):
        for name in self.NAMES:
            self.assertIn(f"`{name}`", DOCS)


class DocsNameWhatTheyMust(unittest.TestCase):
    """The points CLAUDE.md section 2 says the documentation has to make."""

    PHRASES = {
        "the watchdog switch": "Watchdog",
        "the terminal's slash command": "`/terminal`",
        "the browser's internet need": "cdn.jsdelivr.net",
        "the separate terminal tool": "agent's own `terminal` tool",
        "home is not kept": "`~` is `/tmp/hermes-home`",
        "a backup stops the app": "A backup stops the app",
        "the token exposure": "read it from there",
        "plugins download code": "downloads code from GitHub",
        "plugins run next to the token": "next to the Home Assistant token",
        "plugin dependencies are lost": "`installs/`",
        "comments in config.yaml": "Comments in `config.yaml`",
        "shared profile": "share one Hermes profile",
        "the update message": "Updates are delivered as new app versions in Home Assistant.",
        "uninstall keeps the files": "**Uninstalling keeps your files.**",
        "reinstall has no wizard": "no setup wizard appears",
        "env_vars key after a reinstall": "stays in `.env` as if you had added it by hand",
        "secrets in backups": "`addon.json`",
        "the settings button that can't stop": "**Stop server**",
        "saving the model in settings": "**Settings > Preferences**",
        # The gateway, cron jobs and messengers (docs/architecture.md section 15).
        "cron waits for the wizard": "Cron jobs and messengers start once the setup wizard has saved a provider.",
        "missed runs once": "**Missed runs are caught up once.**",
        "a backup interrupts a task": "**A backup interrupts a running task.**",
        "failures as HA notifications": "persistent notification titled \"Hermes Agent\"",
        "failures in the task list": "in the **Tasks** panel and in `hermes cron list`",
        "webui origin limitation": "hermes-agent#87932",
        "the way around it": "--failure-deliver homeassistant",
        "scripts get the Supervisor token": "`SUPERVISOR_TOKEN` in its environment, but not `HASS_TOKEN` or `HASS_URL`",
        "where scripts reach Core": "`http://supervisor/core/api/`",
        "no messenger by default": "## Messengers\n\nNone is set up.",
        "messenger setup": "`hermes gateway setup`",
        "restart after setup": "Restart the app on its page",
        "allowlists": "`TELEGRAM_ALLOWED_USERS=",
        "the danger of allow-all": "lets anyone who finds your bot control your home",
        "pairing": "pairing codes",
        "webhook modes": "Telegram's webhook mode",
        "missing platforms": "Microsoft Teams, Feishu and DingTalk, because the libraries",
        "sms sends only": "**SMS (Twilio)** can send messages",
        "email polls": "**Email** works by polling",
        "listeners stay on loopback": "even when `.env` or `config.yaml` asks for another address such as `0.0.0.0`",
        "no restart from the setup": "When it offers to restart the gateway at the end, answer no",
        "a broken config brings pairing codes back": "so in that state the bot can send pairing codes again",
        "the allowlist warning": "`No env user allowlists configured…`",
        "one notification per incident": "measured: one notification for two failed runs",
        "claude on the gateway": "hermes-official-plugins#47",
        "the gateway's HA connection": "subscribes to its state changes",
        "exit 78": "exit code 78",
    }

    def test_phrases(self):
        for label, phrase in self.PHRASES.items():
            with self.subTest(label=label):
                self.assertIn(phrase, DOCS)

    def test_claude_section_exists(self):
        self.assertIn("## Claude subscription", DOCS)

    def test_terminal_description_names_the_separate_tool(self):
        self.assertIn("agent's own terminal tool", " ".join(TRANSLATIONS["configuration"]["enable_terminal"]["description"].split()))

    def test_claude_description_names_the_risk(self):
        description = TRANSLATIONS["configuration"]["claude_subscription"]["description"]
        for phrase in ("caller name", "relay", "risk"):
            self.assertIn(phrase, description)


class Discovery(unittest.TestCase):
    """The Supervisor treats any config.* at any depth as an app, skipping only hidden folders and rootfs."""

    PATTERN = re.compile(r"^config\.(json|ya?ml)$")

    def walk(self, start):
        found = []
        for folder, subfolders, files in os.walk(start):
            subfolders[:] = [name for name in subfolders if not name.startswith(".") and name != "rootfs"]
            found += [Path(folder, name) for name in files if self.PATTERN.match(name)]
        return found

    def test_nothing_under_tests_is_named_config(self):
        self.assertEqual(self.walk(ROOT / "tests"), [])

    def test_the_repo_holds_exactly_one_app(self):
        self.assertEqual(self.walk(ROOT), [APP / "config.yaml"])


class Repository(unittest.TestCase):
    def test_repository_values(self):
        repository = yaml.safe_load((ROOT / "repository.yaml").read_text(encoding="utf-8"))
        self.assertEqual(repository["name"], "Hermes Agent (WebUI)")
        self.assertEqual(repository["maintainer"], "Electromrk")

    def test_licence_keeps_both_copyright_lines(self):
        licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Wolfram Ravenwolf", licence)
        self.assertIn("Copyright (c) 2026 Electromrk", licence)

    def test_readme_credits(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in ("Wolfram Ravenwolf", "NousResearch/hermes-agent", "nesquena/hermes-webui"):
            self.assertIn(name, readme)


if __name__ == "__main__":
    unittest.main()
