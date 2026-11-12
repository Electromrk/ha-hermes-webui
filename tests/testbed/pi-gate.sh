#!/usr/bin/env bash
#
# The checks of tests/gate.sh that need a real device, run on the enrolled test SSD over SSH.
#
#   tests/testbed/ha-testbed.sh pi-enroll <host> --test-ssd-is-in     # once, see the README
#   tests/testbed/pi-gate.sh
#
# It copies the last commit to the device and lets the Supervisor build it, which is the route users
# take too, and measures the build, the start, the RAM, a backup and a restore. It wipes the app's data
# on the device several times. It never runs without the enrolled host key and marker.
#
# Not covered: a real chat with a real model, which needs the user's login. Do that by hand afterwards.
#
# Results are in ${HERMES_GATE_LOGS:-~/.local/state/hermes-gate}/pi-latest: summary.txt and one log per
# step. They stay outside the repository, because they name the device's address.

# shellcheck disable=SC2317  # step() calls the functions below, which shellcheck can't see.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly REPO_ROOT
readonly LOG_ROOT="${HERMES_GATE_LOGS:-${HOME}/.local/state/hermes-gate}"
readonly TESTBED=tests/testbed/ha-testbed.sh
readonly APP_CONTAINER=app_local_hermes_webui
readonly BACKUP_LIMIT_BYTES=5000000
readonly SETTLE_SECONDS=60

export HERMES_TESTBED_TARGET=pi

cd "${REPO_ROOT}" || exit 9
install -d -m 700 "${LOG_ROOT}"

exec 9>"${LOG_ROOT}/.lock"
flock --nonblock 9 || {
  echo "Another gate run is active." >&2
  exit 9
}

RUN_DIR="${LOG_ROOT}/pi-$(date +%Y%m%d-%H%M%S)"
readonly RUN_DIR
mkdir -p "${RUN_DIR}"
ln -sfn "${RUN_DIR}" "${LOG_ROOT}/pi-latest"
SUMMARY="${RUN_DIR}/summary.txt"
readonly SUMMARY
failed=0

step() {
  local name="$1" start=${SECONDS} rc
  shift
  "$@" >"${RUN_DIR}/${name}.log" 2>&1
  rc=$?
  printf '%-16s rc=%-3s %6s s\n' "${name}" "${rc}" "$((SECONDS - start))" | tee -a "${SUMMARY}"
  [[ ${rc} -eq 0 ]] || failed=1
  return "${rc}"
}

# The Supervisor refuses this for a while after the device has started.
start_app() {
  local deadline=$((SECONDS + 300))
  while ((SECONDS < deadline)); do
    "${TESTBED}" start && return 0
    sleep 15
  done
  return 1
}

memory_now() {
  "${TESTBED}" on-host docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.CPUPerc}}' "${APP_CONTAINER}"
}

ram_sample() {
  sleep "${SETTLE_SECONDS}"
  memory_now
}

# ingress_check.py ends with a restart, so this needs its own chat. The stub model streams for a
# few seconds, and the samples are taken while it does; the last one after the RAM has settled.
ram_after_chat() {
  local chat
  python3 - <<'PYTHON' &
import sys
sys.path.insert(0, "tests/testbed")
from ingress_check import open_ingress, wait_healthy
from webui_client import answer_text, run_chat

_url, client = open_ingress()
if not wait_healthy(client):
    sys.exit("The app doesn't answer.")
_session, timeline = run_chat(client, "hello, to measure the RAM during a chat")
last = timeline[-1][1] if timeline else "nothing"
if answer_text(timeline).split() != [f"chunk-{i}" for i in range(1, 9)]:
    sys.exit(f"The chat failed: the stream ended with {last}.")
print("The chat was answered in full.")
PYTHON
  chat=$!
  while kill -0 "${chat}" 2>/dev/null; do
    echo "during the chat: $(memory_now)"
    sleep 1
  done
  wait "${chat}" || return 1
  echo "settled: $(ram_sample)"
}

