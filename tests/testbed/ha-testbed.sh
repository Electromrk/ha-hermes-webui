#!/usr/bin/env bash
#
# ha-testbed.sh - headless driver for the official Home Assistant devcontainer.
#
# Runs a real Supervisor plus a real Home Assistant Core inside one privileged
# container on this machine, so the app can be tested without VS Code and
# without touching a production system. See tests/testbed/README.md.
#
# Secrets (the test user's password, the long-lived token, Ingress sessions)
# live only under tests/testbed/.local/ and are never printed by this script.
#
# With HERMES_TESTBED_TARGET=pi, the same commands drive a separate test SSD in a
# real device over HAOS debug SSH instead. See "Testing on a real device" in the
# README. Its settings and secrets stay outside the repository, because the
# repository is mounted into the privileged testbed.

# shellcheck disable=SC2016,SC2029  # Remote scripts expand on the device, on purpose.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Multi-arch index digest of ghcr.io/home-assistant/devcontainer:5-apps.
# Bump it together with .devcontainer.json.
readonly TESTBED_IMAGE="ghcr.io/home-assistant/devcontainer:5-apps@sha256:81ea6e6892125d08e88b5a873c12da00131dfc3a184d335179576ec60bc0ecb0"

readonly TESTBED_NAME="hermes-testbed"
readonly TESTBED_LABEL="io.hermes-webui.testbed=1"
readonly VOLUME_DOCKER="${TESTBED_NAME}-docker"
readonly VOLUME_CONTAINERD="${TESTBED_NAME}-containerd"
readonly VOLUME_SUPERVISOR="${TESTBED_NAME}-supervisor"
readonly TESTBED_VOLUMES=("${VOLUME_DOCKER}" "${VOLUME_CONTAINERD}" "${VOLUME_SUPERVISOR}")

# Same location as "workspaceFolder" in .devcontainer.json.
readonly WORKSPACE_DIRECTORY="/mnt/supervisor/apps/local/${REPO_ROOT##*/}"
readonly SUPERVISOR_CHANNEL="${HERMES_TESTBED_CHANNEL:-stable}"
readonly HOST_BIND="127.0.0.1"
readonly HOST_PORT_CORE="7123"
readonly HOST_PORT_OBSERVER="7357"
# Core under the Supervisor defaults to port 80 on a new install (Core 2026.11).
# Port 8123 only redirects to it until onboarding completes, then it closes.
readonly CONTAINER_PORT_CORE="80"
readonly CONTAINER_PORT_OBSERVER="4357"

readonly TARGET="${HERMES_TESTBED_TARGET:-local}"
if [[ "${TARGET}" != "local" && "${TARGET}" != "pi" ]]; then
  printf '[testbed] ERROR: HERMES_TESTBED_TARGET must be local or pi.\n' >&2
  exit 1
fi
readonly PI_CONFIG_DIR="${HERMES_PI_CONFIG_DIR:-${HOME}/.config/hermes-pi}"
readonly PI_TARGET_FILE="${PI_CONFIG_DIR}/target.env"
readonly PI_KNOWN_HOSTS="${PI_CONFIG_DIR}/known_hosts"
readonly PI_KEY="${HERMES_PI_KEY:-${HOME}/.ssh/hermes-pi-test/id_ed25519}"
readonly PI_SSH_PORT="${HERMES_PI_SSH_PORT:-22222}"
readonly PI_DATA_ROOT="${HERMES_PI_DATA_ROOT:-/mnt/data}"
readonly PI_MARKER_FILE="${PI_DATA_ROOT}/hermes-test-marker"
readonly PI_STUBS_DIR="${PI_DATA_ROOT}/hermes-test-stubs"
readonly PI_MARKER_REFUSED=97
HERMES_PI_HOST=""
HERMES_PI_URL=""
HERMES_PI_MARKER_ID=""
# Only cmd_pi_enroll changes these. An inherited value must never loosen a safeguard.
unset PI_ENROLLING PI_HOST_KEY_POLICY
PI_KNOWN_HOSTS_IN_USE="${PI_KNOWN_HOSTS}"
if [[ "${TARGET}" == "pi" && -f "${PI_TARGET_FILE}" ]]; then
  # shellcheck source=/dev/null
  . "${PI_TARGET_FILE}"
  if [[ -n "${HERMES_PI_MARKER_ID}" && ! "${HERMES_PI_MARKER_ID}" =~ ^[0-9a-f]{32}$ ]]; then
    printf '[testbed] ERROR: The marker in %s is malformed. Enrol the test SSD again.\n' "${PI_TARGET_FILE}" >&2
    exit 1
  fi
fi

if [[ "${TARGET}" == "pi" ]]; then
  TESTBED_URL="${HERMES_PI_URL}"
  SUPERVISOR_ROOT="${PI_DATA_ROOT}/supervisor"
  STUBS_DIR="${PI_STUBS_DIR}"
else
  TESTBED_URL="http://${HOST_BIND}:${HOST_PORT_CORE}"
  SUPERVISOR_ROOT="/mnt/supervisor"
  STUBS_DIR="${WORKSPACE_DIRECTORY}/tests/harness/stubs"
fi
readonly SUPERVISOR_ROOT STUBS_DIR

# The app under test. Override it to drive a throwaway fixture.
readonly APP_SLUG="${HERMES_TESTBED_APP:-local_hermes_webui}"

# Bounded waits, in seconds. The first start downloads several GB.
readonly TIMEOUT_PULL="${HERMES_TESTBED_TIMEOUT_PULL:-1800}"
readonly TIMEOUT_DOCKER="${HERMES_TESTBED_TIMEOUT_DOCKER:-180}"
readonly TIMEOUT_SUPERVISOR="${HERMES_TESTBED_TIMEOUT_SUPERVISOR:-900}"
readonly TIMEOUT_CORE="${HERMES_TESTBED_TIMEOUT_CORE:-2400}"
readonly TIMEOUT_STOP="${HERMES_TESTBED_TIMEOUT_STOP:-180}"
readonly TIMEOUT_BUILD="${HERMES_TESTBED_TIMEOUT_BUILD:-7200}"

# The stub model. It runs from the installed app's image, which has Python.
readonly MODEL_STUB_NAME="hermes-testbed-model-stub"
readonly MODEL_STUB_IP="172.30.33.250"

if [[ "${TARGET}" == "pi" ]]; then
  readonly LOCAL_DIR="${PI_CONFIG_DIR}/local"
else
  readonly LOCAL_DIR="${SCRIPT_DIR}/.local"
fi
readonly CREDENTIALS_FILE="${LOCAL_DIR}/credentials.json"
readonly INGRESS_COOKIE_FILE="${LOCAL_DIR}/ingress-cookies.txt"
readonly SUPERVISOR_RUN_LOG="/tmp/supervisor_run.log"

