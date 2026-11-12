"""The harness's own parts: the two stubs over loopback, and its options. Needs no Docker and no internet."""

import http.client
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "harness"
sys.path.insert(0, str(HARNESS))

import harness  # noqa: E402
from webui_client import Client, answer_text, token_gaps  # noqa: E402


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(port, process):
    for _attempt in range(100):
        if process.poll() is not None:
            raise RuntimeError(f"stub exited: {process.stderr.read()}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("stub did not listen")


class Settings(unittest.TestCase):
    def test_default_options_match_the_manifest(self):
        manifest = yaml.safe_load((ROOT / "hermes_webui" / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(harness.DEFAULT_OPTIONS, manifest["options"])

    def test_the_simulator_sits_at_the_supervisors_address(self):
        self.assertEqual(harness.INGRESS_IP, "172.30.32.2")
        self.assertEqual(harness.SUBNET, "172.30.32.0/23")

    def test_the_simulator_sends_all_headers_on_each_listener(self):
        text = (HARNESS / "ingress" / "ingress-sim.nginx.conf").read_text(encoding="utf-8")
        servers = text.split("server {")[1:]
        self.assertEqual(len(servers), 3)
        for server in servers:
            for header in ("Host", "Connection", "X-Ingress-Path", "X-Remote-User-Id", "X-Remote-User-Name",
                           "X-Remote-User-Display-Name", "X-Remote-User-Groups"):
                self.assertRegex(server, rf"proxy_set_header {header} ")
        self.assertIn("172.30.33.10", text)
        self.assertEqual(harness.APP_IP, "172.30.33.10")


class HaStub(unittest.TestCase):
    def setUp(self):
        self.proxy_port, self.direct_port = free_port(), free_port()
        self.process = subprocess.Popen(
            [sys.executable, "-I", str(HARNESS / "stubs" / "ha_api_stub.py"), "--host", "127.0.0.1",
             "--ports", f"{self.proxy_port},{self.direct_port}", "--direct-port", str(self.direct_port),
             "--supervisor-token", "fake-supervisor", "--long-lived-token", "fake-long"],
            stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.process.stderr.close)
        self.addCleanup(self.process.wait)
        self.addCleanup(self.process.kill)
        wait_for(self.proxy_port, self.process)
        wait_for(self.direct_port, self.process)

    def request(self, port, path, token=None, method="GET", body=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read() or b"null")
        connection.close()
        return response.status, data

    def test_the_supervisor_route_takes_only_the_apps_token(self):
        self.assertEqual(self.request(self.proxy_port, "/core/api/", "fake-supervisor")[0], 200)
        self.assertEqual(self.request(self.proxy_port, "/core/api/", "fake-long")[0], 401)
        self.assertEqual(self.request(self.proxy_port, "/core/api/")[0], 401)

    def test_the_direct_route_takes_the_long_lived_token_on_its_port_only(self):
        self.assertEqual(self.request(self.direct_port, "/api/", "fake-long")[0], 200)
        self.assertEqual(self.request(self.direct_port, "/api/", "fake-supervisor")[0], 401)
        self.assertEqual(self.request(self.proxy_port, "/api/", "fake-long")[0], 404)

    def test_states_and_services(self):
        status, states = self.request(self.proxy_port, "/core/api/states", "fake-supervisor")
        self.assertEqual(status, 200)
        self.assertIn("sun.sun", [state["entity_id"] for state in states])
        self.assertEqual(self.request(self.proxy_port, "/core/api/states/missing.entity", "fake-supervisor")[0], 404)
        status, changed = self.request(self.proxy_port, "/core/api/services/light/turn_on", "fake-supervisor",
                                       "POST", {"entity_id": "light.kitchen"})
        self.assertEqual(status, 200)
        self.assertEqual(changed[0]["state"], "on")

    def test_the_call_log_holds_no_tokens(self):
        self.request(self.proxy_port, "/core/api/", "fake-supervisor")
        self.request(self.proxy_port, "/core/api/", "wrong")
        status, calls = self.request(self.proxy_port, "/_stub/calls")
        self.assertEqual(status, 200)
        self.assertEqual([call["authorized"] for call in calls], [True, False])
        self.assertNotIn("fake-supervisor", json.dumps(calls))
        self.assertNotIn("wrong", json.dumps(calls))


class ModelStub(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.log = Path(directory.name) / "stub.jsonl"
        self.process = subprocess.Popen(
            [sys.executable, "-I", str(HARNESS / "stubs" / "model_stub.py"), "--host", "127.0.0.1", "--port", str(self.port),
             "--delay", "0.05", "--chunks", "4", "--log", str(self.log)],
            stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.process.stderr.close)
        self.addCleanup(self.process.wait)
        self.addCleanup(self.process.kill)
        wait_for(self.port, self.process)

    def chat(self, prompt, stream=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", "/v1/chat/completions",
                           body=json.dumps({"model": "m", "stream": stream, "messages": [{"role": "user", "content": prompt}]}),
                           headers={"Content-Type": "application/json", "Authorization": "Bearer fake-key"})
        response = connection.getresponse()
        return connection, response

    def test_streams_delayed_chunks(self):
        connection, response = self.chat("hello")
        moments = []
        while True:
            line = response.readline()
            if not line:
                break
            if line.startswith(b"data: ") and b"chunk-" in line:
                moments.append(time.monotonic())
        connection.close()
        self.assertEqual(len(moments), 4)
        self.assertGreater(moments[-1] - moments[0], 0.1)

    def test_tool_call_and_failure_modes(self):
        connection, response = self.chat('CALL_TOOL terminal {"command": "x"}', stream=False)
        message = json.loads(response.read())["choices"][0]["message"]
        connection.close()
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "terminal")
        connection, response = self.chat("FAIL_HTTP 401")
        response.read()
        connection.close()
        self.assertEqual(response.status, 401)

    def test_the_log_records_that_a_key_came_but_not_the_key(self):
        connection, response = self.chat("hello", stream=False)
        response.read()
        connection.close()
        text = self.log.read_text(encoding="utf-8")
        self.assertIn('"has_auth": true', text)
        self.assertNotIn("fake-key", text)


class ClientHelpers(unittest.TestCase):
    def test_token_helpers(self):
        timeline = [(1.0, "token", '{"text": "a "}'), (1.5, "other", "{}"), (2.0, "token", '{"text": "b"}')]
        self.assertEqual(token_gaps(timeline), (2, [1.0]))
        self.assertEqual(answer_text(timeline), "a b")

    def test_a_session_cookie_replaces_the_old_one(self):
        client = Client("http://127.0.0.1:1/x/", cookie="ingress_session=fixed")
        client.session_cookie = "hermes_session=new"
        self.assertEqual(client._headers()["Cookie"], "ingress_session=fixed; hermes_session=new")


class NoForbiddenNames(unittest.TestCase):
    def test_no_secret_looking_values_are_real(self):
        for name in ("SUPERVISOR_TOKEN", "LONG_LIVED_TOKEN"):
            self.assertRegex(getattr(harness, name), r"^harness-fake-")
        source = (HARNESS / "smoke.py").read_text(encoding="utf-8")
        for value in re.findall(r'FAKE_[A-Z_]+ = "([^"]+)"', source):
            self.assertIn("harness-fake", value)


if __name__ == "__main__":
    unittest.main()
