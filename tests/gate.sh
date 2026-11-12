#!/usr/bin/env bash
#
# The automated part of the gates in CLAUDE.md section 6, in their order, on the current working tree.
# It first restores what a WSL restart removes (the arm64 emulation, the testbed, the model stub), so it
# also works right after one. Every step runs even if an earlier one failed, so one run shows everything.
#
#   tests/gate.sh
#
# The harness lane (smoke tests) and the testbed lane (real Supervisor) run in parallel, so it takes about
# 25 minutes (estimated). Exit code 0 only if all steps pass.
#
# arm64 is checked on the real Pi 5 with tests/testbed/pi-gate.sh (user decision, 2026-11-11). Only when
# the test SSD isn't available, HERMES_GATE_ARM64_EMULATION=1 adds the emulated arm64 build and smoke test
# to the harness lane, which adds over 35 minutes.
# It uses the testbed, which must not be in use meanwhile, and leaves it in its fresh state: default
# options, setup wizard due, no backup of its own left behind.
#
# Not covered, and still needed before a release: the qa-reviewer, the watchdog, restore, update and
# migration tests of section 6.
#
# Results are in ${HERMES_GATE_LOGS:-~/.local/state/hermes-gate}/latest: summary.txt and one log per step.
# They stay outside the repository, because the repository is mounted into the testbed, and they survive
# a WSL restart. The last 10 runs are kept.

# shellcheck disable=SC2317  # step() calls the functions below, which shellcheck can't see.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
readonly LOG_ROOT="${HERMES_GATE_LOGS:-${HOME}/.local/state/hermes-gate}"
readonly KEEP_RUNS=10
readonly TESTBED=tests/testbed/ha-testbed.sh
readonly IMAGE_AMD64=local/hermes_webui:0.1.0-amd64
readonly IMAGE_ARM64=local/hermes_webui:0.1.0-aarch64
readonly IMAGE_ODD_MODES=local/hermes_webui:gate-odd-modes
readonly BINFMT_IMAGE="tonistiigi/binfmt:qemu-v9.2.2@sha256:1b804311fe87047a4c96d38b4b3ef6f62fca8cd125265917a9e3dc3c996c39e6"
readonly BACKUP_LIMIT_BYTES=5000000
readonly ARM64_EMULATION="${HERMES_GATE_ARM64_EMULATION:-}"

cd "${REPO_ROOT}" || exit 9
install -d -m 700 "${LOG_ROOT}"

# Two runs at once would change the same testbed.
exec 9>"${LOG_ROOT}/.lock"
flock --nonblock 9 || {
  echo "Another gate run is active." >&2
  exit 9
}

RUN_DIR="${LOG_ROOT}/$(date +%Y%m%d-%H%M%S)"
readonly RUN_DIR
mkdir -p "${RUN_DIR}"
ln -sfn "${RUN_DIR}" "${LOG_ROOT}/latest"
SUMMARY="${RUN_DIR}/summary.txt"
readonly SUMMARY
failed=0

step() {
  local name="$1" start=${SECONDS} rc
  shift
  "$@" >"${RUN_DIR}/${name}.log" 2>&1
  rc=$?
  printf '%-14s rc=%-3s %5s s\n' "${name}" "${rc}" "$((SECONDS - start))" | tee -a "${SUMMARY}"
  [[ ${rc} -eq 0 ]] || failed=1
}

ensure_emulation() {
  [[ -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]] && return 0
  docker run --privileged --rm "${BINFMT_IMAGE}" --install arm64
}

list_modes() {
  docker run --rm --entrypoint sh "$1" -c \
    "find /etc /opt /usr/local /root -xdev -not -path '/opt/hermes/.venv/*' -printf '%m %U %G %p\n' | sort -k4"
}

# A copy over Samba or a strict umask must not change the image.
modes_ignore_build_folder() {
  local copy rc=0
  copy="$(mktemp -d "${RUN_DIR}/odd-modes.XXXXXX")"
  cp -r hermes_webui/. "${copy}/"
  find "${copy}" -type d -exec chmod 2775 {} +
  find "${copy}" -type f -exec chmod 0770 {} +
  chmod 2775 "${copy}"
  docker buildx build --platform linux/amd64 --load -t "${IMAGE_ODD_MODES}" "${copy}" || rc=1
  if [[ ${rc} -eq 0 ]]; then
    diff <(list_modes "${IMAGE_AMD64}") <(list_modes "${IMAGE_ODD_MODES}") && echo "No differences." || rc=1
  fi
  docker image rm "${IMAGE_ODD_MODES}" >/dev/null 2>&1
  rm -rf "${copy}"
  return "${rc}"
}