log() { printf '[testbed] %s\n' "$*"; }
warn() { printf '[testbed] WARNING: %s\n' "$*" >&2; }
die() {
  printf '[testbed] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<EOF
Usage: $(basename "$0") <command> [arguments]

Testbed lifecycle:
  up                     Create or start the testbed, wait for Core, onboard it
  down                   Stop the testbed, keep its volumes
  status                 Show container, Supervisor, Core and API state
  reset [--yes]          Delete the testbed container, its volumes and credentials

App (${APP_SLUG}; override with HERMES_TESTBED_APP):
  install                Reload the store and install the app (local build)
  rebuild                Reload the store and rebuild the app (--force)
  update                 Reload the store and update the app to a new version in its folder
  start | stop           Start or stop the app
  logs [ha-cli flags]    Show the app log
  backup [name]          Create a partial backup of the app and report its contents
  backup-report <slug>   Report the contents of an existing backup
  restore <slug>         Restore the app from a backup
  options                Show the app's options and watchdog switch
  options '<json>' [--watchdog true|false]
                         Set options (validated by the Supervisor), watchdog, or both
  options --watchdog true|false
  ingress-url            Create an Ingress session; print the URL and a cookie file

Stub model, for chats without a paid key:
  model-stub start|stop  Run tests/harness/stubs/model_stub.py on the hassio network at ${MODEL_STUB_IP}

Helpers:
  ha <args>              Run the ha CLI inside the testbed
  api <METHOD> <PATH> [json]
                         Authenticated Core REST call (token stays out of the output)
  supervisor-api <METHOD> <ENDPOINT> [json]
                         Supervisor API call through Core's WebSocket API
  ws <TYPE> [json]       Any call to Core's WebSocket API, e.g. persistent_notification/get;
                         the json object holds the call's other fields
  onboard                Run or finish onboarding (up does this for you)

  on-host <command...>   Run a command on the testbed's host or the device, as root
  supervisor-root        Print the Supervisor's data folder there
  local-dir              Print the folder that holds the credentials and cookies

A real device, with HERMES_TESTBED_TARGET=pi (down and reset are refused):
  pi-enroll <host> --test-ssd-is-in
                         First contact: pin the host key, check that the system is
                         fresh, and write the marker that every later command checks
  pi-facts               Print the device's facts, such as architecture and page size
  pi-deploy              Copy the app from the last commit to the device's local apps
  pi-install             Install the app, wait for the build, and report how long it took

Environment:
  HERMES_TESTBED_APP      App slug (default: local_hermes_webui)
  HERMES_TESTBED_CHANNEL  Supervisor channel for a new testbed (default: stable)
  HERMES_TESTBED_TARGET   local (default) or pi
EOF
}

is_pi() {
  [[ "${TARGET}" == "pi" ]]
}

# -F /dev/null and the empty global file: no ssh_config of this machine can add trusted keys or hosts.
pi_ssh_options() {
  printf '%s\n' -F /dev/null -i "${PI_KEY}" -p "${PI_SSH_PORT}" -o BatchMode=yes -o IdentitiesOnly=yes \
    -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 -o LogLevel=ERROR \
    -o "UserKnownHostsFile=${PI_KNOWN_HOSTS_IN_USE}" -o GlobalKnownHostsFile=/dev/null \
    -o "StrictHostKeyChecking=${PI_HOST_KEY_POLICY:-yes}"
}

# Quotes for a POSIX shell, because the device's shell may have no $'...' as printf %q can produce.
sh_quote() {
  local argument quoted="" single="'" escaped="'\\''"
  for argument in "$@"; do
    quoted+=" '${argument//${single}/${escaped}}'"
  done
  printf '%s' "${quoted}"
}

# Every command checks the marker on the device first, in the same connection. The host key is
# checked by ssh itself. So a command can't reach a device that isn't the enrolled test SSD.
pi_remote_script() {
  local guard=""
  if [[ -z "${PI_ENROLLING:-}" ]]; then
    [[ "${HERMES_PI_MARKER_ID}" =~ ^[0-9a-f]{32}$ ]] || die "No valid marker, so no command goes to the device."
    guard="[ \"\$(cat$(sh_quote "${PI_MARKER_FILE}") 2>/dev/null)\" =$(sh_quote "${HERMES_PI_MARKER_ID}") ] || { echo 'This is not the enrolled test SSD.' >&2; exit ${PI_MARKER_REFUSED}; }; "
  fi
  printf '%s' "${guard}exec$(sh_quote "$@")"
}

pi_run() {
  local stdin_flag="$1" ssh_options script rc=0
  shift
  # Built first: a failed substitution inside the ssh call would still run ssh, with an empty command.
  script="$(pi_remote_script "$@")" || exit 1
  mapfile -t ssh_options < <(pi_ssh_options)
  # shellcheck disable=SC2086  # The flag is either -n or empty.
  ssh "${ssh_options[@]}" ${stdin_flag} "root@${HERMES_PI_HOST}" "${script}" || rc=$?
  if ((rc == PI_MARKER_REFUSED)); then
    die "The device has no matching marker, so it isn't the enrolled test SSD. Nothing was run."
  fi
  return "${rc}"
}

# Runs a command on the host of the app: the testbed container, or the device.
on_host() {
  if is_pi; then
    pi_run -n "$@"
  else
    docker exec "${TESTBED_NAME}" "$@"
  fi
}

# The same, with this script's standard input passed on.
on_host_with_stdin() {
  if is_pi; then
    pi_run "" "$@"
  else
    docker exec -i "${TESTBED_NAME}" "$@"
  fi
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "docker is not installed."
  local output
  if ! output="$(docker version --format '{{.Server.Version}}' 2>&1)"; then
    printf '%s\n' "${output}" >&2
    die "Cannot talk to the Docker daemon. On this WSL setup, check the socket ACL."
  fi
}

container_state() {
  docker container inspect --format '{{.State.Status}}' "${TESTBED_NAME}" 2>/dev/null || true
}

require_running() {
  if is_pi; then
    [[ -n "${HERMES_PI_HOST}" && -n "${HERMES_PI_MARKER_ID}" && -n "${HERMES_PI_URL}" ]] ||
      die "The test SSD isn't enrolled yet. Run: $0 pi-enroll <host> --test-ssd-is-in"
    on_host true || die "The device doesn't answer over SSH. Is the test SSD in, and has it finished starting?"
    return 0
  fi
  require_docker
  [[ "$(container_state)" == "running" ]] || die "The testbed is not running. Run: $0 up"
}

# Runs a command inside the testbed as the devcontainer's vscode user.
in_testbed() {
  if is_pi; then
    on_host "$@"
  else
    docker exec -u vscode -w / "${TESTBED_NAME}" "$@"
  fi
}

# Calls the CLI container directly: the image's /usr/bin/ha wrapper uses an
# unquoted $* and would split arguments that contain spaces.
ha_cli() {
  in_testbed docker exec hassio_cli ha "$@"
}

