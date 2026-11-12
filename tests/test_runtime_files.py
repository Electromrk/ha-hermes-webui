"""Static contracts of rootfs: the s6 graph, init rules, wrappers. Needs no Docker and no network."""

from pathlib import Path
import os
import re
import signal
import subprocess
import tempfile
import time
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "hermes_webui"
ROOTFS = APP / "rootfs"
S6 = ROOTFS / "etc" / "s6-overlay" / "s6-rc.d"
INIT = (ROOTFS / "etc/s6-overlay/scripts/hermes-init.sh").read_text(encoding="utf-8")
GATEWAY_RUN = (S6 / "hermes-gateway/run").read_text(encoding="utf-8")
GATEWAY_FINISH = (S6 / "hermes-gateway/finish").read_text(encoding="utf-8")
UPDATE_MESSAGE = "Updates are delivered as new app versions in Home Assistant."


def shell_files():
    for path in sorted(ROOTFS.rglob("*")):
        if path.is_file() and path.read_bytes()[:2] == b"#!" and path.suffix != ".py":
            yield path


class ServiceGraph(unittest.TestCase):
    def test_services(self):
        self.assertEqual({path.name for path in S6.iterdir()}, {"hermes-init", "hermes-webui", "hermes-gateway", "nginx", "user"})
        self.assertEqual({path.name for path in (S6 / "user/contents.d").iterdir()},
                         {"hermes-init", "hermes-webui", "hermes-gateway", "nginx"})

    def test_types_and_order(self):
        self.assertEqual((S6 / "hermes-init/type").read_text().strip(), "oneshot")
        self.assertEqual((S6 / "hermes-webui/type").read_text().strip(), "longrun")
        self.assertEqual((S6 / "nginx/type").read_text().strip(), "longrun")
        self.assertTrue((S6 / "hermes-webui/dependencies.d/hermes-init").exists())
        self.assertTrue((S6 / "nginx/dependencies.d/hermes-webui").exists(), "nginx must stop before the webui")

    def test_the_gateway_depends_on_init_only_and_nothing_waits_for_it(self):
        # A slow messenger connect may take about 45 s; nginx and the webui must not wait for that.
        self.assertEqual((S6 / "hermes-gateway/type").read_text().strip(), "longrun")
        self.assertEqual({path.name for path in (S6 / "hermes-gateway/dependencies.d").iterdir()}, {"hermes-init"})
        for service in ("nginx", "hermes-webui"):
            self.assertFalse((S6 / service / "dependencies.d/hermes-gateway").exists(), service)
        self.assertFalse((S6 / "hermes-gateway/notification-fd").exists())

    def test_gateway_stop_fits_inside_the_app_timeout(self):
        manifest = yaml.safe_load((APP / "config.yaml").read_text(encoding="utf-8"))
        kill_after_ms = int((S6 / "hermes-gateway/timeout-kill").read_text().strip())
        self.assertEqual(kill_after_ms, 45000, "the gateway drains cron jobs for up to 30 s")
        self.assertLess(kill_after_ms / 1000, manifest["timeout"])

    def test_webui_stop_fits_inside_the_app_timeout(self):
        manifest = yaml.safe_load((APP / "config.yaml").read_text(encoding="utf-8"))
        kill_after_ms = int((S6 / "hermes-webui/timeout-kill").read_text().strip())
        self.assertGreater(kill_after_ms, 30000, "the webui's drain takes up to 30 s")
        self.assertLess(kill_after_ms / 1000, manifest["timeout"])

    def test_webui_reports_readiness(self):
        self.assertEqual((S6 / "hermes-webui/notification-fd").read_text().strip(), "3")
        self.assertIn("s6-notifyoncheck", (S6 / "hermes-webui/run").read_text(encoding="utf-8"))

    def test_nginx_gets_no_container_environment(self):
        run = (S6 / "nginx/run").read_text(encoding="utf-8")
        self.assertEqual(run.splitlines()[0], "#!/bin/sh")
        self.assertIn("daemon off;", run)

    def test_no_ttyd_anywhere(self):
        for path in ROOTFS.rglob("*"):
            if path.is_file():
                with self.subTest(path=path):
                    self.assertNotIn("ttyd", path.name)
                    self.assertNotIn("ttyd", path.read_text(encoding="utf-8", errors="replace"))


