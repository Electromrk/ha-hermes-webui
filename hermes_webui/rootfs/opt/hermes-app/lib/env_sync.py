#!/usr/bin/env python3
"""Keeps $HERMES_HOME/.env in step with the app's env_vars option.

Called by hermes-init.sh, as root, before any service starts. Standard library only.

  env_sync.py check --options /data/options.json
      Validates the option. Exit 1 if an entry can't be accepted.

  env_sync.py apply --options /data/options.json --env /config/hermes/.env \
                    --marker /data/env_vars_names.json [--owner hermes]
      Upserts the option's entries in place, removes names that left the option, keeps everything
      else, and warns about reserved names edited in by hand.

  env_sync.py claude-check --state on|off --home /config/hermes
      Warns about what stands in the way of the claude_subscription option. Changes nothing.

Output lines start with ERROR: or WARNING:. Values never appear in the output, only names.
"""
import argparse
import json
import os
import pwd
import re
import stat
import sys
import tempfile

NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# A .env assignment: the agent's parser accepts an "export " prefix and spaces before "=".
KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
# Values of these characters need no quoting in any parser that reads this file.
UNQUOTED_RE = re.compile(r"^[A-Za-z0-9_./:@%+=,-]*$")

# Names this app sets itself. Hermes loads .env with override, so one of these in .env would beat
# the service environment. CLAUDE.md section 2.6 is the source of this list; the entries after it
# in each group are further names found in the base image and the agent that would break the same
# way: they move HERMES_HOME, the config or the code paths.
RESERVED_NAMES = frozenset(
    {
        "HASS_TOKEN",
        "HASS_URL",
        "SUPERVISOR_TOKEN",
        "HASSIO_TOKEN",
        "HERMES_HOME",
        "HERMES_WRITE_SAFE_ROOT",
        "HERMES_RUNTIME_DIR",
        "HERMES_BUNDLED_PLUGINS",
        "HERMES_DISABLE_LAZY_INSTALLS",
        "HERMES_PYTHON",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND",
        "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR",
        "DISABLE_AUTOUPDATER",
        "DISABLE_UPDATES",
        "XDG_CACHE_HOME",
        "UV_CACHE_DIR",
        "PIP_CACHE_DIR",
        "NPM_CONFIG_CACHE",
        "npm_config_cache",
        "PATH",
        "HOME",
        "XDG_RUNTIME_DIR",
        "HERMES_TUI_DIR",
        "HERMES_WEB_DIST",
        "HERMES_IGNORE_USER_CONFIG",
        "HERMES_REAL_HOME",
        # The gateway's run script sets these (docs/architecture.md, section 15).
        "HERMES_S6_SUPERVISED_CHILD",
        "HERMES_SUPERVISED_CHILD",
        "HERMES_ALLOW_ROOT_GATEWAY",
        "GATEWAY_MULTIPLEX_PROFILES",
        "GATEWAY_ON_ALL_ADAPTERS_DOWN",
        "HASS_HOME_CHANNEL",
        # Inbound listeners. The app has no port for them, and the webhook adapter binds every interface.
        # The managed layer in /etc/hermes keeps them off or on loopback even when .env sets them.
        "WEBHOOK_ENABLED",
        "WEBHOOK_PORT",
        "WEBHOOK_SECRET",
        "TELEGRAM_WEBHOOK_URL",
        "TELEGRAM_WEBHOOK_SECRET",
        "TELEGRAM_WEBHOOK_PORT",
        "TELEGRAM_WEBHOOK_HOST",
        "LINE_HOST",
        "WHATSAPP_CLOUD_WEBHOOK_HOST",
        "WECOM_CALLBACK_HOST",
        # Hosts of listeners that bind loopback by default but take a wildcard from .env.
        "A2A_HOST",
        "SMS_WEBHOOK_HOST",
        "BLUEBUBBLES_WEBHOOK_HOST",
        "FEISHU_WEBHOOK_HOST",
        "TEAMS_HOST",
    }
)
RESERVED_PREFIXES = (
    "HERMES_WEBUI_",
    "HERMES_MANAGED",
    "HERMES_DASHBOARD",
    "HERMES_HOME_",
    "HERMES_CONFIG",
    "HERMES_ENV",
    "HERMES_GATEWAY_",
    "GATEWAY_RELAY_",
    "API_SERVER_",
    "MSGRAPH_WEBHOOK_",
)

