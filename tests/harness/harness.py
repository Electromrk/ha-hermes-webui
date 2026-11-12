#!/usr/bin/env python3
"""Supervisor and Ingress simulation for the app image: the fast test loop.

  harness.py up [--option NAME=JSON ...]     Fresh start: network, stubs, app, Ingress simulator
  harness.py restart [--option NAME=JSON ...] Recreate the app on the same data, as the Supervisor does
  harness.py status | logs | down
  harness.py payload                         Backup payload of the current data (see backup_payload.py)
  harness.py exec [--user hermes] -- CMD...  Run a command in the app

The simulated setup follows what the real Supervisor does with an app container (docker inspect of
a real one, Supervisor 2026.10.5): no init, seccomp and AppArmor unconfined, private /dev/shm,
OOM score 200, a /config and a /data bind mount, TZ plus both tokens in the environment.

The real gate stays tests/testbed/. Everything here uses fake tokens. No file in this folder may be
named config.*, because the Supervisor parses those at any depth.
"""
import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
APP_DIR = REPO / "hermes_webui"

NETWORK = "hermes-harness"
SUBNET = "172.30.32.0/23"
GATEWAY = "172.30.32.1"
INGRESS_IP = "172.30.32.2"
HA_IP = "172.30.32.3"
APP_IP = "172.30.33.10"
MODEL_IP = "172.30.33.250"
APP = "hermes-harness-app"
INGRESS = "hermes-harness-ingress"
HA = "hermes-harness-ha"
MODEL = "hermes-harness-model"
CONTAINERS = (INGRESS, APP, MODEL, HA)
LABEL = "io.hermes-webui.harness=1"
INGRESS_PREFIX = "/api/hassio_ingress/TESTTOKEN/"

# Fake on purpose: these only ever reach the stubs.
SUPERVISOR_TOKEN = "harness-fake-supervisor-token"
LONG_LIVED_TOKEN = "harness-fake-long-lived-token"

# Same as "options" in hermes_webui/config.yaml. tests/test_harness.py keeps the two equal.
DEFAULT_OPTIONS = {"enable_terminal": False, "claude_subscription": False, "env_vars": [], "log_level": "info"}

# The host ports of the three Ingress listeners, on localhost only.
HOST_PORTS = {"user": 18123, "no_name": 18124, "no_user": 18125}


def state_dir():
    return Path(os.environ.get("HERMES_HARNESS_STATE") or Path(tempfile.gettempdir()) / "hermes-harness")


def default_image():
    match = re.search(r'^version:\s*"?([^"\s]+)', (APP_DIR / "config.yaml").read_text(encoding="utf-8"), re.M)
    return os.environ.get("HERMES_HARNESS_IMAGE") or f"local/hermes_webui:{match.group(1)}-amd64"


def run(*command, check=True, capture=True, input_text=None, timeout=300):
    return subprocess.run(command, check=check, text=True, capture_output=capture, input=input_text, timeout=timeout)


def docker(*arguments, **kwargs):
    return run("docker", *arguments, **kwargs)