class InitRules(unittest.TestCase):
    def executable(self):
        return "\n".join(line for line in INIT.splitlines() if not line.lstrip().startswith("#"))

    def test_strict_mode(self):
        self.assertIn("set -euo pipefail", INIT)

    def test_no_world_writable_modes_or_recursive_chown(self):
        for path in shell_files():
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertNotRegex(text, r"chmod\s+(-\w+\s+)?0?77\d")
                self.assertNotRegex(text, r"chmod\s+(-\w+\s+)?a\+w")
                self.assertNotRegex(text, r"chown\s+-[a-zA-Z]*R")

    def test_config_yaml_is_never_seeded(self):
        lines = [line for line in self.executable().splitlines() if "config.yaml" in line]
        self.assertEqual(lines, ["readonly MANAGED_CONFIG=/etc/hermes/config.yaml"])

    def test_ha_token_stays_out_of_env_and_off_the_command_line(self):
        code = self.executable()
        self.assertNotRegex(code, r"HASS_(TOKEN|URL)=.*\.env\b")
        self.assertIn('-K - "$1/api/"', code)
        self.assertNotRegex(code, r'-H\s+"Authorization')

    def test_probes_only_what_the_architecture_names(self):
        self.assertIn("for candidate in http://homeassistant:8123 http://homeassistant:80; do", INIT)
        self.assertNotIn("https://homeassistant", INIT)
        self.assertNotIn("curl -sk", INIT)

    def test_log_lines_never_expand_a_token(self):
        for line in INIT.splitlines():
            if re.search(r"\blog_(debug|info|warning)\b|\bfail\b", line):
                with self.subTest(line=line):
                    self.assertNotRegex(line, r"\$\{?(ha_token|token_value|SUPERVISOR_TOKEN)")

    def test_token_files(self):
        self.assertIn("chmod 0700 /run/s6/container_environment", INIT)
        self.assertIn("umask 077", INIT)
        # Decision 12: SUPERVISOR_TOKEN stays for scripts in both services; only the old name goes.
        for service in ("hermes-webui", "hermes-gateway"):
            run = (S6 / service / "run").read_text(encoding="utf-8")
            with self.subTest(service=service):
                self.assertRegex(run, r"(?m)^unset HASSIO_TOKEN$")
                self.assertNotRegex(run, r"(?m)^unset .*SUPERVISOR_TOKEN")
                self.assertIn("source /run/hermes-app/ha-access.env" if service == "hermes-webui"
                              else 'source "$RUN_DIR/ha-access.env"', run)

    def test_a_restore_leaves_the_claude_login_readable(self):
        # The Supervisor's restore copies /data and /config without owners, and the CLI's files are 0600.
        self.assertIn('find -P "$HOME_DIR" /data/claude ! -user hermes ! \\( -type f -links +1 \\) -exec chown -h hermes:hermes {} +', self.executable())

    def test_a_file_in_place_of_a_folder_stops_with_a_clear_message(self):
        self.assertIn("isn't a folder. Remove or rename it.", self.executable())

    def test_a_stop_removes_what_a_restore_cannot_copy(self):
        # shutil.copytree stops at a named pipe, and the stop runs before every cold backup.
        self.assertEqual((S6 / "hermes-init/down").read_text().strip(), "/etc/s6-overlay/scripts/hermes-stop.sh")
        script = (ROOTFS / "etc/s6-overlay/scripts/hermes-stop.sh").read_text(encoding="utf-8")
        self.assertIn("find -P /config/hermes /data/claude -type p -delete", script)

    def test_marker_folder_for_the_baked_plugin(self):
        self.assertIn("plugins/homeassistant", self.executable())

    def test_settings_are_enforced_as_the_runtime_user(self):
        self.assertIn("as_hermes env SETTINGS=", INIT)
        self.assertIn("check_for_updates: false, ignore_agent_updates: true", INIT)

    def test_claude_lines_are_grouped(self):
        self.assertEqual(INIT.count("# BEGIN claude-provider"), 1)
        self.assertEqual(INIT.count("# END claude-provider"), 1)
        outside = INIT.split("# BEGIN claude-provider")[0] + INIT.split("# END claude-provider")[1]
        self.assertNotIn("ANTHROPIC", outside)
        self.assertNotIn(".credentials.json", outside)