# The backup gate of CLAUDE.md section 2.2. Each backup holds image.tar, so this one doesn't stay.
backup_gate() {
  local output slug bytes
  output="$("${TESTBED}" backup "gate-$(date +%Y%m%d-%H%M%S)" 2>&1)" || {
    echo "${output}"
    return 1
  }
  echo "${output}"
  slug="$(sed -n 's/.*Backup slug: //p' <<<"${output}")"
  bytes="$(sed -n 's/.*payload without image.tar \([0-9]*\) bytes.*/\1/p' <<<"${output}")"
  [[ -n "${slug}" ]] && "${TESTBED}" ha backups remove "${slug}"
  [[ -n "${bytes}" && ${bytes} -lt ${BACKUP_LIMIT_BYTES} ]]
}

printf 'Tree: %s with %s changed files, started %s\n\n' "$(git rev-parse --short HEAD)" "$(git status --short | wc -l)" "$(date)" | tee "${SUMMARY}"

[[ -z "${ARM64_EMULATION}" ]] || step emulation ensure_emulation
step testbed-up "${TESTBED}" up
step lint bash tests/harness/lint.sh
step build-amd64 docker buildx build --platform linux/amd64 --load -t "${IMAGE_AMD64}" hermes_webui/
[[ -z "${ARM64_EMULATION}" ]] ||
  step build-arm64 docker buildx build --platform linux/arm64 --load -t "${IMAGE_ARM64}" --build-arg BUILD_ARCH=aarch64 hermes_webui/
step modes modes_ignore_build_folder
step unit env HERMES_TEST_IMAGE="${IMAGE_AMD64}" HERMES_NGINX_TEST_IMAGE="${IMAGE_AMD64}" python3 -m unittest discover -s tests

# The two lanes share nothing: the harness runs its own containers and stub on this Docker host, the testbed
# its own Supervisor inside its container. Each lane runs in a subshell and reports its result by exit code.
harness_lane() {
  step smoke-amd64 python3 tests/harness/smoke.py
  step claude-smoke python3 tests/harness/claude_smoke.py
  [[ -z "${ARM64_EMULATION}" ]] || step smoke-arm64 python3 tests/harness/smoke.py --image "${IMAGE_ARM64}"
  return "${failed}"
}
testbed_lane() {
  step rebuild "${TESTBED}" rebuild
  step model-stub "${TESTBED}" model-stub start
  step claude-check python3 tests/testbed/claude_check.py
  step backup backup_gate
  step ingress-check python3 tests/testbed/ingress_check.py --reset
  step cron-check python3 tests/testbed/cron_check.py --reset
  return "${failed}"
}
harness_lane &
harness_pid=$!
testbed_lane &
testbed_pid=$!
wait "${harness_pid}" || failed=1
wait "${testbed_pid}" || failed=1

{
  echo
  for name in unit smoke-amd64 claude-smoke smoke-arm64 claude-check ingress-check cron-check; do
    [[ -f "${RUN_DIR}/${name}.log" ]] || continue
    echo "== ${name}"
    grep -E '^\[FAIL\]|^FAIL|^ERROR|passed|^Ran |^OK|All checks' "${RUN_DIR}/${name}.log" | cut -c1-240 | tail -12
  done
  echo "== backup"
  grep 'Uncompressed' "${RUN_DIR}/backup.log" | cut -c1-160
  echo
  [[ -n "${ARM64_EMULATION}" ]] || echo "arm64: run tests/testbed/pi-gate.sh on the test SSD."
  if [[ ${failed} -eq 0 ]]; then echo "RESULT: GREEN"; else echo "RESULT: RED"; fi
} | tee -a "${SUMMARY}"

mapfile -t runs < <(find "${LOG_ROOT}" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort -r)
for old in "${runs[@]:${KEEP_RUNS}}"; do
  rm -rf "${old}"
done

exit "${failed}"