# Reserved settings of listeners that the managed layer keeps off, which the app itself never sets. In .env
# they change nothing, so the warning says that instead of claiming they override a value of the app.
LISTENER_NAMES = frozenset(
    {"WEBHOOK_PORT", "WEBHOOK_SECRET", "TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_WEBHOOK_PORT", "TELEGRAM_WEBHOOK_HOST"}
)
LISTENER_PREFIXES = ("API_SERVER_", "MSGRAPH_WEBHOOK_")

# The managed .env of the image. Hermes loads it last, so its names beat .env, not the other way round.
MANAGED_ENV = "/etc/hermes/.env"

# Settings that let anyone who finds the bot use it, with the Home Assistant tools and the terminal, and
# the one that turns the approval prompts off. Allowed, because a user may want them, but named at every
# start. A switch counts unless it is off; an allowlist counts when it holds a "*".
OPEN_ACCESS_SWITCH_RE = re.compile(r"^(?:[A-Z0-9_]+_ALLOW_ALL_USERS|HERMES_YOLO_MODE)$")
OPEN_ACCESS_LIST_RE = re.compile(r"^[A-Z0-9_]+_ALLOWED_USERS$")
OPEN_ACCESS_CONFIG_RE = re.compile(r"^\s*allow_all_users:\s*[\"']?(?:true|yes|on|1)[\"']?\s*(?:#.*)?$", re.M | re.I)

# The Claude provider (CLAUDE.md section 3). The plugin refuses to run while one of these is set.
# This is the test its request path uses, so the warning appears exactly when the refusal would:
# a key counts when it isn't empty, a switch when it isn't turned off. The plugin keeps ANTHROPIC_API_KEY,
# ANTHROPIC_AUTH_TOKEN and ANTHROPIC_FOUNDRY_API_KEY away from the CLI instead, so those need no warning.
CLAUDE_PROVIDER = "claude-subscription-directsdk-experimental"
CLAUDE_CONFLICT_KEYS = ("ANTHROPIC_BASE_URL",)
CLAUDE_CONFLICT_SWITCHES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
SWITCH_OFF_VALUES = ("", "0", "false", "no", "off")
# Also matches a list entry such as "- provider: ...", and any section: a fallback or an auxiliary
# task pointing at the provider fails the same way when the switch is off.
CLAUDE_SELECTED_RE = re.compile(rf"^[\s-]*provider:\s*[\"']?{re.escape(CLAUDE_PROVIDER)}[\"']?\s*(?:#.*)?$", re.M)
CLAUDE_MANIFEST_RE = re.compile(rf"^name:\s*[\"']?{re.escape(CLAUDE_PROVIDER)}[\"']?\s*(?:#.*)?$", re.M)


def is_reserved(name):
    return name in RESERVED_NAMES or name.startswith(RESERVED_PREFIXES)


def emit(level, message):
    print(f"{level}: {message}", flush=True)


def load_entries(options_path):
    with open(options_path, encoding="utf-8") as handle:
        options = json.load(handle)
    entries = options.get("env_vars") or []
    return entries if isinstance(entries, list) else [entries]


def validate(entries):
    """Returns error messages. Refers to bad names by position, because a typo there could be a key."""
    errors = []
    for position, entry in enumerate(entries, start=1):
        name = entry.get("name") if isinstance(entry, dict) else None
        value = entry.get("value") if isinstance(entry, dict) else None
        # fullmatch: with match(), the "$" would let a name with a trailing line break through.
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            errors.append(f"env_vars entry {position}: the name must match ^[A-Z_][A-Z0-9_]*$.")
            continue
        if is_reserved(name):
            errors.append(f"env_vars: '{name}' is reserved by this app. Remove it from the option.")
        if not isinstance(value, str):
            errors.append(f"env_vars: the value of '{name}' must be a string.")
        elif any(char in value for char in "\r\n\0"):
            errors.append(f"env_vars: the value of '{name}' must be a single line.")
    return errors


def format_line(name, value):
    if UNQUOTED_RE.match(value):
        return f"{name}={value}\n"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{name}="{escaped}"\n'