class RootRunTools(unittest.TestCase):
    """Whatever runs as root must take no config from a path the runtime user can write."""

    RUN = (S6 / "hermes-webui/run").read_text(encoding="utf-8")
    DOCKERFILE = (APP / "Dockerfile").read_text(encoding="utf-8")

    def test_every_root_curl_ignores_curlrc(self):
        for name, text in (("init", INIT), ("run", self.RUN)):
            lines = [line for line in text.splitlines() if re.search(r"\bcurl\b", line) and not line.lstrip().startswith("#")]
            self.assertTrue(lines, name)
            for line in lines:
                with self.subTest(script=name, line=line):
                    self.assertRegex(line, r"\bcurl -q ", "-q only works as curl's first argument")

    def test_root_home_is_not_the_runtime_users(self):
        environment = self.DOCKERFILE.split("ENV S6_BEHAVIOUR_IF_STAGE2_FAILS", 1)[1].split("\n\n", 1)[0]
        self.assertIn("HOME=/root \\", environment)
        self.assertNotIn(" HOME=/tmp", environment)
        self.assertIn("usermod -d /tmp/hermes-home hermes", self.DOCKERFILE)

    def test_only_the_runtime_user_gets_its_home(self):
        self.assertIn("s6-setuidgid hermes /bin/sh -c 'export HOME=/tmp/hermes-home;", self.RUN)
        self.assertIn("exec s6-setuidgid hermes /bin/sh -c '\n  export HOME=/tmp/hermes-home\n", GATEWAY_RUN)
        for name, text in (("webui", self.RUN), ("gateway", GATEWAY_RUN)):
            with self.subTest(service=name):
                self.assertNotIn("export HOME", text.split("s6-setuidgid hermes", 1)[0])
        self.assertNotRegex(INIT, r"(?m)^\s*(export\s+)?HOME=")

    def test_the_gateway_reads_nothing_the_runtime_user_can_write_while_root(self):
        before_the_drop = "\n".join(line for line in GATEWAY_RUN.split("exec s6-setuidgid hermes", 1)[0].splitlines()
                                     if not line.lstrip().startswith("#"))
        self.assertNotIn("/config", before_the_drop)
        self.assertNotIn("/tmp", before_the_drop)
        self.assertNotIn("curl", GATEWAY_RUN)

    def test_python_as_root_ignores_its_environment(self):
        calls = [line for line in INIT.splitlines() if '"$PYTHON"' in line]
        self.assertTrue(calls)
        for line in calls:
            with self.subTest(line=line):
                self.assertIn('"$PYTHON" -I ', line)

    def test_file_modes_never_come_from_the_build_folder(self):
        # COPY carries them over, even onto /etc and /opt, and a copy over Samba can change them.
        self.assertNotIn("COPY rootfs/ /\n", self.DOCKERFILE)
        self.assertIn("COPY rootfs/ /tmp/app-rootfs/", self.DOCKERFILE)
        self.assertIn("find /tmp/app-rootfs -type d -exec chmod 00755", self.DOCKERFILE)
        self.assertIn("find /tmp/app-rootfs -type f -exec chmod 0644", self.DOCKERFILE)

    def test_links_are_refused_before_the_first_chown(self):
        self.assertIn('for folder in "$HOME_DIR" /data/claude; do', INIT)
        self.assertLess(INIT.index("is a symbolic link"), INIT.index("install -d"))


