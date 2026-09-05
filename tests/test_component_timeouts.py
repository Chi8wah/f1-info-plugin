"""新闻组件超时、请求截止和输出路径的回归测试。"""

from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Dict
from unittest.mock import AsyncMock, Mock, call, patch

import asyncio
import tempfile
import time
import unittest

from f1_info_plugin import core as core_module
from f1_info_plugin import http_client as http_module
from f1_info_plugin.core import F1InfoPlugin
from f1_info_plugin.models import F1ExternalApiError, NewsPageData, NewsSummaryData


def component_timeouts(plugin: F1InfoPlugin) -> Dict[str, int]:
    return {
        component["name"]: component["metadata"]["timeout_ms"]
        for component in plugin.get_components()
        if component["name"] in {"f1_daily_news", "f1_news_command"}
    }


def configured_plugin() -> F1InfoPlugin:
    plugin = F1InfoPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(
        SimpleNamespace(
            logger=Mock(),
            send=SimpleNamespace(text=AsyncMock(), image=AsyncMock()),
        )
    )
    return plugin


class NewsComponentTimeoutTest(unittest.TestCase):
    def test_tool_and_command_cover_cold_cache_query(self) -> None:
        plugin = configured_plugin()
        timeouts = component_timeouts(plugin)

        self.assertEqual(timeouts, {"f1_daily_news": 131500, "f1_news_command": 161500})
        # 复现过的 RSS 2 秒 + 模型 59 秒必须落在 Tool 的总预算内。
        self.assertGreater(timeouts["f1_daily_news"], (2 + 59) * 1000)
        self.assertEqual(len(plugin.get_components()), 14)

    def test_image_modes_cover_render_and_two_send_attempts(self) -> None:
        for mode in ("image", "both"):
            with self.subTest(mode=mode):
                plugin = configured_plugin()
                data = plugin.get_plugin_config_data()
                data["news"]["output_mode"] = mode
                plugin.set_plugin_config(data)
                timeouts = component_timeouts(plugin)
                self.assertEqual(timeouts["f1_daily_news"], 131500)
                self.assertEqual(timeouts["f1_news_command"], 201500)

    def test_discovery_reads_http_and_model_settings_before_injection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.toml").write_text(
                "[api]\nrequest_timeout_seconds = 10\nretry_count = 1\n"
                "[model]\nllm_timeout_seconds = 120\n"
                '[news]\noutput_mode = "image"\n',
                encoding="utf-8",
            )
            with patch.object(core_module, "PLUGIN_ROOT", root):
                timeouts = component_timeouts(F1InfoPlugin())
        self.assertEqual(timeouts, {"f1_daily_news": 150500, "f1_news_command": 220500})

    def test_discovery_rejects_invalid_timeout_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.toml").write_text("[api]\nretry_count = -1\n", encoding="utf-8")
            with patch.object(core_module, "PLUGIN_ROOT", root), self.assertRaises(ValueError):
                F1InfoPlugin().get_components()

    def test_retries_use_the_backoff_in_the_registered_budget(self) -> None:
        plugin = configured_plugin()
        with (
            patch.object(http_module, "urlopen", side_effect=TimeoutError("测试请求超时")) as request,
            patch.object(http_module.time, "sleep") as sleep,
            self.assertRaises(F1ExternalApiError),
        ):
            plugin._fetch_text_sync("https://example.com/feed")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0)])

    def test_sync_retry_stops_when_deadline_cannot_cover_backoff(self) -> None:
        plugin = configured_plugin()
        with (
            patch.object(http_module, "urlopen", side_effect=TimeoutError("测试请求超时")) as request,
            patch.object(http_module.time, "monotonic", side_effect=[0.0, 9.8]),
            patch.object(http_module.time, "sleep") as sleep,
            self.assertRaises(F1ExternalApiError),
        ):
            plugin._fetch_text_sync("https://example.com/feed", deadline=10.0)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["timeout"], 10.0)
        sleep.assert_not_called()


