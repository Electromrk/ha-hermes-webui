"""nginx: static contracts always, plus an opt-in run against real nginx.

Opt-in with HERMES_NGINX_TEST_IMAGE=local/hermes_webui:0.1.0-amd64. That part needs Docker, no internet.
It renders the two parts hermes-init writes, then checks /health and the source-address rules with a
fake webui behind nginx. tests/harness/smoke.py covers the same rules through the whole image.
"""

import http.server
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
NGINX = ROOT / "hermes_webui" / "rootfs" / "opt" / "hermes-app" / "nginx"
CONFIG = (NGINX / "nginx.conf").read_text(encoding="utf-8")
TERMINAL_ON = (NGINX / "terminal-on.conf").read_text(encoding="utf-8")
TERMINAL_OFF = (NGINX / "terminal-off.conf").read_text(encoding="utf-8")


def _locations(text):
    return re.findall(r'location\s+~\*\s+"([^"]+)"', text)


class StaticContracts(unittest.TestCase):
    def test_only_the_supervisor_may_connect(self):
        server_rules = CONFIG.split("# Only the Supervisor's Ingress may connect.", 1)[1]
        self.assertRegex(server_rules, r"allow 172\.30\.32\.2;\s+deny all;")
        self.assertNotIn("allow 127.0.0.1", server_rules)

    def test_health_allows_loopback_and_stays_below_the_watchdog_timeout(self):
        health = CONFIG.split("location = /health {", 1)[1].split("}", 1)[0]
        self.assertIn("allow 172.30.32.2;", health)
        self.assertIn("allow 127.0.0.1;", health)
        self.assertIn("deny all;", health)
        total = sum(int(value) for value in re.findall(r"proxy_(?:connect|read)_timeout (\d+)s;", health))
        self.assertLess(total, 10)

    def test_streaming_and_body_settings(self):
        self.assertIn("proxy_buffering off;", CONFIG)
        self.assertIn("proxy_request_buffering on;", CONFIG)
        self.assertIn("client_max_body_size 25m;", CONFIG)
        self.assertIn("proxy_http_version 1.1;", CONFIG)
        self.assertRegex(CONFIG, r"proxy_read_timeout 3600s;")

    def test_one_request_per_upstream_connection(self):
        # On a reused connection, a body the webui didn't read is parsed as the next request.
        self.assertNotRegex(CONFIG, r"(?m)^\s*keepalive\b")
        self.assertIn("proxy_set_header Connection close;", CONFIG)
        self.assertNotIn('proxy_set_header Connection "";', CONFIG)

    def test_host_header_keeps_the_port(self):
        self.assertIn("proxy_set_header Host $http_host;", CONFIG)
        self.assertNotIn("$host;", CONFIG)

    def test_no_websocket_map_is_left(self):
        self.assertNotIn("$http_upgrade", CONFIG)
        self.assertNotIn("connection_upgrade", CONFIG)

    def test_frame_headers(self):
        self.assertIn("proxy_hide_header X-Frame-Options;", CONFIG)
        for header in ("Content-Security-Policy", "Content-Security-Policy-Report-Only"):
            self.assertIn(f"proxy_hide_header {header};", CONFIG)
            self.assertRegex(CONFIG, rf"add_header {header} \$csp_\w+ always;")
        self.assertEqual(CONFIG.count("frame-ancestors 'self'"), 2)
        self.assertEqual(CONFIG.count("frame-ancestors 'none'"), 2)

    def test_every_block_is_case_insensitive_and_catches_url_parameters(self):
        blocks = _locations(CONFIG) + _locations(TERMINAL_ON) + _locations(TERMINAL_OFF)
        self.assertGreaterEqual(len(blocks), 7)
        for block in blocks:
            with self.subTest(block=block):
                self.assertTrue(block.endswith("(/|;|$)"), block)
        self.assertNotRegex(CONFIG + TERMINAL_ON + TERMINAL_OFF, r"location\s+(\^~\s+)?/(api|terminal)")

    def test_denied_paths_from_the_architecture(self):
        patterns = [re.compile(block, re.I) for block in _locations(CONFIG)]
        for path in ("/api/updates/apply", "/api/updates/force", "/api/updates/clear_lock",
                     "/api/extensions/install", "/api/extensions/uninstall", "/api/extensions/registry",
                     "/api/health/restart", "/api/gateway/start", "/api/gateway/stop", "/api/gateway/restart",
                     "/api/shutdown", "/API/Shutdown", "/api/shutdown;x",
                     "/API/Updates/Apply", "/api/updates/apply;x", "/api/updates/apply/"):
            with self.subTest(path=path):
                self.assertTrue(any(pattern.search(path) for pattern in patterns), path)
        for path in ("/api/updates/check", "/api/updates/summary", "/api/health/agent", "/api/gateway/status",
                     "/api/extensions/config", "/api/shutdownx"):
            with self.subTest(path=path):
                self.assertFalse(any(pattern.search(path) for pattern in patterns), path)

    def test_terminal_off_answers_with_json_naming_the_option(self):
        self.assertEqual(TERMINAL_OFF.count("default_type application/json;"), 2)
        self.assertEqual(TERMINAL_OFF.count("return 404"), 2)
        self.assertIn("enable_terminal", TERMINAL_OFF)
        patterns = [re.compile(block, re.I) for block in _locations(TERMINAL_OFF)]
        for path in ("/api/terminal/start", "/API/TERMINAL/OUTPUT", "/api/terminal", "/api/terminal/start;x", "/terminal/", "/terminal"):
            with self.subTest(path=path):
                self.assertTrue(any(pattern.search(path) for pattern in patterns), path)

    def test_terminal_on_leaves_the_webui_terminal_alone(self):
        self.assertNotIn("/api/terminal", " ".join(_locations(TERMINAL_ON)))

    def test_no_ttyd_is_left(self):
        for text in (CONFIG, TERMINAL_ON, TERMINAL_OFF):
            self.assertNotIn("7681", text)
            self.assertNotIn("proxy_pass http://127.0.0.1:", text)