# Creates the folder for local secrets. The .gitignore is written first, because
# the testbed can exist before the git checkout does.
ensure_local_dir() {
  mkdir -p "${LOCAL_DIR}"
  chmod 700 "${LOCAL_DIR}"
  if [[ ! -f "${LOCAL_DIR}/.gitignore" ]]; then
    printf '*\n' >"${LOCAL_DIR}/.gitignore"
  fi
}

create_container() {
  log "Creating container ${TESTBED_NAME} (privileged; ports on ${HOST_BIND} only)."
  local volume
  for volume in "${TESTBED_VOLUMES[@]}"; do
    docker volume create --label "${TESTBED_LABEL}" "${volume}" >/dev/null
  done
  docker run --detach \
    --name "${TESTBED_NAME}" \
    --hostname "${TESTBED_NAME}" \
    --label "${TESTBED_LABEL}" \
    --privileged \
    --publish "${HOST_BIND}:${HOST_PORT_CORE}:${CONTAINER_PORT_CORE}" \
    --publish "${HOST_BIND}:${HOST_PORT_OBSERVER}:${CONTAINER_PORT_OBSERVER}" \
    --env "WORKSPACE_DIRECTORY=${WORKSPACE_DIRECTORY}" \
    --env "SUPERVISOR_CHANNEL=${SUPERVISOR_CHANNEL}" \
    --mount "type=bind,source=${REPO_ROOT},target=${WORKSPACE_DIRECTORY}" \
    --mount "type=volume,source=${VOLUME_DOCKER},target=/var/lib/docker" \
    --mount "type=volume,source=${VOLUME_CONTAINERD},target=/var/lib/containerd" \
    --mount "type=volume,source=${VOLUME_SUPERVISOR},target=/mnt/supervisor" \
    --mount "type=tmpfs,target=/tmp" \
    "${TESTBED_IMAGE}" >/dev/null
}

wait_for_inner_docker() {
  log "Waiting for the inner Docker daemon (up to ${TIMEOUT_DOCKER}s)."
  local deadline=$((SECONDS + TIMEOUT_DOCKER))
  while ((SECONDS < deadline)); do
    if docker exec "${TESTBED_NAME}" docker info >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  docker logs --tail 40 "${TESTBED_NAME}" >&2 || true
  docker exec "${TESTBED_NAME}" journalctl --no-pager -u docker -n 40 >&2 || true
  die "The inner Docker daemon did not answer within ${TIMEOUT_DOCKER}s."
}

supervisor_container_running() {
  [[ "$(in_testbed docker container inspect --format '{{.State.Status}}' hassio_supervisor 2>/dev/null || true)" == "running" ]]
}

print_supervisor_run_log() {
  docker exec "${TESTBED_NAME}" tail -n 40 "${SUPERVISOR_RUN_LOG}" >&2 || true
}

start_supervisor() {
  if supervisor_container_running; then
    log "The Supervisor is already running."
    return 0
  fi
  log "Starting the Supervisor in the background (log inside the testbed: ${SUPERVISOR_RUN_LOG})."
  docker exec --detach -u vscode -w / "${TESTBED_NAME}" \
    bash -c "exec supervisor_run >>${SUPERVISOR_RUN_LOG} 2>&1"
  local deadline=$((SECONDS + TIMEOUT_SUPERVISOR))
  sleep 2
  while ((SECONDS < deadline)); do
    if supervisor_container_running; then
      log "The Supervisor container is running."
      return 0
    fi
    if ! docker exec "${TESTBED_NAME}" pgrep -f supervisor_run >/dev/null 2>&1; then
      print_supervisor_run_log
      die "supervisor_run exited before the Supervisor came up."
    fi
    sleep 5
  done
  print_supervisor_run_log
  die "The Supervisor did not start within ${TIMEOUT_SUPERVISOR}s."
}

# Prints the HTTP status of the REST root without credentials.
core_api_status() {
  curl --silent --output /dev/null --max-time 5 --write-out '%{http_code}' \
    "${TESTBED_URL}/api/" 2>/dev/null || true
}

# Prints the version the Supervisor reports for Core, or nothing.
core_version() {
  ha_cli core info --raw-json 2>/dev/null |
    python3 -I -c 'import json, sys; print(json.load(sys.stdin)["data"].get("version") or "")' \
      2>/dev/null || true
}

# The landing page, shown while Core is being installed, also answers /api/ with
# 401. Only the Supervisor can tell the two apart.
core_is_up() {
  [[ "$(core_api_status)" == "401" ]] || return 1
  local version
  version="$(core_version)"
  [[ -n "${version}" && "${version}" != "landingpage" ]]
}

print_progress() {
  local elapsed="$1" containers
  containers="$(in_testbed docker ps --format '{{.Names}}' 2>/dev/null | sort | tr '\n' ' ' || true)"
  log "${elapsed}s: Core version '$(core_version)'; API HTTP $(core_api_status); inner containers: ${containers:-none}"
}

wait_for_core() {
  log "Waiting for Home Assistant Core (up to ${TIMEOUT_CORE}s; the first start downloads several GB)."
  local started="${SECONDS}" deadline=$((SECONDS + TIMEOUT_CORE)) next_progress=0
  while ((SECONDS < deadline)); do
    if core_is_up; then
      log "Core $(core_version) answers after $((SECONDS - started))s."
      return 0
    fi
    if ((SECONDS >= next_progress)); then
      print_progress "$((SECONDS - started))"
      next_progress=$((SECONDS + 60))
    fi
    sleep 5
  done
  print_supervisor_run_log
  in_testbed docker logs --tail 40 hassio_supervisor >&2 || true
  die "Core did not answer within ${TIMEOUT_CORE}s."
}

cmd_up() {
  if is_pi; then
    require_running
    wait_for_core
    cmd_onboard
    log "Ready. Core: ${TESTBED_URL}; credentials in ${CREDENTIALS_FILE}."
    return 0
  fi
  require_docker
  ensure_local_dir
  local started="${SECONDS}" state
  state="$(container_state)"
  case "${state}" in
    "")
      log "Pulling ${TESTBED_IMAGE} (up to ${TIMEOUT_PULL}s)."
      timeout "${TIMEOUT_PULL}" docker pull --quiet "${TESTBED_IMAGE}" >/dev/null
      create_container
      ;;
    running)
      log "Container ${TESTBED_NAME} is already running."
      ;;
    *)
      log "Starting the existing container ${TESTBED_NAME} (state: ${state})."
      docker start "${TESTBED_NAME}" >/dev/null
      ;;
  esac
  wait_for_inner_docker
  # Runs on every start: its bind mount does not survive a container restart.
  in_testbed bash devcontainer_bootstrap
  start_supervisor
  wait_for_core
  cmd_onboard
  log "Ready after $((SECONDS - started))s: ${TESTBED_URL}/ (credentials: ${CREDENTIALS_FILE})"
}

