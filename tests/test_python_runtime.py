"""How the webui is started: on the agent's sealed Python, with nothing installed at start.

Static checks always. HERMES_TEST_IMAGE=local/hermes_webui:0.1.0-amd64 adds checks inside the image,
which need Docker and no internet.
"""

import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
ROOTFS = ROOT / "hermes_webui" / "rootfs"
RUN = (ROOTFS / "etc/s6-overlay/s6-rc.d/hermes-webui/run").read_text(encoding="utf-8")
PYTHON = "/opt/hermes/.venv/bin/python"


def executable_lines():
    return [line for line in RUN.splitlines() if line.strip() and not line.lstrip().startswith("#")]


class StartCommand(unittest.TestCase):
    def test_starts_server_py_with_the_agents_python(self):
        self.assertIn(f"exec {PYTHON} server.py", RUN)

    def test_never_starts_the_bootstrap_which_can_install_packages(self):
        self.assertFalse([line for line in executable_lines() if "bootstrap.py" in line])
        self.assertIn("HERMES_WEBUI_DISABLE_LOCAL_VENV=1", RUN)

    def test_does_not_pin_a_venv_generation(self):
        self.assertFalse([line for line in executable_lines() if "HERMES_WEBUI_PYTHON" in line])

    def test_runs_as_the_runtime_user_in_the_foreground(self):
        self.assertIn("s6-setuidgid hermes", RUN)
        self.assertIn("HERMES_WEBUI_FOREGROUND=1", RUN)

    def test_tokens_and_trusted_header(self):
        self.assertIn("source /run/hermes-app/ha-access.env", RUN)
        # SUPERVISOR_TOKEN stays for scripts the agent runs (decision 12 in docs/architecture.md).
        self.assertIn("\nunset HASSIO_TOKEN\n", RUN)
        self.assertNotIn("unset SUPERVISOR_TOKEN", RUN)
        self.assertIn("HERMES_WEBUI_TRUSTED_AUTH_HEADER=X-Remote-User-Id", RUN)
        self.assertIn("HERMES_WEBUI_TRUSTED_PROXY_CIDRS=127.0.0.1/32", RUN)
        self.assertNotIn("HERMES_WEBUI_ONBOARDING_OPEN", RUN)

    def test_state_lives_under_hermes_home(self):
        self.assertIn("HERMES_WEBUI_STATE_DIR=/config/hermes/webui", RUN)
        self.assertIn("HERMES_WEBUI_DEFAULT_WORKSPACE=/config/hermes/workspace", RUN)

    def test_extension_paths_are_closed(self):
        self.assertIn("HERMES_WEBUI_PLUGINS_DIR=/opt/hermes-app/empty-plugins", RUN)
        self.assertNotIn("HERMES_WEBUI_AUTO_INSTALL", RUN)


@unittest.skipUnless(os.environ.get("HERMES_TEST_IMAGE"), "opt-in: set HERMES_TEST_IMAGE to an app image")
class InsideTheImage(unittest.TestCase):
    def run_in_image(self, *command):
        return subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", command[0],
                               os.environ["HERMES_TEST_IMAGE"], *command[1:]],
                              text=True, capture_output=True, check=False, timeout=180)

    def test_python_version_the_agent_needs(self):
        result = self.run_in_image(PYTHON, "-c", "import sys; print(sys.version_info[:2])")
        self.assertEqual(result.stdout.strip(), "(3, 14)", result.stderr)

    def test_webui_needs_no_local_venv_or_git(self):
        result = self.run_in_image("/bin/sh", "-c", "cd /opt/hermes-webui; ls -A; test ! -e .venv; test ! -e .git; test ! -e bootstrap.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ttyd_is_not_in_the_image(self):
        result = self.run_in_image("/bin/sh", "-c", "command -v ttyd; test ! -e /etc/s6-overlay/s6-rc.d/ttyd")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.strip(), "")

    def test_nothing_under_config_or_data(self):
        result = self.run_in_image("/bin/sh", "-c", "test ! -e /config && test ! -e /data")
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