class NewsDeadlineAndOutputTest(unittest.IsolatedAsyncioTestCase):
    async def test_rss_sources_share_deadline_and_keep_completed_source(self) -> None:
        plugin = configured_plugin()
        release = Event()
        observed_deadlines = []
        xml = "<rss><channel><item><title>测试新闻</title><link>https://example.com/news</link></item></channel></rss>"

        def fetch(url: str, deadline: float) -> str:
            observed_deadlines.append(deadline)
            if url.endswith("slow"):
                release.wait(timeout=2)
            return xml

        try:
            with (
                patch.object(
                    plugin,
                    "_parse_feed_specs",
                    return_value=[
                        ("成功源", "https://example.com/fast", 1.0),
                        ("慢源", "https://example.com/slow", 1.0),
                    ],
                ),
                patch.object(plugin, "_fetch_text_sync", side_effect=fetch),
                patch.dict(F1InfoPlugin._collect_news_items.__globals__, rss_request_budget_seconds=lambda *_: 0.1),
            ):
                items = await asyncio.wait_for(plugin._collect_news_items(), timeout=1)
        finally:
            release.set()
        self.assertEqual([item.source for item in items], ["成功源"])
        self.assertEqual(len(observed_deadlines), 2)
        self.assertEqual(observed_deadlines[0], observed_deadlines[1])
        self.assertTrue(plugin.ctx.logger.warning.called)

    async def test_expired_deadline_does_not_start_http_request(self) -> None:
        plugin = configured_plugin()
        with patch.object(plugin, "_fetch_text_sync") as fetch:
            with self.assertRaises(F1ExternalApiError) as raised:
                await plugin._fetch_text("https://example.com/feed", deadline=time.monotonic() - 1)
        fetch.assert_not_called()
        self.assertEqual(raised.exception.category, "timeout")

    async def test_cancellation_remains_visible(self) -> None:
        plugin = configured_plugin()
        with patch.object(http_module.asyncio, "to_thread", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await plugin._fetch_text("https://example.com/feed", deadline=time.monotonic() + 10)

    async def test_image_send_failure_still_sends_text_with_explicit_timeout(self) -> None:
        plugin = configured_plugin()
        plugin.ctx.send.image.side_effect = TimeoutError("测试发图超时")
        page = NewsPageData("新闻", [NewsSummaryData("测试摘要", "https://example.com/news")])
        with patch.object(plugin, "_render_html_image", new=AsyncMock(return_value="图片数据")):
            await plugin._send_page_output(
                "test-session", "新闻", page, lambda _: "测试摘要", lambda _: "<html></html>", mode="image"
            )
        plugin.ctx.send.image.assert_awaited_once_with("图片数据", "test-session", rpc_timeout_ms=30000)
        plugin.ctx.send.text.assert_awaited_once_with("测试摘要", "test-session", rpc_timeout_ms=30000)

    async def test_both_mode_sends_text_and_image_with_explicit_timeout(self) -> None:
        plugin = configured_plugin()
        page = NewsPageData("新闻", [NewsSummaryData("测试摘要", "https://example.com/news")])
        with patch.object(plugin, "_render_html_image", new=AsyncMock(return_value="图片数据")):
            await plugin._send_page_output(
                "test-session", "新闻", page, lambda _: "测试摘要", lambda _: "<html></html>", mode="both"
            )
        plugin.ctx.send.text.assert_awaited_once_with("测试摘要", "test-session", rpc_timeout_ms=30000)
        plugin.ctx.send.image.assert_awaited_once_with("图片数据", "test-session", rpc_timeout_ms=30000)

    async def test_budget_change_warns_until_plugin_is_reloaded(self) -> None:
        plugin = configured_plugin()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(core_module, "CACHE_PATH", Path(directory) / "cache.json"),
            patch.object(plugin, "_load_cache", return_value={}),
            patch.object(plugin, "_load_schedule_context_cache_async", new=AsyncMock()),
        ):
            await plugin.on_load()
        registered = dict(plugin._registered_news_timeouts)
        data = plugin.get_plugin_config_data()
        data["model"]["llm_timeout_seconds"] = 120
        plugin.set_plugin_config(data)
        with (
            patch.object(plugin, "_save_cache_async", new=AsyncMock()),
            patch.object(plugin, "_restart_scheduler", new=AsyncMock()),
            patch.object(plugin, "_reconfigure_schedule_context_task", new=AsyncMock()),
        ):
            await plugin.on_config_update("self", data, "1.2.0")
        plugin.ctx.logger.warning.assert_called_once()
        self.assertIn("重载插件", plugin.ctx.logger.warning.call_args.args[0])
        self.assertEqual(plugin._registered_news_timeouts, registered)

        reloaded = F1InfoPlugin()
        reloaded.set_plugin_config(data)
        self.assertEqual(component_timeouts(reloaded)["f1_daily_news"], 191500)


if __name__ == "__main__":
    unittest.main()