cmd_down() {
  is_pi && die "down is for the local testbed. Shut the device down from Home Assistant."
  require_docker
  if [[ "$(container_state)" != "running" ]]; then
    log "The testbed is not running."
    return 0
  fi
  log "Stopping the testbed (up to ${TIMEOUT_STOP}s). Volumes are kept."
  docker stop --time "${TIMEOUT_STOP}" "${TESTBED_NAME}" >/dev/null
  log "Stopped."
}

cmd_status() {
  if is_pi; then
    require_running
    log "Target: test SSD at ${TESTBED_URL}"
    status_details
    return 0
  fi
  require_docker
  local state
  state="$(container_state)"
  log "Container: ${TESTBED_NAME} (${state:-does not exist})"
  local volume
  for volume in "${TESTBED_VOLUMES[@]}"; do
    if docker volume inspect "${volume}" >/dev/null 2>&1; then
      log "Volume: ${volume}"
    fi
  done
  if [[ -f "${CREDENTIALS_FILE}" ]]; then
    log "Credentials: ${CREDENTIALS_FILE}"
  else
    log "Credentials: none yet"
  fi
  [[ "${state}" == "running" ]] || return 0
  log "Image: $(docker container inspect --format '{{.Config.Image}}' "${TESTBED_NAME}")"
  log "Ports: $(docker port "${TESTBED_NAME}" | tr '\n' ' ')"
  status_details
}

status_details() {
  if ! supervisor_container_running; then
    log "Supervisor: not running"
    return 0
  fi
  in_testbed docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  log "Supervisor, Core and system state:"
  ha_cli supervisor info --raw-json | python3 -I -c '
import json, sys
data = json.load(sys.stdin)["data"]
for key in ("version", "channel", "healthy", "supported", "arch", "diagnostics"):
    print(f"  supervisor.{key}: {data.get(key)}")
' || warn "ha supervisor info failed."
  ha_cli core info --raw-json | python3 -I -c '
import json, sys
data = json.load(sys.stdin)["data"]
for key in ("version", "machine", "boot"):
    print(f"  core.{key}: {data.get(key)}")
' || warn "ha core info failed."
  ha_cli resolution info --raw-json | python3 -I -c '
import json, sys
data = json.load(sys.stdin)["data"]
print("  resolution.unsupported:", data.get("unsupported"))
print("  resolution.unhealthy:", data.get("unhealthy"))
' || warn "ha resolution info failed."
  log "Core REST API without credentials: HTTP $(core_api_status)"
  if [[ -f "${CREDENTIALS_FILE}" ]]; then
    ha_helper api GET /api/ >/dev/null && log "Core REST API with the long-lived token: OK"
  fi
}

cmd_reset() {
  is_pi && die "reset is for the local testbed. It never runs on a device."
  require_docker
  log "Reset deletes the following, and nothing else:"
  log "  container: ${TESTBED_NAME}"
  local volume
  for volume in "${TESTBED_VOLUMES[@]}"; do
    log "  volume:    ${volume}"
  done
  log "  files:     ${CREDENTIALS_FILE} and ${INGRESS_COOKIE_FILE}"
  log "This is the whole test instance: Core data, backups, built app images."
  if [[ "${1:-}" != "--yes" ]]; then
    [[ -t 0 ]] || die "Not a terminal. Pass --yes to confirm."
    local answer
    read -r -p "[testbed] Type yes to continue: " answer
    [[ "${answer}" == "yes" ]] || die "Aborted."
  fi
  docker rm --force "${TESTBED_NAME}" >/dev/null 2>&1 || true
  for volume in "${TESTBED_VOLUMES[@]}"; do
    if docker volume inspect "${volume}" >/dev/null 2>&1; then
      docker volume rm "${volume}" >/dev/null
    fi
  done
  rm -f "${CREDENTIALS_FILE}" "${INGRESS_COOKIE_FILE}"
  log "Reset done. Run: $0 up"
}

cmd_install() {
  require_running
  log "Reloading the store, which also updates its remote repositories."
  ha_helper store-reload
  ha_cli apps install "${APP_SLUG}"
}

cmd_rebuild() {
  require_running
  log "Reloading the store, which also updates its remote repositories."
  ha_helper store-reload
  ha_cli apps rebuild --force "${APP_SLUG}"
}

# rebuild refuses when config.yaml carries a new version, so a changed version needs this.
cmd_update() {
  require_running
  log "Reloading the store, which also updates its remote repositories."
  ha_helper store-reload
  ha_cli apps update "${APP_SLUG}"
}

cmd_backup() {
  require_running
  local name="${1:-${APP_SLUG}-$(date +%Y%m%d-%H%M%S)}" output slug
  log "Creating a partial backup of ${APP_SLUG} (name: ${name})."
  output="$(ha_cli backups new --app "${APP_SLUG}" --name "${name}" --raw-json)"
  slug="$(printf '%s' "${output}" | python3 -I -c 'import json, sys; print(json.load(sys.stdin)["data"]["slug"])')"
  log "Backup slug: ${slug}"
  cmd_backup_report "${slug}"
}

# Reports sizes per archive and separates image.tar from the payload, so the
# backup gate can state the size with and without the locally built image.
cmd_backup_report() {
  require_running
  local slug="${1:-}"
  [[ -n "${slug}" ]] || die "Usage: $0 backup-report <slug>"
  [[ "${slug}" =~ ^[a-zA-Z0-9_-]+$ ]] || die "Invalid backup slug."
  local backup_file="${SUPERVISOR_ROOT}/backup/${slug}.tar"
  on_host test -f "${backup_file}" || die "No backup file for slug ${slug}."
  log "Backup file: $(on_host stat -c '%s bytes' "${backup_file}")"
  on_host tar -tvf "${backup_file}" | sed 's/^/  /'
  local inner listing
  while IFS= read -r inner; do
    # Unpacked where the backup is, so that a device doesn't send its image over the network.
    listing="$(on_host sh -c 'tar -xOf "$1" "$2" | tar -tzv 2>/dev/null' sh "${backup_file}" "${inner}")"
    log "Archive ${inner} (largest entries, uncompressed bytes):"
    # sed instead of head: it reads all input, so sort cannot die of SIGPIPE.
    awk '{ print $3, $NF }' <<<"${listing}" | sort -rn | sed -n '1,25s/^/  /p'
    awk '{ total += $3; if ($NF ~ /image\.tar$/) image += $3 }
         END { printf "  Uncompressed: total %d bytes; image.tar %d bytes; payload without image.tar %d bytes\n",
               total, image, total - image }' <<<"${listing}"
  done < <(on_host tar -tf "${backup_file}" | grep -E '\.tar\.gz$' || true)
}

cmd_restore() {
  require_running
  local slug="${1:-}"
  [[ -n "${slug}" ]] || die "Usage: $0 restore <slug>"
  ha_cli backups restore "${slug}" --app "${APP_SLUG}"
}