class GatewayService(unittest.TestCase):
    """The run and finish scripts of hermes-gateway, statically and with a stand-in for Hermes."""

    def test_environment(self):
        self.assertIn("export HERMES_S6_SUPERVISED_CHILD=1 HERMES_SUPERVISED_CHILD=1", GATEWAY_RUN)
        self.assertIn("export GATEWAY_ON_ALL_ADAPTERS_DOWN=stay_alive", GATEWAY_RUN)
        self.assertIn("export HASS_HOME_CHANNEL=persistent_notification", GATEWAY_RUN)
        self.assertEqual(GATEWAY_RUN.splitlines()[0], "#!/command/with-contenv bash")

    def test_it_runs_the_default_profile_in_the_foreground(self):
        self.assertIn("exec /opt/hermes/.venv/bin/hermes gateway run --replace", GATEWAY_RUN)
        self.assertNotIn("--profile", GATEWAY_RUN)
        self.assertNotIn("gateway start", GATEWAY_RUN)

    def test_a_stop_ends_every_wait_at_once(self):
        # A trap would make bash wait for the running sleep before it ends.
        for name, text in (("run", GATEWAY_RUN), ("finish", GATEWAY_FINISH)):
            with self.subTest(script=name):
                self.assertNotRegex(text, r"(?m)^[^#]*\btrap\b")

    def test_finish_never_keeps_the_service_down(self):
        self.assertNotIn("125", GATEWAY_FINISH)
        self.assertNotIn("sleep", GATEWAY_FINISH)
        self.assertEqual(GATEWAY_FINISH.strip().splitlines()[-1], "exit 0")

    def inner_script(self, home, started):
        """The part that runs as the runtime user, with a folder for HERMES_HOME and a marker for Hermes."""
        inner = GATEWAY_RUN.split("/bin/sh -c '", 1)[1].rsplit("' sh \"$level\"", 1)[0]
        inner = inner.replace("/config/hermes", str(home)).replace("sleep 3", "sleep 0.1").replace("sleep 2", "sleep 0")
        return inner.replace("exec /opt/hermes/.venv/bin/hermes gateway run --replace", f"echo started > {started}; exec sleep 30")

    def test_it_waits_for_the_wizards_config_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            home, started = Path(directory), Path(directory, "started")
            process = subprocess.Popen(["sh", "-c", self.inner_script(home, started), "sh", "info"], stdout=subprocess.PIPE, text=True)
            try:
                time.sleep(0.6)
                self.assertFalse(started.exists(), "Hermes must not start before config.yaml exists")
                (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
                deadline = time.monotonic() + 3
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(started.exists())
            finally:
                process.kill()
                output = process.communicate()[0]
            self.assertEqual(output.count("start once the setup wizard has saved a provider"), 1)

    def test_the_wait_line_follows_the_log_level_and_a_stop_is_quick(self):
        with tempfile.TemporaryDirectory() as directory:
            home, started = Path(directory), Path(directory, "started")
            for level, lines in (("warning", 0), ("error", 0), ("debug", 1)):
                process = subprocess.Popen(["sh", "-c", self.inner_script(home, started), "sh", level], stdout=subprocess.PIPE, text=True)
                time.sleep(0.4)
                begun = time.monotonic()
                process.send_signal(signal.SIGTERM)
                output = process.communicate(timeout=5)[0]
                with self.subTest(level=level):
                    self.assertLess(time.monotonic() - begun, 1.0)
                    self.assertEqual(output.count("start once the setup wizard"), lines)
                    self.assertFalse(started.exists())

    def finish(self, *arguments):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, "finish")
            script.write_text(GATEWAY_FINISH.replace("/run/hermes-app", directory), encoding="utf-8")
            result = subprocess.run(["sh", str(script), *arguments], capture_output=True, text=True, check=False, timeout=5)
            return result, Path(directory, "gateway-paused").exists()

    def test_exit_78_is_an_error_with_a_cause_and_a_pause_before_the_next_start(self):
        result, paused = self.finish("78", "0")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(paused)
        self.assertIn("[hermes-gateway] ERROR:", result.stdout)
        for cause in ("bot token", "another program or Hermes installation", "/config/hermes/.env", "30 s"):
            self.assertIn(cause, result.stdout)
        self.assertIn('if [[ -e "$RUN_DIR/gateway-paused" ]]; then', GATEWAY_RUN)
        self.assertIn('  rm -f "$RUN_DIR/gateway-paused"\n  sleep 30\n', GATEWAY_RUN)

    def test_other_ends_restart_without_a_pause(self):
        for arguments in (("0", "0"), ("1", "0"), ("256", "9"), ("256", "15")):
            with self.subTest(arguments=arguments):
                result, paused = self.finish(*arguments)
                self.assertEqual((result.returncode, result.stdout, paused), (0, "", False))

    def test_init_hands_the_log_level_over(self):
        self.assertIn('printf \'%s\\n\' "$log_level" >"$RUN_DIR/log-level"', INIT)
        self.assertIn('level="$(cat "$RUN_DIR/log-level" 2>/dev/null || echo info)"', GATEWAY_RUN)


