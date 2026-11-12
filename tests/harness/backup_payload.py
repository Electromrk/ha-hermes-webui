#!/usr/bin/env python3
"""Calculates what a Supervisor backup of the app would contain, without Home Assistant.

Applies backup_exclude the way the Supervisor does (supervisor/apps/app.py, Supervisor 2026.10.5):

- Each file's path is rebuilt as the Supervisor sees it on the host, for example
  /data/app_configs/local_hermes_webui/hermes/tools/x. The container's /config path never appears.
- A pattern matches through PurePath.match(), anchored on the right.
- The walk (securetar's atomic_contents_add) tests each entry before descending, so a matching folder
  is pruned, and it never follows a symlink into a folder.

Reports uncompressed bytes. A local build adds image.tar on top, which only the testbed can measure.
Standard library only, so that it can also run inside the app image.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

DEFAULT_SLUG = "local_hermes_webui"
# Below /data on the Supervisor's side.
SUPERVISOR_ROOT = PurePosixPath("/data")
GATE_BYTES = 5 * 1024 * 1024


def host_paths(slug):
    """The two folders of an app as the Supervisor sees them: (config, data)."""
    return SUPERVISOR_ROOT / "app_configs" / slug, SUPERVISOR_ROOT / "apps" / "data" / slug


def is_excluded(host_path, patterns):
    return any(PurePosixPath(host_path).match(pattern) for pattern in patterns)


def walk(origin, host_origin, arcname, patterns):
    """Yields (arcname, size, excluded, is_dir), mirroring securetar's walk over one folder."""
    try:
        entries = sorted(os.scandir(origin), key=lambda entry: entry.name)
    except OSError as error:
        raise SystemExit(f"Can't read {origin}: {error}") from None
    for entry in entries:
        item_arc = f"{arcname}/{entry.name}"
        item_host = host_origin / entry.name
        if is_excluded(item_host, patterns):
            yield item_arc, 0, True, entry.is_dir(follow_symlinks=False)
            continue
        if entry.is_dir(follow_symlinks=False):
            yield item_arc, 0, False, True
            yield from walk(entry.path, item_host, item_arc, patterns)
        else:
            yield item_arc, entry.stat(follow_symlinks=False).st_size, False, False


def excluded_bytes(path):
    """Bytes behind an excluded entry, to show what the excludes saved."""
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        return path.lstat().st_size
    total = 0
    for root, _folders, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def calculate(config_dir, data_dir, patterns, slug=DEFAULT_SLUG):
    host_config, host_data = host_paths(slug)
    files, excluded = [], []
    total = 0
    for origin, host_origin, arcname in ((data_dir, host_data, "data"), (config_dir, host_config, "config")):
        if not os.path.isdir(origin):
            continue
        for item_arc, size, was_excluded, is_dir in walk(origin, host_origin, arcname, patterns):
            if was_excluded:
                relative = item_arc.split("/", 1)[1]
                excluded.append((item_arc, excluded_bytes(os.path.join(origin, relative))))
            elif not is_dir:
                files.append((item_arc, size))
                total += size
    return {"total_bytes": total, "files": files, "excluded": excluded,
            "gate_bytes": GATE_BYTES, "gate_passed": total < GATE_BYTES}


def read_excludes(manifest_text):
    """Reads backup_exclude from config.yaml text, without a YAML library."""
    patterns, inside = [], False
    for line in manifest_text.splitlines():
        if re.match(r"^backup_exclude:\s*$", line):
            inside = True
        elif inside:
            match = re.match(r"^\s+-\s+(.+?)\s*$", line)
            if match:
                patterns.append(match.group(1).strip("\"'"))
            elif line.strip() and not line.lstrip().startswith("#"):
                break
    return patterns


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="folder mounted at /config")
    parser.add_argument("--data", required=True, help="folder mounted at /data")
    parser.add_argument("--manifest", required=True, help="the app's config.yaml")
    parser.add_argument("--slug", default=DEFAULT_SLUG)
    parser.add_argument("--top", type=int, default=15, help="how many of the largest files to list")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    with open(args.manifest, encoding="utf-8") as handle:
        patterns = read_excludes(handle.read())
    result = calculate(args.config, args.data, patterns, args.slug)
    if args.json:
        print(json.dumps(result))
    else:
        print(f"Patterns: {len(patterns)}; files: {len(result['files'])}")
        print(f"Payload without image.tar: {result['total_bytes']} bytes (uncompressed)")
        print(f"Gate below {GATE_BYTES} bytes: {'PASS' if result['gate_passed'] else 'FAIL'}")
        print("Largest files:")
        for name, size in sorted(result["files"], key=lambda item: -item[1])[: args.top]:
            print(f"  {size:>10}  {name}")
        print("Excluded:")
        for name, size in result["excluded"]:
            print(f"  {size:>10}  {name}")
    return 0 if result["gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