cmd_options() {
  require_running
  local options="" watchdog=""
  while (($#)); do
    case "$1" in
      --watchdog)
        [[ $# -ge 2 ]] || die "--watchdog needs true or false."
        watchdog="$2"
        shift 2
        ;;
      *)
        options="$1"
        shift
        ;;
    esac
  done
  ha_helper options "${APP_SLUG}" "${options}" "${watchdog}"
}

cmd_model_stub() {
  require_running
  case "${1:-}" in
    start)
      local image
      image="$(in_testbed docker images --format '{{.Repository}}:{{.Tag}}' | grep -E "addon-${APP_SLUG#local_}:" | head -n 1 || true)"
      [[ -n "${image}" ]] || die "No image of ${APP_SLUG} here yet. Run: $0 install"
      in_testbed docker rm --force --volumes "${MODEL_STUB_NAME}" >/dev/null 2>&1 || true
      in_testbed docker run --detach --name "${MODEL_STUB_NAME}" --network hassio --ip "${MODEL_STUB_IP}" \
        --mount "type=bind,source=${STUBS_DIR},target=/stubs,readonly" \
        --entrypoint /opt/hermes/.venv/bin/python "${image}" -I /stubs/model_stub.py \
        --port 9999 --delay 0.5 --chunks 8 --log /tmp/model_stub.jsonl >/dev/null
      log "Model stub running at http://${MODEL_STUB_IP}:9999/v1 (from ${image})."
      ;;
    stop)
      in_testbed docker rm --force --volumes "${MODEL_STUB_NAME}" >/dev/null 2>&1 || true
      log "Model stub removed."
      ;;
    *) die "Usage: $0 model-stub start|stop" ;;
  esac
}

# Embedded helper for everything that speaks HTTP or WebSocket to Core. It uses
# the standard library only and never writes a credential to stdout or stderr.
# ---- A real device. ----

pi_core_url_candidates() {
  if [[ -n "${HERMES_PI_PROBE_URLS:-}" ]]; then
    printf '%s\n' "${HERMES_PI_PROBE_URLS}" | tr ' ' '\n'
  else
    # 8123 on the user's HAOS, 80 on some new installs. Both are probed in the app, too.
    printf 'http://%s:8123\nhttp://%s:80\n' "${HERMES_PI_HOST}" "${HERMES_PI_HOST}"
  fi
}

pi_refuse_used_system() {
  die "$1 So this may not be a fresh test SSD. Stopping before anything is changed. Take the USB stick out of the device if this is your real system."
}

cmd_pi_enroll() {
  is_pi || die "pi-enroll needs HERMES_TESTBED_TARGET=pi."
  local host="${1:-}" confirmation="${2:-}"
  [[ -n "${host}" && "${confirmation}" == "--test-ssd-is-in" ]] ||
    die "Usage: $0 pi-enroll <host> --test-ssd-is-in"
  [[ -z "${HERMES_PI_MARKER_ID}" ]] ||
    die "Already enrolled. For a newly installed SSD, remove ${PI_CONFIG_DIR} first."
  [[ -f "${PI_KEY}" ]] || die "No SSH key at ${PI_KEY}."
  command -v python3 >/dev/null 2>&1 || die "python3 is not installed."
  install -d -m 0700 "${PI_CONFIG_DIR}"

  HERMES_PI_HOST="${host}"
  # ssh stores a new host key before it authenticates. So the key goes to a separate file that only
  # becomes the pinned one after every check has passed, and a refused system leaves nothing pinned.
  PI_KNOWN_HOSTS_IN_USE="${PI_KNOWN_HOSTS}.enrolling"
  rm -f "${PI_KNOWN_HOSTS_IN_USE}" "${PI_KNOWN_HOSTS}"
  PI_ENROLLING=1
  PI_HOST_KEY_POLICY=accept-new
  local machine
  machine="$(on_host uname -m)" ||
    die "SSH was refused or the device doesn't answer. That's what your real system does, because it has no key for this. Check that the test SSD and the stick are in."
  [[ "${machine}" == "aarch64" || -n "${HERMES_PI_ANY_ARCH:-}" ]] || die "The device reports ${machine}, not aarch64."
  PI_HOST_KEY_POLICY=yes
  log "SSH works, and the device reports ${machine}."

  log "Waiting for Home Assistant Core (up to ${TIMEOUT_CORE}s; the first start downloads a lot)."
  local deadline=$((SECONDS + TIMEOUT_CORE)) url="" candidate
  while ((SECONDS < deadline)); do
    if on_host docker ps --format '{{.Names}}' 2>/dev/null | grep -qx hassio_cli; then
      local version
      version="$(core_version)"
      if [[ -n "${version}" && "${version}" != "landingpage" ]]; then
        while IFS= read -r candidate; do
          if [[ "$(curl --silent --output /dev/null --max-time 5 --write-out '%{http_code}' "${candidate}/api/" || true)" == "401" ]]; then
            url="${candidate}"
            break
          fi
        done < <(pi_core_url_candidates)
        [[ -z "${url}" ]] || break
      fi
    fi
    sleep 10
  done
  [[ -n "${url}" ]] || die "Core did not answer within ${TIMEOUT_CORE}s."
  TESTBED_URL="${url}"
  log "Core answers at ${url}."

  local onboarding
  onboarding="$(curl --silent --max-time 10 "${url}/api/onboarding" || true)"
  printf '%s' "${onboarding}" | python3 -I -c '
import json, sys
steps = json.load(sys.stdin)
sys.exit(0 if isinstance(steps, list) and any(s.get("step") == "user" and not s.get("done") for s in steps) else 1)
' 2>/dev/null || pi_refuse_used_system "Core already has a user."
  local counts
  counts="$(ha_cli apps --raw-json 2>/dev/null | python3 -I -c '
import json, sys
data = json.load(sys.stdin)["data"]
print(len([a for a in data.get("apps", data.get("addons", [])) if a.get("installed", True)]))
' || true)"
  [[ "${counts}" == "0" ]] || pi_refuse_used_system "It has installed apps (${counts:-unknown})."
  counts="$(ha_cli backups --raw-json 2>/dev/null | python3 -I -c '
import json, sys
print(len(json.load(sys.stdin)["data"].get("backups", [])))
' || true)"
  [[ "${counts}" == "0" ]] || pi_refuse_used_system "It has backups (${counts:-unknown})."
  log "The system is fresh: no user, no apps, no backups."

  local marker
  marker="$(python3 -I -c 'import secrets; print(secrets.token_hex(16))')"
  on_host sh -c 'umask 077; printf %s "$1" > "$2"' sh "${marker}" "${PI_MARKER_FILE}"
  (
    umask 077
    printf 'HERMES_PI_HOST=%q\nHERMES_PI_URL=%q\nHERMES_PI_MARKER_ID=%q\n' "${host}" "${url}" "${marker}" >"${PI_TARGET_FILE}"
  )
  mv "${PI_KNOWN_HOSTS_IN_USE}" "${PI_KNOWN_HOSTS}"
  PI_KNOWN_HOSTS_IN_USE="${PI_KNOWN_HOSTS}"
  log "Enrolled, and the host key is pinned. Every later command checks the host key and this marker first."
  log "Now take the USB stick out of the device, and keep it away from your real system."
}

