#!/command/with-contenv bash
# shellcheck shell=bash
# Runs once at each start, as root, before any service: validates the options, prepares the data
# folders for the runtime user, chooses the Home Assistant access and renders the nginx parts.
# Everything that touches files under /config runs as the runtime user or refuses symlinks, because
# that folder is writable by the agent and this script is root. For the same reason, tools running as
# root take no config from there or from /tmp: HOME is /root (see the Dockerfile) and curl gets -q.
set -euo pipefail

readonly OPTIONS=/data/options.json
readonly HOME_DIR=/config/hermes
readonly RUN_DIR=/run/hermes-app
readonly APP_DIR=/opt/hermes-app
readonly MARKER=/data/env_vars_names.json
readonly MANAGED_CONFIG=/etc/hermes/config.yaml
readonly PYTHON=/opt/hermes/.venv/bin/python

# Numeric levels for the log_level option; until it is read, everything shows.
LOG_THRESHOLD=0
log_at() {
  local level="$1" number="$2"
  shift 2
  if ((number >= LOG_THRESHOLD)); then
    printf '[hermes-init] %s%s\n' "$level" "$*"
  fi
  return 0
}
log_debug() { log_at "" 0 "$@"; }
log_info() { log_at "" 1 "$@"; }
log_warning() { log_at "WARNING: " 2 "$@"; }
fail() {
  printf '[hermes-init] ERROR: %s\n' "$*" >&2
  exit 1
}

as_hermes() { s6-setuidgid hermes "$@"; }

# Sends the helper's WARNING and ERROR lines through the log functions, and keeps its exit code.
env_sync() {
  local output line status=0
  output="$("$PYTHON" -I "$APP_DIR/lib/env_sync.py" "$@" 2>&1)" || status=$?
  while IFS= read -r line; do
    case "$line" in
      "") ;;
      WARNING:*) log_warning "${line#WARNING: }" ;;
      ERROR:*) printf '[hermes-init] %s\n' "$line" >&2 ;;
      *) log_info "$line" ;;
    esac
  done <<<"$output"
  return "$status"
}

# ---- 1. Options. The Supervisor checks the schema; this adds what a schema can't say. ----

[[ -r "$OPTIONS" ]] || fail "$OPTIONS is missing."
jq -e 'type == "object"' "$OPTIONS" >/dev/null || fail "$OPTIONS is not a valid JSON object."
opt() { jq -r "$1" "$OPTIONS"; }

log_level="$(opt '.log_level // "info"')"
case "$log_level" in
  debug) LOG_THRESHOLD=0 managed_level=DEBUG nginx_level=info ;;
  info) LOG_THRESHOLD=1 managed_level=INFO nginx_level=warn ;;
  warning) LOG_THRESHOLD=2 managed_level=WARNING nginx_level=warn ;;
  error) LOG_THRESHOLD=3 managed_level=ERROR nginx_level=error ;;
  *)
    LOG_THRESHOLD=1 managed_level=INFO nginx_level=warn
    log_warning "log_level has an unknown value. Using info."
    ;;
esac

env_sync check --options "$OPTIONS" || fail "The env_vars option has errors; see above."

# ---- 2. Folders, for the runtime user only, nothing wider. ----

# A link would take the chown below and everything after it somewhere else. The runtime user can't put
# one here, but a file share or a restored backup can.
for folder in "$HOME_DIR" /data/claude; do
  [[ ! -L "$folder" ]] || fail "$folder is a symbolic link. Make it a real folder; the app doesn't follow a link here."
  [[ ! -e "$folder" || -d "$folder" ]] || fail "$folder isn't a folder. Remove or rename it."
done
install -d -o hermes -g hermes -m 0700 "$HOME_DIR" /data/claude
# /tmp outlives the services if the container itself is restarted, so a link or file planted there must
# not redirect the chown below or stop the start. HOME and XDG_RUNTIME_DIR stay non-persistent on purpose.
for folder in /tmp/hermes-home /tmp/hermes-runtime /tmp/hermes-cache; do
  if [[ -L "$folder" || (-e "$folder" && ! -d "$folder") ]]; then
    rm -f "$folder"
  fi
  install -d -o hermes -g hermes -m 0700 "$folder"
