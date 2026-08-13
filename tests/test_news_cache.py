from __future__ import annotations
# pyright: reportAny=false, reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false

import importlib.util
import sys
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PACKAGE = ROOT / "f1_info_plugin"


def load_sdk_free_module(module_name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


package = types.ModuleType("f1_info_plugin")
package.__path__ = [str(PLUGIN_PACKAGE)]
_ = sys.modules.setdefault("f1_info_plugin", package)

constants_module = load_sdk_free_module("f1_info_plugin.constants", PLUGIN_PACKAGE / "constants.py")
models_module = load_sdk_free_module("f1_info_plugin.models", PLUGIN_PACKAGE / "models.py")
cache_module = load_sdk_free_module("f1_info_plugin.cache", PLUGIN_PACKAGE / "cache.py")
renderers_module = load_sdk_free_module("f1_info_plugin.renderers", PLUGIN_PACKAGE / "renderers.py")
news_module = load_sdk_free_module("f1_info_plugin.news", PLUGIN_PACKAGE / "news.py")

BEIJING_TZ = constants_module.BEIJING_TZ
NEWS_FALLBACK_NOTICE = constants_module.NEWS_FALLBACK_NOTICE
UTC = constants_module.UTC
CacheMixin = cache_module.CacheMixin
NewsItem = models_module.NewsItem
NewsMixin = news_module.NewsMixin
NewsPageData = models_module.NewsPageData
NewsSummaryData = models_module.NewsSummaryData
RendererMixin = renderers_module.RendererMixin


class NewsCacheHarness(NewsMixin, CacheMixin, RendererMixin):
    def __init__(self, summary_attempts: list[list[NewsSummaryData]]) -> None:
        self.config = SimpleNamespace(
            plugin=SimpleNamespace(enabled=True),
            news=SimpleNamespace(daily_limit=10, cache_ttl_minutes=60),
        )
        self._cache: dict[str, object] = {}
        self._summary_attempts = list(summary_attempts)
        self.collect_calls = 0
        self.summary_calls = 0
        self.save_calls = 0
        self._source_items = [
            NewsItem(
                source="Test RSS",
                title="Test F1 headline",
                url="https://example.com/f1-news",
                description="Test description",
                published_at=datetime.now(UTC),
                weight=1.0,
            )
        ]

    def cache_key(self, limit: int = 1) -> str:
        return f"news:{datetime.now(BEIJING_TZ).date().isoformat()}:{limit}"

    async def _collect_news_items(self) -> list[NewsItem]:
        self.collect_calls += 1
        return list(self._source_items)

    async def _generate_news_summary_items(
        self,
        groups: list[dict[str, object]],
        limit: int,
    ) -> list[NewsSummaryData]:
        del groups, limit
        self.summary_calls += 1
        return self._summary_attempts.pop(0)

    async def _save_cache_async(self) -> None:
        self.save_calls += 1


class NewsCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_summary_is_not_cached_and_next_request_retries(self) -> None:
        harness = NewsCacheHarness(
            [
                [],
                [NewsSummaryData(summary="这是重试后生成的中文摘要", url="https://example.com/f1-news")],
            ]
        )

        fallback = await harness._news_page_data(limit=1)

        self.assertIsInstance(fallback, NewsPageData)
        self.assertTrue(fallback.using_raw_fallback)
        self.assertNotIn(harness.cache_key(), harness._cache)
        self.assertEqual(harness.save_calls, 0)

        retry = await harness._news_page_data(limit=1)

        self.assertIsInstance(retry, NewsPageData)
        self.assertFalse(retry.using_raw_fallback)
        self.assertEqual(retry.items[0].summary, "这是重试后生成的中文摘要")
        self.assertEqual(harness.collect_calls, 2)
        self.assertEqual(harness.summary_calls, 2)
        self.assertIn(harness.cache_key(), harness._cache)
        self.assertEqual(harness.save_calls, 1)

    async def test_legacy_failed_cache_is_removed_before_retrying(self) -> None:
        harness = NewsCacheHarness(
            [[NewsSummaryData(summary="重新生成的中文摘要", url="https://example.com/f1-news")]]
        )
        key = harness.cache_key()
        harness._cache[key] = {
            "expires_at": time.time() + 3600,
            "news_notice": NEWS_FALLBACK_NOTICE,
            "using_raw_fallback": True,
            "news_items": [{"summary": "Test RSS: stale fallback", "url": "https://example.com/f1-news"}],
        }

        page = await harness._news_page_data(limit=1)

        self.assertIsInstance(page, NewsPageData)
        self.assertFalse(page.using_raw_fallback)
        self.assertEqual(page.items[0].summary, "重新生成的中文摘要")
        self.assertEqual(harness.collect_calls, 1)
        self.assertEqual(harness.summary_calls, 1)
        self.assertEqual(harness.save_calls, 2)
        cached_page = harness._cache_news_page(harness._get_cache_row(key) or {})
        self.assertIsNotNone(cached_page)
        if cached_page is not None:
            self.assertFalse(cached_page.using_raw_fallback)
            self.assertNotEqual(cached_page.notice, NEWS_FALLBACK_NOTICE)


if __name__ == "__main__":
    _ = unittest.main()