cmd_pi_facts() {
  is_pi || die "pi-facts needs HERMES_TESTBED_TARGET=pi."
  require_running
  on_host sh -c '
echo "machine: $(uname -m)"
echo "kernel: $(uname -r)"
page="$(getconf PAGESIZE 2>/dev/null)"
[ -n "$page" ] || page="$(awk "/KernelPageSize/ {print \$2 * 1024; exit}" /proc/self/smaps 2>/dev/null)"
echo "page size: $page"
echo "model: $(tr -d "\000" </proc/device-tree/model 2>/dev/null)"
echo "protected_hardlinks: $(cat /proc/sys/fs/protected_hardlinks 2>/dev/null)"
echo "protected_symlinks: $(cat /proc/sys/fs/protected_symlinks 2>/dev/null)"
echo "apparmor enabled: $(cat /sys/module/apparmor/parameters/enabled 2>/dev/null)"
echo "memory kB: $(awk "/MemTotal/ {print \$2}" /proc/meminfo)"
echo "free on data, kB: $(df -k "$1" | awk "NR==2 {print \$4}")"
' sh "${PI_DATA_ROOT}"
  ha_cli os info --raw-json 2>/dev/null | python3 -I -c '
import json, sys
data = json.load(sys.stdin)["data"]
print("haos:", data.get("version"), "| board:", data.get("board"))
' || warn "ha os info failed."
  status_details
}

# Copies what's committed, so that a test always names one exact state.
cmd_pi_deploy() {
  is_pi || die "pi-deploy needs HERMES_TESTBED_TARGET=pi."
  require_running
  local folder
  folder="$(on_host sh -c 'for d in apps/local addons/local; do if [ -d "$1/$d" ]; then echo "$d"; exit 0; fi; done; exit 1' sh "${SUPERVISOR_ROOT}")" ||
    die "No folder for local apps under ${SUPERVISOR_ROOT}."
  git -C "${REPO_ROOT}" archive HEAD hermes_webui |
    on_host_with_stdin sh -c 'rm -rf "$1/hermes_webui" && tar -xf - -C "$1"' sh "${SUPERVISOR_ROOT}/${folder}"
  git -C "${REPO_ROOT}" archive HEAD tests/harness/stubs |
    on_host_with_stdin sh -c 'rm -rf "$1" && mkdir -p "$1" && tar -xf - -C "$1" --strip-components=3' sh "${STUBS_DIR}"
  log "Copied commit $(git -C "${REPO_ROOT}" rev-parse --short HEAD) to ${folder}/hermes_webui."
  git -C "${REPO_ROOT}" diff --quiet HEAD -- hermes_webui tests/harness/stubs ||
    warn "The working tree differs from that commit, and the differences were not copied."
}

# The ID of the image the Supervisor built for the app, or nothing.
app_image_id() {
  in_testbed docker image ls --no-trunc --format '{{.ID}}' --filter "reference=local/*-addon-${APP_SLUG#local_}" 2>/dev/null | head -n 1
}

app_version_and_state() {
  ha_cli apps info "${APP_SLUG}" --raw-json 2>/dev/null | python3 -I -c '
import json, sys
data = json.load(sys.stdin)["data"]
print(data.get("version") or "", data.get("state") or "")
' 2>/dev/null || true
}

# Installs the app, or rebuilds it when it's already there, so that a second run tests the newly
# copied files and not the old image. A build on a device can outlast the ha CLI's own time limit
# (1 h for install, 10 min for rebuild), while the Supervisor carries on.
cmd_pi_install() {
  is_pi || die "pi-install needs HERMES_TESTBED_TARGET=pi."
  require_running
  log "Reloading the store."
  ha_helper store-reload
  local image_before image_now action cli_limit cli_started rc=0 state="" must_change=""
  image_before="$(app_image_id)"
  if [[ -n "${image_before}" ]]; then
    action=(rebuild --force)
    cli_limit=600
  else
    action=(install)
    cli_limit=3600
  fi
  local started="${SECONDS}" deadline=$((SECONDS + TIMEOUT_BUILD))
  log "Running ${action[0]} for ${APP_SLUG}. The device builds it, so this takes a while (up to ${TIMEOUT_BUILD}s)."
  cli_started="${SECONDS}"
  ha_cli apps "${action[@]}" "${APP_SLUG}" || rc=$?
  if ((rc != 0)); then
    # An answer well before the CLI's time limit is the Supervisor's own refusal or a failed build.
    ((SECONDS - cli_started >= cli_limit - 60)) ||
      die "The ${action[0]} failed after $((SECONDS - cli_started))s, before the ha CLI's time limit. See the message above."
    warn "The ha CLI gave up after its time limit. Waiting for the Supervisor's build."
    # Without the CLI's answer, only a new image shows that the build has finished.
    must_change=1
  fi
  while ((SECONDS < deadline)); do
    state="$(app_version_and_state)"
    image_now="$(app_image_id)"
    if [[ ("${state}" == *" started" || "${state}" == *" stopped") && -n "${image_now}" ]] &&
      [[ -z "${must_change}" || "${image_now}" != "${image_before}" ]]; then
      break
    fi
    state=""
    sleep 15
  done
  [[ -n "${state}" ]] || die "The ${action[0]} didn't finish within ${TIMEOUT_BUILD}s."
  if [[ "${image_now}" == "${image_before}" ]]; then
    log "The image is unchanged, because the app's files are the same as at the last build."
  else
    log "The image is new."
  fi
  log "Finished ${action[0]} after $((SECONDS - started))s (measured): ${state}"
}