def container_state(name):
    result = docker("container", "inspect", "--format", "{{.State.Status}}", name, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def remove_container(name, stop_time=None):
    if not container_state(name):
        return
    if stop_time is not None:
        docker("stop", "--time", str(stop_time), name, check=False, timeout=stop_time + 30)
    # -v takes the base image's anonymous /opt/data volume along, as the Supervisor does.
    docker("rm", "--force", "--volumes", name, check=False)


def ensure_network():
    if docker("network", "inspect", NETWORK, check=False).returncode == 0:
        return
    docker("network", "create", "--label", LABEL, "--subnet", SUBNET, "--gateway", GATEWAY, NETWORK)


class Harness:
    def __init__(self, image=None):
        self.image = image or default_image()
        self.state = state_dir()
        self.data = self.state / "data"
        self.config = self.state / "config"

    # ---- Data ----

    def wipe_data(self):
        """The folders hold uid 10000 files, so a container deletes them."""
        if self.state.exists():
            docker("run", "--rm", "--network", "none", "--mount", f"type=bind,source={self.state},target=/state",
                   "--entrypoint", "/bin/sh", self.image, "-c", "rm -rf /state/data /state/config")
            shutil.rmtree(self.state, ignore_errors=True)

    def write_options(self, options):
        self.data.mkdir(parents=True, exist_ok=True)
        self.config.mkdir(parents=True, exist_ok=True)
        path = self.data / "options.json"
        # A container may have left this root-owned on an earlier run; the folder is ours.
        if path.exists():
            docker("run", "--rm", "--network", "none", "--mount", f"type=bind,source={self.data},target=/d",
                   "--entrypoint", "/bin/rm", self.image, "-f", "/d/options.json")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(options, handle)

    # ---- Containers ----

    def start_stubs(self):
        ensure_network()
        python = "/opt/hermes/.venv/bin/python"
        stubs = f"type=bind,source={HERE / 'stubs'},target=/stubs,readonly"
        if not container_state(HA):
            docker("run", "--detach", "--name", HA, "--label", LABEL, "--network", NETWORK, "--ip", HA_IP,
                   "--network-alias", "supervisor", "--network-alias", "homeassistant", "--mount", stubs,
                   "--entrypoint", python, self.image, "-I", "/stubs/ha_api_stub.py",
                   "--supervisor-token", SUPERVISOR_TOKEN, "--long-lived-token", LONG_LIVED_TOKEN)
        if not container_state(MODEL):
            docker("run", "--detach", "--name", MODEL, "--label", LABEL, "--network", NETWORK, "--ip", MODEL_IP,
                   "--network-alias", "model-stub", "--mount", stubs, "--entrypoint", python, self.image,
                   "-I", "/stubs/model_stub.py", "--port", "9999", "--delay", "0.5", "--chunks", "8",
                   "--log", "/tmp/model_stub.jsonl")

    def start_ingress(self):
        if container_state(INGRESS):
            return
        conf = HERE / "ingress" / "ingress-sim.nginx.conf"
        publish = []
        for container_port, host_port in zip((8123, 8124, 8125), HOST_PORTS.values()):
            publish += ["--publish", f"127.0.0.1:{host_port}:{container_port}"]
        docker("run", "--detach", "--name", INGRESS, "--label", LABEL, "--network", NETWORK, "--ip", INGRESS_IP,
               *publish, "--mount", f"type=bind,source={conf},target=/etc/ingress-sim.conf,readonly",
               "--entrypoint", "/usr/sbin/nginx", self.image, "-c", "/etc/ingress-sim.conf", "-g", "daemon off;")

    def start_app(self, options, environment=None, isolated=False):
        """isolated=True starts the app with no network at all, for fake messenger tokens: nothing it
        starts can reach a real messenger's servers, nor the stubs and the Ingress simulator."""
        environment = {"TZ": "Etc/UTC", "SUPERVISOR_TOKEN": SUPERVISOR_TOKEN, "HASSIO_TOKEN": SUPERVISOR_TOKEN,
                       **(environment or {})}
        self.write_options(options)
        env_args = []
        for name, value in environment.items():
            env_args += ["--env", f"{name}={value}"]
        network = ["--network", "none"] if isolated else ["--network", NETWORK, "--ip", APP_IP]
        docker("run", "--detach", "--name", APP, "--label", LABEL, *network,
               "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined",
               "--tmpfs", "/dev/shm", "--oom-score-adj", "200", "--stop-timeout", "60", *env_args,
               "--mount", f"type=bind,source={self.data},target=/data",
               "--mount", f"type=bind,source={self.config},target=/config", self.image)

    def stop_app(self, stop_time=60):
        remove_container(APP, stop_time=stop_time)

    def wait_healthy(self, timeout=120):
        """Waits until /health answers through nginx, from the Supervisor's address."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if container_state(APP) != "running":
                return False
            code = self.ingress_curl("-s", "-m", "3", "-o", "/dev/null", "-w", "%{http_code}",
                                     f"http://{APP_IP}:8099/health").stdout.strip()
            if code == "200":
                return True
            time.sleep(0.5)
        return False

    def wait_local_healthy(self, timeout=120):
        """Waits until /health answers inside the app, for an isolated start."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if container_state(APP) != "running":
                return False
            if self.app_exec("curl", "-q", "-s", "-m", "3", "-o", "/dev/null", "-w", "%{http_code}",
                             "http://127.0.0.1:8099/health").stdout.strip() == "200":
                return True
            time.sleep(0.5)
        return False

    def up(self, options=None, environment=None):
        self.down()
        self.start_stubs()
        self.start_app({**DEFAULT_OPTIONS, **(options or {})}, environment)
        self.start_ingress()
        return self.wait_healthy()

    def restart(self, options=None, environment=None, stop_time=60, isolated=False):
        """Recreates the app container on the same data, as every Supervisor stop and start does."""
        self.stop_app(stop_time=stop_time)
        self.start_app({**DEFAULT_OPTIONS, **(options or {})}, environment, isolated=isolated)
        return self.wait_local_healthy() if isolated else self.wait_healthy()

    def down(self):
        for name in CONTAINERS:
            remove_container(name)
        docker("network", "rm", NETWORK, check=False)
        self.wipe_data()

    # ---- Access ----

    def app_exec(self, *command, user=None, check=False, timeout=120, input_text=None):
        # The image's HOME is root's. Only the webui's run script sets the runtime user's, so do the same here.
        user_args = ["--user", user, "--env", "HOME=/tmp/hermes-home"] if user == "hermes" else (["--user", user] if user else [])
        stdin_args = ["--interactive"] if input_text is not None else []
        return docker("exec", *user_args, *stdin_args, APP, *command, check=check, timeout=timeout,
                      input_text=input_text)

    def ingress_curl(self, *arguments, timeout=60):
        """curl from the Supervisor's address, straight to wherever the arguments say."""
        return docker("exec", INGRESS, "curl", *arguments, check=False, timeout=timeout)

    def other_source_curl(self, *arguments, timeout=60):
        """curl from an address on the hassio network that isn't the Supervisor's."""
        return docker("exec", HA, "curl", *arguments, check=False, timeout=timeout)

    def logs(self):
        result = docker("logs", APP, check=False)
        return result.stdout + result.stderr

    def stub_calls(self):
        result = docker("exec", HA, "curl", "-s", "http://127.0.0.1/_stub/calls", check=False)
        return json.loads(result.stdout or "[]")

    def model_log(self):
        result = docker("exec", MODEL, "cat", "/tmp/model_stub.jsonl", check=False)
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def payload(self):
        """Runs the calculator inside a throwaway container, because the data is uid 10000, mode 0700."""
        result = docker(
            "run", "--rm", "--network", "none",
            "--mount", f"type=bind,source={self.config},target=/config,readonly",
            "--mount", f"type=bind,source={self.data},target=/data,readonly",
            "--mount", f"type=bind,source={HERE / 'backup_payload.py'},target=/tmp/backup_payload.py,readonly",
            "--mount", f"type=bind,source={APP_DIR / 'config.yaml'},target=/tmp/manifest.yaml,readonly",
            "--entrypoint", "/opt/hermes/.venv/bin/python", self.image, "-I", "/tmp/backup_payload.py",
            "--config", "/config", "--data", "/data", "--manifest", "/tmp/manifest.yaml", "--json", check=False)
        if not result.stdout.strip():
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout)