def merge(text, wanted, previous):
    """Returns .env text with `wanted` upserted and `previous` names no longer wanted removed.

    Comments, blank lines, the order of the file and every other key stay exactly as they were.
    """
    out = []
    written = set()
    for line in text.splitlines(keepends=True):
        match = KEY_RE.match(line)
        key = match.group(1) if match else None
        if key in wanted:
            if key not in written:
                newline = "\r\n" if line.endswith("\r\n") else "\n"
                out.append(format_line(key, wanted[key]).rstrip("\n") + newline)
                written.add(key)
            continue
        if key in previous:
            continue
        out.append(line)
    if out and not out[-1].endswith("\n") and len(written) < len(wanted):
        out[-1] += "\n"
    for key, value in wanted.items():
        if key not in written:
            out.append(format_line(key, value))
    return "".join(out)


def reserved_in_env(text):
    names = []
    for line in text.splitlines():
        match = KEY_RE.match(line)
        if match and is_reserved(match.group(1)) and match.group(1) not in names:
            names.append(match.group(1))
    return names


def read_env(path):
    """Reads .env without following a symlink. Returns text, or None if it isn't a regular file."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    # Before fdopen, which raises for a folder.
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        return None
    with os.fdopen(descriptor, "rb") as handle:
        raw = handle.read()
    # Notepad and PowerShell on a Samba share write these. The agent's own loader reads them as well.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8", "surrogateescape")


def write_file(path, text, owner=None):
    """Replaces a file atomically, mode 0600. A failure leaves the old file as it was."""
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".env_sync_")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", errors="surrogateescape", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if owner is not None:
            os.chown(temporary, owner.pw_uid, owner.pw_gid)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def env_values(text):
    """Name to value for every assignment, the last one winning. Enough to tell set from empty."""
    values = {}
    for line in text.splitlines():
        match = KEY_RE.match(line)
        if not match:
            continue
        raw = line[match.end():].strip()
        if raw[:1] in ("'", '"'):
            closing = raw.find(raw[0], 1)
            values[match.group(1)] = raw[1:closing] if closing > 0 else raw[1:]
        else:
            values[match.group(1)] = "" if raw.startswith("#") else raw.split(" #", 1)[0].strip()
    return values


def managed_env_names(path):
    """Names the image's managed .env sets. An unreadable file gives none, so every warning stays generic."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return set()
    return {match.group(1) for match in map(KEY_RE.match, text.splitlines()) if match}


def open_access_names(env_text):
    """Names in .env that open a messenger bot to everyone. Only names are returned, never values."""
    found = []
    for name, value in env_values(env_text).items():
        value = value.strip()
        if OPEN_ACCESS_SWITCH_RE.match(name) and value.lower() not in SWITCH_OFF_VALUES:
            found.append(name)
        elif OPEN_ACCESS_LIST_RE.match(name) and "*" in value:
            found.append(name)
    return found


def claude_conflicts(env_text, environment):
    """Returns "NAME (where)" for each variable the plugin refuses. .env wins, as it does in Hermes."""
    from_file = env_values(env_text)
    found = []
    for name in CLAUDE_CONFLICT_KEYS + CLAUDE_CONFLICT_SWITCHES:
        value = from_file.get(name, environment.get(name, ""))
        is_set = value.strip().lower() not in SWITCH_OFF_VALUES if name in CLAUDE_CONFLICT_SWITCHES else bool(value)
        if is_set:
            found.append(f"{name} ({'.env' if name in from_file else 'environment'})")
    return found


def own_plugin_copies(home):
    """Real folders under plugins/ that hold the Claude plugin. The app's own entry is a symlink."""
    plugins = os.path.join(home, "plugins")
    try:
        names = sorted(os.listdir(plugins))
    except OSError:
        return []
    copies = []
    for name in names:
        folder = os.path.join(plugins, name)
        if not os.path.islink(folder) and CLAUDE_MANIFEST_RE.search(read_env(os.path.join(folder, "plugin.yaml")) or ""):
            copies.append(name)
    return copies


def read_marker(path):
    try:
        with open(path, encoding="utf-8") as handle:
            names = json.load(handle)
    except FileNotFoundError:
        return set()
    except (OSError, ValueError):
        emit("WARNING", "The env_vars marker can't be read. Names removed from the option stay in .env.")
        return set()
    return {name for name in names if isinstance(name, str)} if isinstance(names, list) else set()