done
install -d -o root -g root -m 0700 "$RUN_DIR"
# Files created as root through Samba or File Editor must stay usable. A restore also leaves everything
# to root, because the Supervisor copies /config and /data without owners, which would hide the Claude
# login from the CLI. Only owners change, never a file's content. A hard link is skipped, because it would
# hand over its target, and links don't survive a restore anyway. Nothing else is touched.
find -P "$HOME_DIR" /data/claude ! -user hermes ! \( -type f -links +1 \) -exec chown -h hermes:hermes {} +
# By default this is 755 with 644 files, so any process could read SUPERVISOR_TOKEN there.
chmod 0700 /run/s6/container_environment

# ---- 3. Skeleton, only for what is missing. ----

# config.yaml is not created at all. The upstream example's provider "auto" makes the webui skip its
# setup wizard, and with any existing config.yaml the wizard's save is refused: the backend wants
# confirm_overwrite, which the frontend never sends.
#
# plugins/homeassistant stays an empty folder. The agent treats the plugin as installed when it
# exists; without it, the agent reports the baked plugin as missing and tells the user to download it.
as_hermes mkdir -p "$HOME_DIR/webui" "$HOME_DIR/workspace" "$HOME_DIR/plugins/homeassistant"
if [[ ! -e "$HOME_DIR/SOUL.md" && ! -L "$HOME_DIR/SOUL.md" ]]; then
  as_hermes cp /opt/hermes/docker/SOUL.md "$HOME_DIR/SOUL.md"
fi
# Not the upstream .env.example: it has active defaults, and .env overrides the environment for good.
if [[ ! -e "$HOME_DIR/.env" && ! -L "$HOME_DIR/.env" ]]; then
  # shellcheck disable=SC2016  # The inner shell expands "$1".
  as_hermes sh -c 'umask 077; cat > "$1"' sh "$HOME_DIR/.env" <<'EOF'
# Environment for Hermes. Keys from the env_vars option are added and removed by the app. Keys you
# add here by hand stay. All known variables are listed in /opt/hermes/.env.example in the terminal.
EOF
fi

# ---- 4. .env: upsert env_vars, remove names that left the option, warn about reserved names. ----

# The agent reads this file while it imports, so a folder or a pipe here would crash or hang the webui
# again and again, behind a healthy-looking container. Better to stop with the reason.
if [[ -e "$HOME_DIR/.env" && ! -f "$HOME_DIR/.env" ]]; then
  fail "$HOME_DIR/.env isn't a file, so Hermes can't start. Remove or rename it."
fi
env_sync apply --options "$OPTIONS" --env "$HOME_DIR/.env" --marker "$MARKER" --owner hermes
# A link can lead to a file the runtime user can't read, with the same crash loop. This check comes after
# the sync, which repairs the mode of a regular file. A dangling link counts as no file, and Hermes starts.
if [[ -e "$HOME_DIR/.env" ]] && ! as_hermes test -r "$HOME_DIR/.env"; then
  fail "$HOME_DIR/.env can't be read by the hermes user, so Hermes can't start. Remove or rename it."
fi

# ---- 5. Home Assistant access. Written for the webui's run script, never into .env. ----

# Sends the token through stdin, so it never shows up in a process list. -q must stay the first argument.
probe_core() {
  curl -q -s -m 4 -o /dev/null -w '%{http_code}' -K - "$1/api/" <<<"header = \"Authorization: Bearer $2\"" || true
}

ha_token="$(opt '.homeassistant_token // empty')"
ha_mode=supervisor
ha_url=http://supervisor/core
token_value="${SUPERVISOR_TOKEN:-}"
if [[ -n "$ha_token" ]]; then
  chosen=""
  if [[ "$ha_token" =~ ^[A-Za-z0-9._~+/=-]+$ ]]; then
    # New installs of Core use port 80, older ones 8123, and the app can't ask which without more rights.
    for candidate in http://homeassistant:8123 http://homeassistant:80; do
      code="$(probe_core "$candidate" "$ha_token")"
      log_debug "Home Assistant probe ${candidate}: HTTP ${code}"
      if [[ "$code" == "200" ]]; then
        chosen="$candidate"
        break
      fi
    done
  fi
  if [[ -n "$chosen" ]]; then
    ha_mode=long-lived-token
    ha_url="$chosen"
    token_value="$ha_token"
  else
    log_warning "homeassistant_token was not accepted on port 8123 or 80. Using the Supervisor token instead."
  fi