def parse_options(pairs):
    options = {}
    for pair in pairs or []:
        name, separator, value = pair.partition("=")
        if not separator:
            raise SystemExit(f"--option needs NAME=JSON, got {pair!r}")
        try:
            options[name] = json.loads(value)
        except ValueError:
            options[name] = value
    return options


def cmd_status(harness):
    print(f"Image: {harness.image}")
    print(f"State: {harness.state}")
    for name in CONTAINERS:
        print(f"{name}: {container_state(name) or 'absent'}")
    for label, port in HOST_PORTS.items():
        print(f"Ingress ({label}): http://127.0.0.1:{port}{INGRESS_PREFIX}")
    print(f"Panel page (frames the app): http://127.0.0.1:{HOST_PORTS['user']}/")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("up", "restart"):
        command = commands.add_parser(name)
        command.add_argument("--option", action="append", metavar="NAME=JSON")
    for name in ("status", "logs", "down", "payload"):
        commands.add_parser(name)
    execute = commands.add_parser("exec")
    execute.add_argument("--user")
    execute.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    harness = Harness(args.image)

    if args.command in ("up", "restart"):
        healthy = getattr(harness, args.command)(parse_options(args.option))
        cmd_status(harness)
        print(f"Healthy: {healthy}")
        return 0 if healthy else 1
    if args.command == "status":
        cmd_status(harness)
    elif args.command == "logs":
        sys.stdout.write(harness.logs())
    elif args.command == "down":
        harness.down()
    elif args.command == "payload":
        result = harness.payload()
        print(f"Payload without image.tar: {result['total_bytes']} bytes; gate: {result['gate_passed']}")
        for name, size in sorted(result["files"], key=lambda item: -item[1])[:15]:
            print(f"  {size:>10}  {name}")
        for name, size in result["excluded"]:
            print(f"  excluded {size:>10}  {name}")
    elif args.command == "exec":
        command = [word for word in args.arguments if word != "--"]
        result = harness.app_exec(*command, user=args.user)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