def cmd_check(args):
    errors = validate(load_entries(args.options))
    for message in errors:
        emit("ERROR", message)
    return 1 if errors else 0


def cmd_apply(args):
    entries = load_entries(args.options)
    errors = validate(entries)
    if errors:
        for message in errors:
            emit("ERROR", message)
        return 1
    wanted = {}
    for entry in entries:
        if entry["name"] in wanted:
            emit("WARNING", f"env_vars: '{entry['name']}' appears more than once. The last value wins.")
        wanted[entry["name"]] = entry["value"]

    owner = pwd.getpwnam(args.owner) if args.owner else None
    text = read_env(args.env)
    if text is None:
        # A symlink here could point root at any file, so nothing follows it. The file shares can fix it.
        emit("WARNING", ".env is not a regular file. env_vars are not applied until it is.")
        return 0
    merged = merge(text, wanted, read_marker(args.marker))
    if merged != text or stat.S_IMODE(os.lstat(args.env).st_mode) != 0o600:
        write_file(args.env, merged, owner)
    try:
        write_file(args.marker, json.dumps(sorted(wanted)) + "\n")
    except OSError:
        emit("WARNING", "The env_vars marker can't be saved. Names removed from the option stay in .env.")

    managed = managed_env_names(args.managed_env)
    for name in reserved_in_env(merged):
        if name in managed:
            emit("WARNING", f".env sets the reserved name {name}, which has no effect: this app's own value wins. "
                            "Remove it from .env.")
        elif name in LISTENER_NAMES or name.startswith(LISTENER_PREFIXES):
            emit("WARNING", f".env sets the reserved name {name}, which has no effect: this app keeps that "
                            "listener off. Remove it from .env.")
        else:
            emit("WARNING", f".env sets the reserved name {name}, which overrides this app's value. Remove it from .env.")
    for name in open_access_names(merged):
        if name == "HERMES_YOLO_MODE":
            emit("WARNING", ".env sets HERMES_YOLO_MODE, so the agent runs risky commands without asking first, "
                            "also for messenger users. Remove it to get the approval prompts back.")
        else:
            emit("WARNING", f".env sets {name}, which lets anyone who finds your messenger bot use it, including "
                            "the Home Assistant tools and the terminal. Use an allowlist of user IDs instead.")
    config = read_env(os.path.join(os.path.dirname(args.env), "config.yaml")) or ""
    if OPEN_ACCESS_CONFIG_RE.search(config):
        emit("WARNING", "config.yaml sets allow_all_users, which lets anyone who finds your messenger bot use it, "
                        "including the Home Assistant tools and the terminal. Use an allowlist of user IDs instead.")
    return 0


def cmd_claude_check(args):
    if args.state == "on":
        found = claude_conflicts(read_env(os.path.join(args.home, ".env")) or "", os.environ)
        if found:
            emit("WARNING", f"The Claude provider refuses to run while these are set: {', '.join(found)}. "
                            "Remove them to use it. See Claude subscription in the app's documentation.")
    elif CLAUDE_SELECTED_RE.search(read_env(os.path.join(args.home, "config.yaml")) or ""):
        emit("WARNING", "config.yaml still selects the Claude provider, but claude_subscription is off. "
                        "Those requests fail until you turn the option on or pick another provider.")
    for name in own_plugin_copies(args.home):
        emit("WARNING", f"plugins/{name} is your own copy of the Claude plugin. claude_subscription doesn't "
                        "control it, and it competes with the tested copy in the image. Remove it to go back to that.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--options", required=True)
    check.set_defaults(handler=cmd_check)
    apply_ = commands.add_parser("apply")
    apply_.add_argument("--options", required=True)
    apply_.add_argument("--env", required=True)
    apply_.add_argument("--marker", required=True)
    apply_.add_argument("--owner")
    apply_.add_argument("--managed-env", default=MANAGED_ENV)
    apply_.set_defaults(handler=cmd_apply)
    claude = commands.add_parser("claude-check")
    claude.add_argument("--state", required=True, choices=("on", "off"))
    claude.add_argument("--home", required=True)
    claude.set_defaults(handler=cmd_claude_check)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, ValueError) as error:
        # The exception text can quote file content for a ValueError, so only its type is shown.
        emit("ERROR", f"env_sync {args.command} failed: {type(error).__name__}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