ha_helper() {
  HELPER_URL="${TESTBED_URL}" \
    HELPER_CREDENTIALS="${CREDENTIALS_FILE}" \
    HELPER_COOKIES="${INGRESS_COOKIE_FILE}" \
    python3 -I - "$@" <<'PYTHON'
import base64
import json
import os
import secrets
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = os.environ["HELPER_URL"]
CREDENTIALS = Path(os.environ["HELPER_CREDENTIALS"])
COOKIES = Path(os.environ["HELPER_COOKIES"])
ONBOARDING_WAIT_SECONDS = 180
# A store reload pulls every remote repository first. That can exceed both the ha
# CLI's 30-second client timeout and Core's 10-second default for this API.
STORE_RELOAD_TIMEOUT_SECONDS = 300
CLIENT_ID = f"{BASE_URL}/"
REDIRECT_URI = f"{BASE_URL}/?auth_callback=1"
TEST_USER = {"name": "Hermes Test", "username": "hermes-test"}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class HelperError(Exception):
    """An error whose message is safe to show."""


def write_private(path, text):
    if not (path.parent / ".gitignore").is_file():
        raise HelperError(f"{path.parent}/.gitignore is missing; refusing to store secrets.")
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def load_credentials():
    if not CREDENTIALS.is_file():
        return {}
    return json.loads(CREDENTIALS.read_text(encoding="utf-8"))


def save_credentials(credentials):
    write_private(CREDENTIALS, json.dumps(credentials, indent=2) + "\n")


def long_lived_token():
    token = load_credentials().get("long_lived_token")
    if not token:
        raise HelperError("No long-lived token stored. Run the up or onboard command first.")
    return token


def http(method, path, body=None, form=None, token=None, timeout=60):
    headers = {}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with OPENER.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except OSError as error:
        raise HelperError(f"{method} {path} failed: {error}") from None
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def expect_ok(result, what):
    status, payload = result
    if status >= 300:
        # Only a server-provided message, never the request or the whole body.
        detail = payload.get("message") if isinstance(payload, dict) else None
        raise HelperError(f"{what} failed with HTTP {status}: {detail or 'no message'}")
    return payload


class WebSocket:
    """A minimal RFC 6455 client: unfragmented sends, no compression, no TLS."""

    def __init__(self, token, timeout=STORE_RELOAD_TIMEOUT_SECONDS + 30):
        url = urllib.parse.urlsplit(BASE_URL)
        self.sock = socket.create_connection((url.hostname, url.port), timeout=timeout)
        self.buffer = b""
        self.next_id = 1
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall(
            (
                "GET /api/websocket HTTP/1.1\r\n"
                f"Host: {url.hostname}:{url.port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise HelperError("WebSocket handshake: connection closed.")
            head += chunk
        head, self.buffer = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise HelperError("WebSocket handshake refused.")
        if self.receive().get("type") != "auth_required":
            raise HelperError("WebSocket: unexpected greeting.")
        self.send({"type": "auth", "access_token": token})
        if self.receive().get("type") != "auth_ok":
            raise HelperError("WebSocket authentication failed.")

    def _read(self, count):
        while len(self.buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise HelperError("WebSocket: connection closed.")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def _write_frame(self, opcode, payload):
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send(self, message):
        self._write_frame(0x1, json.dumps(message).encode())

    def receive(self):
        message = b""
        while True:
            first, second = self._read(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(length)
            if opcode == 0x8:
                raise HelperError("WebSocket: closed by the server.")
            if opcode == 0x9:
                self._write_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if first & 0x80:
                return json.loads(message)

    def call(self, message_type, **fields):
        message_id = self.next_id
        self.next_id += 1
        self.send({"id": message_id, "type": message_type, **fields})
        while True:
            reply = self.receive()
            if reply.get("id") == message_id and reply.get("type") == "result":
                if reply.get("success"):
                    return reply.get("result")
                error = reply.get("error") or {}
                raise HelperError(
                    f"{message_type} failed: {error.get('code')}: {error.get('message')}"
                )

    def supervisor(self, method, endpoint, data=None, timeout=None):
        fields = {"endpoint": endpoint, "method": method}
        if data is not None:
            fields["data"] = data
        if timeout is not None:
            fields["timeout"] = timeout
        return self.call("supervisor/api", **fields)

    def close(self):
        self.sock.close()


def token_from_code(code):
    payload = expect_ok(
        http(
            "POST",
            "/auth/token",
            form={"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID},
        ),
        "Token exchange",
    )
    return payload["access_token"]


def login(credentials):
    flow = expect_ok(
        http(
            "POST",
            "/auth/login_flow",
            body={
                "client_id": CLIENT_ID,
                "handler": ["homeassistant", None],
                "redirect_uri": REDIRECT_URI,
            },
        ),
        "Starting the login flow",
    )
    result = expect_ok(
        http(
            "POST",
            f"/auth/login_flow/{flow['flow_id']}",
            body={
                "client_id": CLIENT_ID,
                "username": credentials["username"],
                "password": credentials["password"],
            },
        ),
        "Login",
    )
    if result.get("type") != "create_entry":
        raise HelperError("Login was refused. The stored password does not match this Core.")
    return token_from_code(result["result"])


def pending_steps(have_credentials):
    """Return the onboarding steps still open; empty once Core has finished.

    Core drops the view once onboarding is complete, so 404 means "finished".
    It also means "not set up yet" for a moment while Core starts, so without
    stored credentials a 404 only counts when it persists.
    """
    deadline = time.monotonic() + ONBOARDING_WAIT_SECONDS
    while True:
        try:
            status, payload = http("GET", "/api/onboarding")
        except HelperError:
            status, payload = None, None
        if status == 200 and isinstance(payload, list):
            return [step["step"] for step in payload if not step["done"]]
        if status == 404 and have_credentials:
            return []
        if time.monotonic() >= deadline:
            if status == 404:
                return []
            raise HelperError(f"Unexpected answer from /api/onboarding: HTTP {status}")
        time.sleep(3)


def cmd_onboard():
    credentials = load_credentials()
    pending = pending_steps(bool(credentials))
    if "user" in pending:
        credentials = {
            **TEST_USER,
            "password": secrets.token_urlsafe(24),
            "url": BASE_URL,
        }
        # Stored first, so that a failure after this point can be resumed.
        save_credentials(credentials)
        payload = expect_ok(
            http(
                "POST",
                "/api/onboarding/users",
                body={"client_id": CLIENT_ID, "language": "en", **TEST_USER,
                      "password": credentials["password"]},
            ),
            "Creating the test user",
        )
        access_token = token_from_code(payload["auth_code"])
        print("Onboarding: created the test user.")
    elif not pending and credentials.get("long_lived_token"):
        if http("GET", "/api/", token=credentials["long_lived_token"])[0] == 200:
            print("Onboarding: already complete, stored token works.")
            return
        raise HelperError("The stored token is rejected. The credentials do not match this Core.")
    elif credentials.get("password"):
        access_token = login(credentials)
    else:
        raise HelperError(
            "Core already has an owner, but no credentials are stored. Use the reset command."
        )

    # Marks the steps done. No location is sent, so Core keeps its defaults.
    if "core_config" in pending:
        expect_ok(http("POST", "/api/onboarding/core_config", body={}, token=access_token),
                  "Onboarding step core_config")
    if "analytics" in pending:
        expect_ok(http("POST", "/api/onboarding/analytics", body={}, token=access_token),
                  "Onboarding step analytics")
    if "integration" in pending:
        expect_ok(
            http(
                "POST",
                "/api/onboarding/integration",
                body={"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI},
                token=access_token,
            ),
            "Onboarding step integration",
        )

    socket_client = WebSocket(access_token)
    try:
        socket_client.call(
            "analytics/preferences",
            preferences={"base": False, "usage": False, "statistics": False, "diagnostics": False},
        )
        analytics = socket_client.call("analytics")
        if any(analytics.get("preferences", {}).values()):
            raise HelperError("Analytics could not be turned off.")
        if not credentials.get("long_lived_token"):
            credentials["long_lived_token"] = socket_client.call(
                "auth/long_lived_access_token", client_name="hermes-testbed", lifespan=3650
            )
            save_credentials(credentials)
    finally:
        socket_client.close()

    if http("GET", "/api/", token=credentials["long_lived_token"])[0] != 200:
        raise HelperError("The new long-lived token does not work.")
    print(f"Onboarding: complete. Analytics off. Credentials stored in {CREDENTIALS}.")


def parse_json_argument(text, what):
    try:
        return json.loads(text)
    except ValueError as error:
        raise HelperError(f"{what} is not valid JSON: {error}") from None


def cmd_api(method, path, body=None):
    parsed = parse_json_argument(body, "The body") if body else None
    status, payload = http(method.upper(), path, body=parsed, token=long_lived_token())
    print(f"HTTP {status}", file=sys.stderr)
    print(json.dumps(payload, indent=2) if isinstance(payload, (dict, list)) else payload)
    if status >= 400:
        raise SystemExit(1)


def cmd_supervisor_api(method, endpoint, body=None):
    parsed = parse_json_argument(body, "The body") if body else None
    socket_client = WebSocket(long_lived_token())
    try:
        print(json.dumps(socket_client.supervisor(method.lower(), endpoint, parsed), indent=2))
    finally:
        socket_client.close()


def cmd_ws(message_type, fields=None):
    parsed = parse_json_argument(fields, "The fields") if fields else {}
    if not isinstance(parsed, dict):
        raise HelperError("The fields must be a JSON object.")
    socket_client = WebSocket(long_lived_token())
    try:
        print(json.dumps(socket_client.call(message_type, **parsed), indent=2))
    finally:
        socket_client.close()


def cmd_store_reload():
    socket_client = WebSocket(long_lived_token())
    try:
        socket_client.supervisor(
            "post", "/store/reload", timeout=STORE_RELOAD_TIMEOUT_SECONDS
        )
    finally:
        socket_client.close()
    print("Store reloaded.")


def cmd_options(slug, options, watchdog):
    data = {}
    if options:
        data["options"] = parse_json_argument(options, "The options value")
    if watchdog:
        if watchdog not in ("true", "false"):
            raise HelperError("--watchdog needs true or false.")
        data["watchdog"] = watchdog == "true"
    socket_client = WebSocket(long_lived_token())
    try:
        if data:
            socket_client.supervisor("post", f"/addons/{slug}/options", data)
            print("Options accepted by the Supervisor.")
        info = socket_client.supervisor("get", f"/addons/{slug}/info")
    finally:
        socket_client.close()
    shown = {key: info.get(key) for key in ("options", "watchdog", "state")}
    # The option can hold a long-lived token, which must not reach the terminal or a log.
    if isinstance(shown["options"], dict) and shown["options"].get("homeassistant_token"):
        shown["options"]["homeassistant_token"] = "<set>"
    print(json.dumps(shown, indent=2))


def cmd_ingress_url(slug):
    socket_client = WebSocket(long_lived_token())
    try:
        info = socket_client.supervisor("get", f"/addons/{slug}/info")
        if not info.get("ingress_url"):
            raise HelperError(f"{slug} has no Ingress URL (is it installed, with ingress: true?).")
        session = socket_client.supervisor("post", "/ingress/session")["session"]
    finally:
        socket_client.close()
    host = urllib.parse.urlsplit(BASE_URL).hostname
    # Netscape cookie format, for curl --cookie.
    write_private(
        COOKIES,
        "# Netscape HTTP Cookie File\n"
        f"{host}\tFALSE\t/api/hassio_ingress/\tFALSE\t0\tingress_session\t{session}\n",
    )
    print(f"URL: {BASE_URL}{info['ingress_url']}")
    print(f"Cookie file (for curl --cookie): {COOKIES}")


COMMANDS = {
    "onboard": cmd_onboard,
    "api": cmd_api,
    "supervisor-api": cmd_supervisor_api,
    "ws": cmd_ws,
    "store-reload": cmd_store_reload,
    "options": cmd_options,
    "ingress-url": cmd_ingress_url,
}

try:
    COMMANDS[sys.argv[1]](*sys.argv[2:])
except HelperError as error:
    print(f"[testbed] ERROR: {error}", file=sys.stderr)
    raise SystemExit(1) from None
except TypeError as error:
    print(f"[testbed] ERROR: wrong arguments: {error}", file=sys.stderr)
    raise SystemExit(2) from None
PYTHON
}

cmd_onboard() {
  ensure_local_dir
  ha_helper onboard
  # The Supervisor otherwise keeps the time zone it detected from this machine's
  # IP address. It treats plain "UTC" as unset and detects again, so use Etc/UTC.
  ha_cli supervisor options --timezone Etc/UTC >/dev/null
}

main() {
  local command="${1:-}"
  [[ $# -eq 0 ]] || shift
  case "${command}" in
    up) cmd_up ;;
    down) cmd_down ;;
    status) cmd_status ;;
    reset) cmd_reset "$@" ;;
    install) cmd_install ;;
    rebuild) cmd_rebuild ;;
    update) cmd_update ;;
    start)
      require_running
      ha_cli apps start "${APP_SLUG}"
      ;;
    stop)
      require_running
      ha_cli apps stop "${APP_SLUG}"
      ;;
    logs)
      require_running
      ha_cli apps logs "${APP_SLUG}" "$@"
      ;;
    backup) cmd_backup "$@" ;;
    backup-report) cmd_backup_report "$@" ;;
    restore) cmd_restore "$@" ;;
    options) cmd_options "$@" ;;
    model-stub) cmd_model_stub "$@" ;;
    on-host)
      require_running
      on_host "$@"
      ;;
    supervisor-root) printf '%s\n' "${SUPERVISOR_ROOT}" ;;
    local-dir) printf '%s\n' "${LOCAL_DIR}" ;;
    pi-enroll) cmd_pi_enroll "$@" ;;
    pi-facts) cmd_pi_facts ;;
    pi-deploy) cmd_pi_deploy ;;
    pi-install) cmd_pi_install ;;
    ingress-url)
      require_running
      ensure_local_dir
      ha_helper ingress-url "${APP_SLUG}"
      ;;
    ha)
      require_running
      ha_cli "$@"
      ;;
    api)
      require_running
      ha_helper api "$@"
      ;;
    supervisor-api)
      require_running
      ha_helper supervisor-api "$@"
      ;;
    ws)
      require_running
      ha_helper ws "$@"
      ;;
    onboard)
      require_running
      cmd_onboard
      ;;
    "" | -h | --help | help) usage ;;
    *)
      usage >&2
      die "Unknown command: ${command}"
      ;;
  esac
}

main "$@"