class ManagedConfig(unittest.TestCase):
    def test_keys_are_enforced(self):
        managed = yaml.safe_load((APP / "managed-config.yaml").read_text(encoding="utf-8"))
        self.assertIs(managed["auth"]["adopt_external_logins"], False)
        self.assertIs(managed["security"]["allow_lazy_installs"], False)
        self.assertIs(managed["updates"]["check"], False)
        self.assertEqual(managed["plugins"]["auto_update_check_hours"], 0)
        self.assertIs(managed["plugins"]["auto_apply"], False)
        self.assertEqual(managed["logging"]["level"], "INFO")

    def test_gateway_keys(self):
        managed = yaml.safe_load((APP / "managed-config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(managed["gateway"], {"unauthorized_dm_behavior": "ignore"})
        platforms = managed["platforms"]
        self.assertIs(platforms["homeassistant"]["gateway_restart_notification"], False)
        self.assertNotIn("enabled", platforms["homeassistant"], "the HA platform stays on (decision 13)")
        # Each of these opens an inbound listener, several of them on every interface.
        for name in ("webhook", "api_server", "msgraph_webhook", "line", "whatsapp_cloud", "wecom_callback", "teams"):
            with self.subTest(platform=name):
                self.assertEqual(platforms[name], {"enabled": False})
        # Raft's host can only be widened in config.yaml, so it is pinned there; its listener is local by design.
        self.assertEqual(platforms["raft"], {"extra": {"host": "127.0.0.1"}})
        # The browser-hosted Desktop can bind a wildcard; off, the gateway's own listener stays on loopback.
        self.assertEqual(managed["webapp"], {"enabled": False})

    def test_managed_env_keeps_listeners_off_without_the_config(self):
        values = {}
        for line in (ROOTFS / "etc/hermes/.env").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                name, _equals, value = line.partition("=")
                values[name] = value
        self.assertEqual(values, {
            "WEBHOOK_ENABLED": "false", "MSGRAPH_WEBHOOK_ENABLED": "false", "API_SERVER_KEY": "off", "TELEGRAM_WEBHOOK_URL": "",
            "LINE_HOST": "127.0.0.1", "WHATSAPP_CLOUD_WEBHOOK_HOST": "127.0.0.1", "WECOM_CALLBACK_HOST": "127.0.0.1",
            # Environment beats config.yaml for these (gateway/platforms/_shared.py extra_or_secret, A2A and SMS
            # read only the environment), so a wildcard can't come from either file. Teams is off in the config.
            "A2A_HOST": "127.0.0.1", "SMS_WEBHOOK_HOST": "127.0.0.1", "BLUEBUBBLES_WEBHOOK_HOST": "127.0.0.1",
            "FEISHU_WEBHOOK_HOST": "127.0.0.1", "TEAMS_HOST": "127.0.0.1"})
        self.assertLess(len(values["API_SERVER_KEY"]), 16, "Hermes needs 16 characters for a usable key")

    def test_init_can_rewrite_the_level_line(self):
        text = (APP / "managed-config.yaml").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"^  level: ", text, re.M)), 1)
        self.assertIn('sed -i "s/^  level: .*/  level: ${managed_level}/"', INIT)


class Wrappers(unittest.TestCase):
    WRAPPERS = [ROOTFS / "opt/hermes-app/bin/hermes", ROOTFS / "opt/hermes-app/libexec-hermes"]

    def run_wrapper(self, wrapper, *arguments):
        # A throwaway copy with the real launcher swapped for a recorder, so no Hermes is needed.
        with tempfile.TemporaryDirectory() as directory:
            recorder = Path(directory, "recorder")
            recorder.write_text('#!/bin/sh\necho "real:$HERMES_HOME:$HERMES_WRITE_SAFE_ROOT:$HERMES_DISABLE_LAZY_INSTALLS:$*"\n')
            os.chmod(recorder, 0o755)
            text = re.sub(r"exec /opt/hermes/\S+", f"exec {recorder}", wrapper.read_text(encoding="utf-8"))
            copy = Path(directory, "wrapper")
            copy.write_text(text)
            os.chmod(copy, 0o755)
            environment = {key: value for key, value in os.environ.items() if not key.startswith("HERMES_")}
            return subprocess.run([str(copy), *arguments], capture_output=True, text=True, check=False, env=environment)

    def test_update_is_blocked(self):
        for wrapper in self.WRAPPERS:
            for arguments in (["update"], ["--quiet", "update"], ["update", "--force"]):
                with self.subTest(wrapper=wrapper.name, arguments=arguments):
                    result = self.run_wrapper(wrapper, *arguments)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stderr.strip(), UPDATE_MESSAGE)
                    self.assertNotIn("real:", result.stdout)

    def test_everything_else_reaches_the_real_command_with_the_environment(self):
        for wrapper in self.WRAPPERS:
            for arguments in (["model"], ["plugins", "update"], ["--version"], []):
                with self.subTest(wrapper=wrapper.name, arguments=arguments):
                    result = self.run_wrapper(wrapper, *arguments)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(result.stdout.strip(), f"real:/config/hermes:/config/hermes:1:{' '.join(arguments)}")

    def test_profile_puts_the_wrappers_first(self):
        profile = (ROOTFS / "etc/profile.d/hermes.sh").read_text(encoding="utf-8")
        self.assertIn('PATH="/opt/hermes-app/bin:', profile)
        self.assertNotIn("/opt/data", profile)


if __name__ == "__main__":
    unittest.main()