fi
unset ha_token
(
  umask 077
  : >"$RUN_DIR/ha-access.env"
  if [[ -n "$token_value" ]]; then
    {
      printf 'export HASS_URL=%q\n' "$ha_url"
      printf 'export HASS_TOKEN=%q\n' "$token_value"
    } >"$RUN_DIR/ha-access.env"
  fi
)
if [[ -n "$token_value" ]]; then
  log_info "Home Assistant access mode: ${ha_mode} (${ha_url})"
else
  log_warning "No Home Assistant token is available, so the Home Assistant tools stay off."
fi
unset token_value

# ---- 6. Webui settings, on every start. Runs as the runtime user, so a link can't reach root's files. ----

# shellcheck disable=SC2016  # The inner shell expands these.
as_hermes env SETTINGS="$HOME_DIR/webui/settings.json" sh -eu -c '
  umask 077
  tmp="$(mktemp "$SETTINGS.XXXXXX")"
  trap "rm -f \"$tmp\"" EXIT
  if [ -e "$SETTINGS" ] && ! jq -e "type == \"object\"" "$SETTINGS" >/dev/null 2>&1; then
    mv -f "$SETTINGS" "$SETTINGS.invalid"
    echo "[hermes-init] WARNING: webui/settings.json was not valid and is kept as settings.json.invalid."
  fi
  if [ -e "$SETTINGS" ]; then
    jq ". + {check_for_updates: false, ignore_agent_updates: true}" "$SETTINGS" >"$tmp"
  else
    jq -n "{check_for_updates: false, ignore_agent_updates: true}" >"$tmp"
  fi
  mv -f "$tmp" "$SETTINGS"
'

# ---- 7. Claude subscription provider (CLAUDE.md section 3). ----
# BEGIN claude-provider
# Model-provider plugins load from plugins/ whenever they are there, so this symlink to the pinned
# copy is the whole switch. The code stays in the image and is never copied to /config.
claude_link="$HOME_DIR/plugins/claude-subscription-directsdk"
claude_state=off
if [[ "$(opt '.claude_subscription')" == "true" ]]; then
  claude_state=on
  # A real folder with that name is the user's own install. It stays, and claude-check warns.
  if [[ -L "$claude_link" || ! -e "$claude_link" ]]; then
    as_hermes ln -sfn /opt/hermes-plugins/claude-subscription-directsdk "$claude_link"
  fi
elif [[ -L "$claude_link" ]]; then
  as_hermes rm -f "$claude_link"
fi
log_info "Claude subscription provider: $claude_state"
# Warnings only, naming variables and folders, never values. They must not stop the start.
env_sync claude-check --state "$claude_state" --home "$HOME_DIR" || true
if [[ "$claude_state" == on ]]; then
  # Only the exit code counts: the command prints account details, so its output goes nowhere. It
  # inherits no variables, so neither a key nor the Supervisor's token can reach it or sway the answer.
  claude_status_seconds=10
  claude_status=0
  as_hermes env -i PATH=/usr/bin:/bin HOME=/tmp/hermes-home USER=hermes LOGNAME=hermes \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    timeout -k 2 "$claude_status_seconds" "$APP_DIR/bin/claude" auth status \
    </dev/null >/dev/null 2>&1 || claude_status=$?
  case "$claude_status" in
    0) log_info "Claude CLI logged in: yes" ;;
    124 | 137)
      log_warning "The Claude CLI didn't answer the login check within ${claude_status_seconds} s, so it counts as not logged in."
      log_info "Claude CLI logged in: no"
      ;;
    *) log_info "Claude CLI logged in: no" ;;
  esac
fi
# END claude-provider

# ---- 8. Parts rendered from the options. ----

# The agent reads this layer last and the user can't change it from within Hermes (CLAUDE.md 2.4).
sed -i "s/^  level: .*/  level: ${managed_level}/" "$MANAGED_CONFIG"
printf 'error_log /dev/stderr %s;\n' "$nginx_level" >"$RUN_DIR/nginx-log.conf"
# For the gateway's own run script, which logs one line while it waits for the setup wizard.
printf '%s\n' "$log_level" >"$RUN_DIR/log-level"

if [[ "$(opt '.enable_terminal')" == "true" ]]; then
  cp "$APP_DIR/nginx/terminal-on.conf" "$RUN_DIR/nginx-terminal.conf"
  log_info "Terminal: on"
else
  cp "$APP_DIR/nginx/terminal-off.conf" "$RUN_DIR/nginx-terminal.conf"
  log_info "Terminal: off"
fi

log_debug "Startup checks done."
