"""Backup policy of the app: mode, anchored excludes, and what the Supervisor would really pack.

The matching follows supervisor/apps/app.py and securetar's walk, through tests/harness/backup_payload.py.
Everything is tested against host-style paths, never the container's /config.
"""

import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "harness"))

import backup_payload  # noqa: E402

MANIFEST = yaml.safe_load((ROOT / "hermes_webui" / "config.yaml").read_text(encoding="utf-8"))
PATTERNS = MANIFEST["backup_exclude"]
SLUGS = ("local_hermes_webui", "5c53de3b_hermes_webui")


def host_path(slug, relative):
    return PurePosixPath("/data/app_configs") / slug / "hermes" / relative


def is_pruned(path):
    """A file is left out when it or any folder above it matches, because the walk prunes folders."""
    path = PurePosixPath(path)
    return any(backup_payload.is_excluded(candidate, PATTERNS) for candidate in [path, *path.parents])


class BackupManifestTests(unittest.TestCase):
    def test_backup_mode_is_cold_with_no_hook(self):
        self.assertEqual(MANIFEST.get("backup"), "cold")
        self.assertNotIn("backup_pre", MANIFEST)
        self.assertNotIn("backup_post", MANIFEST)

    def test_every_pattern_is_anchored_to_the_app_folder(self):
        for pattern in PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertTrue(pattern.startswith("*_hermes_webui/hermes/"), pattern)

    def test_patterns_in_the_manifest_match_the_calculators_reader(self):
        text = (ROOT / "hermes_webui" / "config.yaml").read_text(encoding="utf-8")
        self.assertEqual(backup_payload.read_excludes(text), PATTERNS)

    def test_database_journals_stay_in_the_backup(self):
        joined = " ".join(PATTERNS)
        self.assertNotIn(".db-wal", joined)
        self.assertNotIn(".db-shm", joined)


class ExcludeMatchingTests(unittest.TestCase):
    EXCLUDED = [
        "logs/agent.log",
        "logs/curator/run.log",
        "cache/plugin-catalog.json",
        "cache/uv/archive/x",
        "tools/sub/dir/file",
        "installs/351f81cc06284494/bootstrap/default.json",
        "installs/x/env/lib/python/site.py",
        "environments/x/bin/python",
        "models_dev_cache.json",
        "models_dev_cache.etag",
        "models_dev_cache.json.corrupt",
        "sessions/request_dump_abc_20261106_081300_803993.json",
        "backups/pre-migration-2026-11-06-081300.zip",
        "backups/pre-update-2026-11-06-081300.incomplete.zip",
    ]
    KEPT = [
        "config.yaml",
        ".env",
        "SOUL.md",
        "auth.json",
        "state.db",
        "state.db-wal",
        "state.db-shm",
        "sessions/session_abc.json",
        "memories/MEMORY.md",
        "skills/foo/SKILL.md",
        "skills/foo/tools/keep.txt",
        "cron/jobs.json",
        "plugins/spotify/plugin.yaml",
        "webui/settings.json",
        "webui/sessions/abc.json",
        "workspace/proj/tools/keep.txt",
        "workspace/hermes/tools/keep.txt",
        "workspace/proj/cache/keep.txt",
        "workspace/logs/keep.txt",
        "workspace/request_dump_mine.json",
        "workspace/models_dev_cache.json",
        "workspace/report.zip",
        "backups/config/config.yaml.good.20261106-081255",
    ]

    def test_host_paths_for_local_and_store_slugs(self):
        for slug in SLUGS:
            for relative in self.EXCLUDED:
                with self.subTest(slug=slug, relative=relative):
                    self.assertTrue(is_pruned(host_path(slug, relative)))
            for relative in self.KEPT:
                with self.subTest(slug=slug, relative=relative):
                    self.assertFalse(is_pruned(host_path(slug, relative)))

    def test_the_container_path_never_matches(self):
        """Documents why the patterns can't be written against /config."""
        for pattern in PATTERNS:
            self.assertFalse(PurePosixPath("/config/hermes/tools/x").match(pattern))


class PayloadCalculatorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "config"
        self.data = self.root / "data"
        (self.config / "hermes").mkdir(parents=True)
        self.data.mkdir()

    def write(self, relative, size=10, base=None):
        path = (base or self.config / "hermes") / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    def calculate(self):
        return backup_payload.calculate(self.config, self.data, PATTERNS)

    def test_counts_kept_files_and_prunes_excluded_folders(self):
        self.write("workspace/proj/tools/keep.txt", 100)
        self.write("tools/sub/drop.bin", 1000)
        self.write("models_dev_cache.json", 5000)
        self.write("options.json", 50, base=self.data)
        result = self.calculate()
        names = {name for name, _size in result["files"]}
        self.assertEqual(names, {"config/hermes/workspace/proj/tools/keep.txt", "data/options.json"})
        self.assertEqual(result["total_bytes"], 150)
        excluded = dict(result["excluded"])
        self.assertEqual(excluded["config/hermes/tools"] if "config/hermes/tools" in excluded else None, None)
        self.assertEqual(excluded["config/hermes/tools/sub"], 1000)
        self.assertEqual(excluded["config/hermes/models_dev_cache.json"], 5000)

    def test_gate(self):
        self.write("workspace/small", 1000)
        self.assertTrue(self.calculate()["gate_passed"])
        self.write("workspace/large", backup_payload.GATE_BYTES)
        self.assertFalse(self.calculate()["gate_passed"])

    def test_a_symlink_to_a_folder_is_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "big").write_bytes(b"x" * 9999)
        (self.config / "hermes" / "plugins").mkdir()
        (self.config / "hermes" / "plugins" / "linked").symlink_to(outside)
        result = self.calculate()
        self.assertLess(result["total_bytes"], 9999)
        self.assertFalse([name for name, _size in result["files"] if name.endswith("/big")])

    def test_uses_the_host_side_paths(self):
        config, data = backup_payload.host_paths("local_hermes_webui")
        self.assertEqual(str(config), "/data/app_configs/local_hermes_webui")
        self.assertEqual(str(data), "/data/apps/data/local_hermes_webui")


if __name__ == "__main__":
    unittest.main()
