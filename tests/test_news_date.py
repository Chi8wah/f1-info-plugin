from __future__ import annotations
# pyright: reportAny=false, reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false

import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path


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

_ = load_sdk_free_module("f1_info_plugin.constants", PLUGIN_PACKAGE / "constants.py")
models_module = load_sdk_free_module("f1_info_plugin.models", PLUGIN_PACKAGE / "models.py")
renderers_module = load_sdk_free_module("f1_info_plugin.renderers", PLUGIN_PACKAGE / "renderers.py")
cache_module = load_sdk_free_module("f1_info_plugin.cache", PLUGIN_PACKAGE / "cache.py")

CacheMixin = cache_module.CacheMixin
NewsPageData = models_module.NewsPageData
NewsSummaryData = models_module.NewsSummaryData
RendererMixin = renderers_module.RendererMixin

CacheRowValue = str | float | bool | list[str] | list[dict[str, str]]


class CacheHarness(CacheMixin):
    _cache: dict[str, dict[str, CacheRowValue]]

    def __init__(self) -> None:
        super().__init__()
        self._cache = {}


class NewsDateRenderingTest(unittest.TestCase):
    def test_news_text_renders_beijing_date_when_present(self) -> None:
        page = NewsPageData(
            title="今日 F1 重要新闻",
            beijing_date="北京时间 2026-07-16",
            items=[NewsSummaryData(summary="维斯塔潘完成测试", url="https://example.com/news")],
        )

        text = RendererMixin._render_news_text(page)

        self.assertIn("北京时间 2026-07-16", text)
        self.assertEqual(text.splitlines()[1], "北京时间 2026-07-16")

    def test_news_text_omits_empty_beijing_date(self) -> None:
        page = NewsPageData(
            title="今日 F1 重要新闻",
            items=[NewsSummaryData(summary="维斯塔潘完成测试", url="")],
        )

        text = RendererMixin._render_news_text(page)

        self.assertNotIn("北京时间", text)
        self.assertEqual(text.splitlines()[1], "1. 维斯塔潘完成测试")

    def test_news_html_renders_beijing_date_when_present(self) -> None:
        page = NewsPageData(
            title="今日 F1 重要新闻",
            beijing_date="北京时间 2026-07-16",
            items=[NewsSummaryData(summary="维斯塔潘完成测试", url="")],
        )

        html = RendererMixin()._render_news_html(page)

        self.assertIn("北京时间 2026-07-16", html)
        self.assertIn('class="news-date"', html)

    def test_old_cache_without_beijing_date_renders_no_date(self) -> None:
        harness = CacheHarness()
        row = {
            "expires_at": time.time() + 60,
            "news_title": "今日 F1 重要新闻",
            "news_items": [{"summary": "旧缓存新闻", "url": ""}],
        }

        page = harness._cache_news_page(row)

        if page is None:
            self.fail("Expected old cache row to reconstruct a news page")
        self.assertEqual(page.beijing_date, "")
        self.assertNotIn("北京时间", RendererMixin._render_news_text(page))

    def test_new_cache_round_trips_beijing_date(self) -> None:
        harness = CacheHarness()

        harness._set_cache(
            "news:2026-07-16:1",
            "",
            ttl_seconds=60,
            news_items=[NewsSummaryData(summary="新缓存新闻", url="")],
            news_beijing_date="北京时间 2026-07-16",
        )
        row = harness._get_cache_row("news:2026-07-16:1")
        if row is None:
            self.fail("Expected new cache row to be stored")
        page = harness._cache_news_page(row)

        if page is None:
            self.fail("Expected new cache row to reconstruct a news page")
        self.assertEqual(page.beijing_date, "北京时间 2026-07-16")


if __name__ == "__main__":
    _ = unittest.main()