# A cold backup stops the app. The device's own Docker events give the downtime, so the clocks of
# this machine and the device don't need to agree.
backup_gate() {
  local output slug bytes since
  since="$("${TESTBED}" on-host date +%s)" || return 1
  output="$("${TESTBED}" backup "gate-$(date +%Y%m%d-%H%M%S)" 2>&1)" || {
    echo "${output}"
    return 1
  }
  echo "${output}"
  "${TESTBED}" on-host docker events --since "${since}" --until "$("${TESTBED}" on-host date +%s)" \
    --filter "container=${APP_CONTAINER}" --filter event=die --filter event=start --format '{{.TimeNano}} {{.Action}}' |
    awk '$2 == "die" && !stop { stop = $1 } $2 == "start" { start = $1 }
         END { if (stop && start > stop) printf "Downtime: the container was down for %.1f s (measured; the webui needs a few seconds more to answer).\n", (start - stop) / 1e9
               else print "Downtime: not measured, no stop and start in the Docker events." }'
  slug="$(sed -n 's/.*Backup slug: //p' <<<"${output}")"
  bytes="$(sed -n 's/.*payload without image.tar \([0-9]*\) bytes.*/\1/p' <<<"${output}")"
  [[ -n "${slug}" ]] && "${TESTBED}" ha backups remove "${slug}"
  [[ -n "${bytes}" && ${bytes} -lt ${BACKUP_LIMIT_BYTES} ]]
}

# The checks need a fresh app, logged out of Claude, with the wizard due. The device keeps its app
# between runs, so a run starts from there: a Claude login made by hand is signed out first, so that
# its session ends at Anthropic too, and then the app's data and options are wiped.
fresh_app() {
  "${TESTBED}" on-host docker exec -u hermes -e HOME=/tmp/hermes-home "${APP_CONTAINER}" sh -c \
    'if [ -e /data/claude/.credentials.json ]; then claude auth logout >/dev/null 2>&1 && echo "Signed out of Claude."; fi; true'
  python3 - <<'PYTHON'
import sys
sys.path.insert(0, "tests/testbed")
import ingress_check
ingress_check.reset_app("local_hermes_webui")
sys.exit(1 if ingress_check.FAILED else 0)
PYTHON
}

finish() {
  {
    echo
    for name in ingress-check claude-check cron-check; do
      [[ -f "${RUN_DIR}/${name}.log" ]] || continue
      echo "== ${name}"
      grep -E '^\[FAIL\]|All checks|failed$' "${RUN_DIR}/${name}.log" | cut -c1-240 | tail -12
    done
    for name in facts ram-idle ram-after-chat; do
      [[ -f "${RUN_DIR}/${name}.log" ]] || continue
      echo "== ${name}"
      grep -vE '^\[testbed\]|^$' "${RUN_DIR}/${name}.log" | cut -c1-160 | head -24
    done
    if [[ -f "${RUN_DIR}/install.log" ]]; then
      echo "== install"
      grep -E 'Finished|image is' "${RUN_DIR}/install.log" | cut -c1-160
    fi
    if [[ -f "${RUN_DIR}/backup.log" ]]; then
      echo "== backup"
      grep -E 'Uncompressed|Downtime' "${RUN_DIR}/backup.log" | cut -c1-160
    fi
    echo
    [[ -z "${1:-}" ]] || echo "Stopped early: ${1}"
    echo "Still by hand: a real chat with a real model at the current plugin commit."
    if [[ ${failed} -eq 0 ]]; then echo "RESULT: GREEN"; else echo "RESULT: RED"; fi
  } | tee -a "${SUMMARY}"
  exit "${failed}"
}

printf 'Commit: %s, started %s\n\n' "$(git rev-parse --short HEAD)" "$(date)" | tee "${SUMMARY}"

# Nothing else makes sense on a device that doesn't pass these.
step up "${TESTBED}" up || finish "the device isn't ready."
step facts "${TESTBED}" pi-facts
step deploy "${TESTBED}" pi-deploy || finish "the app couldn't be copied."
step install "${TESTBED}" pi-install || finish "the app didn't build."
step start start_app || finish "the app didn't start."
step fresh-app fresh_app || finish "the app couldn't be reset to a fresh install."
step ram-idle ram_sample
step model-stub "${TESTBED}" model-stub start
step ingress-check python3 tests/testbed/ingress_check.py
step ram-after-chat ram_after_chat
step claude-check python3 tests/testbed/claude_check.py
step backup backup_gate
# Scheduled tasks through the gateway, with its own backup and restore; that takes a while on a device.
# --reset leaves the app fresh, so the wizard is due for a chat by hand instead of the stopped stub model.
step cron-check python3 tests/testbed/cron_check.py --reset
step model-stub-stop "${TESTBED}" model-stub stop
finish