@unittest.skipUnless(os.environ.get("HERMES_NGINX_TEST_IMAGE"), "opt-in: set HERMES_NGINX_TEST_IMAGE to an app image")
class RealNginx(unittest.TestCase):
    def setUp(self):
        self.image = os.environ["HERMES_NGINX_TEST_IMAGE"]
        self.workdir = Path(tempfile.mkdtemp(prefix="hermes-nginx-test-"))
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.hang = threading.Event()
        self.addCleanup(self.hang.set)
        hang = self.hang

        class Backend(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/slow":
                    hang.wait(30)
                body = b'{"status":"ok"}'
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; object-src 'none'")
                    self.send_header("X-Frame-Options", "DENY")
                    self.end_headers()
                    self.wfile.write(body)
                except BrokenPipeError:  # nginx gave up on the hung request, as it should.
                    pass

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]

    def render(self, health_path="/health"):
        run_dir = self.workdir
        text = CONFIG.replace("/run/hermes-app/", f"{run_dir}/")
        text = text.replace("server 127.0.0.1:8787;", f"server 127.0.0.1:{self.server.server_port};")
        text = text.replace("listen 8099 default_server;", f"listen 127.0.0.1:{self.port} default_server;")
        text = text.replace("/run/nginx.pid", f"{run_dir}/nginx.pid")
        text = text.replace("proxy_pass http://webui/health;", f"proxy_pass http://webui{health_path};")
        (run_dir / "nginx.conf").write_text(text, encoding="utf-8")
        (run_dir / "nginx-log.conf").write_text("error_log /dev/stderr warn;\n", encoding="utf-8")
        (run_dir / "nginx-terminal.conf").write_text(TERMINAL_OFF, encoding="utf-8")
        os.chmod(run_dir, 0o755)

    def start(self):
        common = ["docker", "run", "--rm", "--network", "host",
                  "--mount", f"type=bind,source={self.workdir},target={self.workdir}",
                  "--entrypoint", "/usr/sbin/nginx", self.image]
        syntax = subprocess.run(common + ["-t", "-c", f"{self.workdir}/nginx.conf"], text=True, capture_output=True, check=False)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        container = subprocess.run(
            common[:2] + ["-d"] + common[2:] + ["-c", f"{self.workdir}/nginx.conf", "-g", "daemon off;"],
            check=True, capture_output=True, text=True).stdout.strip()
        self.addCleanup(subprocess.run, ["docker", "rm", "-f", "-v", container], capture_output=True, check=False)
        for _attempt in range(100):
            try:
                self.get("/health")
                return
            except OSError:
                time.sleep(0.05)
        self.fail("nginx failed to become ready")

    def get(self, path, timeout=20):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=timeout) as response:
                return response.status, dict(response.getheaders())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, dict(error.headers)

    def test_health_and_source_rules_from_loopback(self):
        self.render()
        self.start()
        status, headers = self.get("/health")
        self.assertEqual(status, 200)
        self.assertNotIn("X-Frame-Options", headers)
        self.assertEqual(headers["Content-Security-Policy"], "default-src 'self'; frame-ancestors 'self'; object-src 'none'")
        self.assertEqual(self.get("/")[0], 403)
        # A "return" runs before allow and deny, so other addresses get the same fixed answer. Nothing
        # reaches the webui either way, and this request would fail without the rendered part.
        self.assertEqual(self.get("/api/terminal/start")[0], 404)

    def test_a_hung_webui_gives_504_below_the_watchdog_timeout(self):
        self.render(health_path="/slow")
        self.start()
        started = time.monotonic()
        status, _headers = self.get("/health")
        self.assertEqual(status, 504)
        self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    unittest.main()
