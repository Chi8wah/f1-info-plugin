"""统一缓存目录、无损迁移和加载/卸载路径的回归测试。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import json
import tempfile
import unittest

from f1_info_plugin import cache as cache_module
from f1_info_plugin.core import F1InfoPlugin


class CacheStorageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.legacy_path = root / "source" / "data" / "cache.json"
        self.data_dir = root / "persistent" / "chi8wah.f1-info-plugin"
        self.cache_path = self.data_dir / "cache.json"
        legacy_patch = patch.object(cache_module, "LEGACY_CACHE_PATH", self.legacy_path)
        legacy_patch.start()
        self.addCleanup(legacy_patch.stop)
        self.plugin = F1InfoPlugin()
        self.plugin.set_plugin_config(self.plugin.get_default_config())
        self.plugin._set_context(
            SimpleNamespace(paths=SimpleNamespace(data_dir=self.data_dir), logger=Mock())
        )
        self.cached = {
            "news:2026-11-03:1": {
                "value": "已生成的中文摘要",
                "expires_at": 1,
                "urls": ["https://example.com/f1-news"],
            }
        }
        self.cached_bytes = json.dumps(self.cached, ensure_ascii=False, indent=1).encode("utf-8")

    def write_legacy_cache(self) -> None:
        self.legacy_path.parent.mkdir(parents=True)
        self.legacy_path.write_bytes(self.cached_bytes)

    def test_fresh_install_saves_only_to_granted_data_directory(self) -> None:
        self.assertEqual(self.plugin._load_cache(), {})
        self.plugin._cache = self.cached

        self.plugin._save_cache()

        self.assertEqual(json.loads(self.cache_path.read_text(encoding="utf-8")), self.cached)
        self.assertFalse(self.legacy_path.parent.exists())

    def test_migration_preserves_cache_bytes_and_other_persistent_data(self) -> None:
        self.write_legacy_cache()
        self.data_dir.mkdir(parents=True)
        schedule_path = self.data_dir / "schedule_context.json"
        schedule_path.write_bytes(b'{"version": 1}')

        self.assertEqual(self.plugin._load_cache(), self.cached)

        self.assertEqual(self.cache_path.read_bytes(), self.cached_bytes)
        self.assertEqual(schedule_path.read_bytes(), b'{"version": 1}')
        self.assertFalse(self.legacy_path.parent.exists())
        self.assertEqual(self.plugin._load_cache(), self.cached)
        self.assertEqual(list(self.data_dir.glob("cache.legacy.*.json")), [])

    def test_existing_new_cache_wins_and_legacy_cache_is_backed_up_once(self) -> None:
        self.write_legacy_cache()
        self.data_dir.mkdir(parents=True)
        current_bytes = b'{"new-entry": {"value": "new summary"}}'
        self.cache_path.write_bytes(current_bytes)

        self.assertEqual(self.plugin._load_cache(), json.loads(current_bytes))

        backups = list(self.data_dir.glob("cache.legacy.*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.cached_bytes)
        self.assertEqual(self.cache_path.read_bytes(), current_bytes)
        self.assertFalse(self.legacy_path.parent.exists())
        self.assertEqual(self.plugin._load_cache(), json.loads(current_bytes))
        self.assertEqual(list(self.data_dir.glob("cache.legacy.*.json")), backups)

    def test_migration_keeps_unrelated_legacy_files(self) -> None:
        self.write_legacy_cache()
        unrelated = self.legacy_path.parent / "keep.txt"
        unrelated.write_bytes(b"user data")

        self.assertEqual(self.plugin._load_cache(), self.cached)

        self.assertEqual(unrelated.read_bytes(), b"user data")
        self.assertFalse(self.legacy_path.exists())
        self.assertEqual(self.cache_path.read_bytes(), self.cached_bytes)

    def test_failed_copy_preserves_legacy_cache_and_removes_partial_copy(self) -> None:
        self.write_legacy_cache()

        with (
            patch.object(cache_module.os, "fsync", side_effect=OSError("disk full")),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            self.plugin._load_cache()

        self.assertEqual(self.legacy_path.read_bytes(), self.cached_bytes)
        self.assertFalse(self.cache_path.exists())
        self.assertEqual(list(self.data_dir.iterdir()), [])

    async def test_load_and_unload_keep_using_migrated_cache(self) -> None:
        self.write_legacy_cache()
        with (
            patch.object(self.plugin, "_start_scheduler"),
            patch.object(self.plugin, "_start_schedule_context_task"),
            patch.object(self.plugin, "_reconfigure_meme", new=AsyncMock()),
        ):
            await self.plugin.on_load()
            self.assertEqual(self.plugin._cache, self.cached)
            self.plugin._cache["after-load"] = {"value": "新记录"}
            await self.plugin.on_unload()

        self.assertEqual(json.loads(self.cache_path.read_text(encoding="utf-8")), self.plugin._cache)
        self.assertFalse(self.legacy_path.parent.exists())


if __name__ == "__main__":
    unittest.main()
