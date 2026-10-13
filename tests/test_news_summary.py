"""新闻模型 JSON 格式、修正预算和失败日志的回归测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import asyncio
import json
import unittest

from f1_info_plugin.core import F1InfoPlugin
from f1_info_plugin.models import NewsItem


URL = "https://example.com/news"
# 对应线上两次失败：正文中的 ASCII 引号没有转义。
BROKEN_RESPONSES = (
    '[{"summary":"迈凯伦车队领队称此事"不可接受"，诺里斯表示自己被"训了一顿"。",'
    f'"url":"{URL}"}}]',
    '[{"summary":"安东内利得知后惊讶地表示"真的可以吗"。",'
    f'"url":"{URL}"}}]',
)
SUMMARY = '车队领队称此次碰撞"不可接受"。'
VALID_RESPONSE = json.dumps([{"summary": SUMMARY, "url": URL}], ensure_ascii=False)


def configured_plugin(*responses: str) -> F1InfoPlugin:
    plugin = F1InfoPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(
        SimpleNamespace(
            logger=Mock(),
            llm=SimpleNamespace(
                generate=AsyncMock(side_effect=[{"success": True, "response": response} for response in responses])
            ),
        )
    )
    return plugin


def news_groups() -> list[dict]:
    return [{"score": 100, "topic": "test", "items": [NewsItem("Test", "Headline", URL, "", None, 1.0)]}]


class NewsSummaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_online_quote_failures_are_corrected_once(self) -> None:
        for broken in BROKEN_RESPONSES:
            with self.subTest(response=broken):
                plugin = configured_plugin(broken, VALID_RESPONSE)
                items = await plugin._generate_news_summary_items(news_groups(), 1)
                self.assertEqual([(item.summary, item.url) for item in items], [(SUMMARY, URL)])
                self.assertEqual(plugin.ctx.llm.generate.await_count, 2)
                repair_prompt = plugin.ctx.llm.generate.await_args_list[1].kwargs["prompt"]
                self.assertIn("仅修正格式", repair_prompt)
                self.assertIn(broken, repair_prompt)
                parse_warning = plugin.ctx.logger.warning.call_args_list[0]
                self.assertIn("JSON 解析失败", parse_warning.args[0])
                self.assertEqual(parse_warning.args[1], "Expecting ',' delimiter")
                self.assertGreater(parse_warning.args[3], 0)
                self.assertLessEqual(len(parse_warning.args[-1]), 160)

    async def test_valid_escaped_quotes_and_code_fences_need_no_retry(self) -> None:
        plugin = configured_plugin(f"```json\n{VALID_RESPONSE}\n```")
        items = await plugin._generate_news_summary_items(news_groups(), 1)
        self.assertEqual(items[0].summary, SUMMARY)
        plugin.ctx.llm.generate.assert_awaited_once()
        plugin.ctx.logger.warning.assert_not_called()

    async def test_failed_correction_is_not_retried_or_cached(self) -> None:
        plugin = configured_plugin(*BROKEN_RESPONSES)
        with patch.object(plugin, "_save_cache_async", new=AsyncMock()) as save:
            page = await plugin._news_page_data_from_groups("test-news", news_groups(), 1)
        self.assertTrue(page.using_raw_fallback)
        self.assertNotIn("test-news", plugin._cache)
        save.assert_not_awaited()
        self.assertEqual(plugin.ctx.llm.generate.await_count, 2)
        self.assertIn("格式修正后仍无有效新闻摘要", plugin.ctx.logger.warning.call_args.args[0])

    async def test_correction_uses_remaining_rpc_budget(self) -> None:
        plugin = configured_plugin(BROKEN_RESPONSES[0], VALID_RESPONSE)
        with patch.dict(
            F1InfoPlugin._request_news_summary_items.__globals__,
            time=SimpleNamespace(monotonic=Mock(side_effect=[0.0, 10.0])),
        ):
            items = await plugin._request_news_summary_items("prompt", news_groups(), 1, deadline=60.0)
        self.assertEqual(items[0].summary, SUMMARY)
        self.assertEqual(
            [call.kwargs["rpc_timeout_ms"] for call in plugin.ctx.llm.generate.await_args_list], [60000, 50000]
        )

    async def test_expired_budget_does_not_start_correction(self) -> None:
        plugin = configured_plugin(BROKEN_RESPONSES[0])
        with patch.dict(
            F1InfoPlugin._request_news_summary_items.__globals__,
            time=SimpleNamespace(monotonic=Mock(side_effect=[0.0, 60.0])),
        ):
            items = await plugin._request_news_summary_items("prompt", news_groups(), 1, deadline=60.0)
        self.assertEqual(items, [])
        plugin.ctx.llm.generate.assert_awaited_once()
        self.assertIn("预算已耗尽", plugin.ctx.logger.warning.call_args.args[0])

    async def test_correction_is_cancelled_by_total_wait_budget(self) -> None:
        plugin = configured_plugin()
        repair_cancelled = asyncio.Event()

        async def generate(**kwargs):
            if "待修正内容" not in kwargs["prompt"]:
                return {"success": True, "response": BROKEN_RESPONSES[0]}
            try:
                await asyncio.Event().wait()
            finally:
                repair_cancelled.set()

        plugin.ctx.llm.generate.side_effect = generate
        with patch.object(plugin, "_news_summary_wait_seconds", return_value=0.03):
            items = await plugin._generate_news_summary_items(news_groups(), 1)
        self.assertEqual(items, [])
        self.assertTrue(repair_cancelled.is_set())
        self.assertEqual(plugin.ctx.llm.generate.await_count, 2)
        self.assertIn("摘要超时", plugin.ctx.logger.warning.call_args.args[0])

    async def test_unsuccessful_model_result_is_logged_without_format_retry(self) -> None:
        plugin = configured_plugin()
        plugin.ctx.llm.generate.side_effect = None
        plugin.ctx.llm.generate.return_value = {"success": False, "error": "模型调用失败"}
        self.assertEqual(await plugin._generate_news_summary_items(news_groups(), 1), [])
        plugin.ctx.llm.generate.assert_awaited_once()
        self.assertIn("模型调用失败", plugin.ctx.logger.warning.call_args.args[1])

    async def test_empty_missing_array_and_invalid_items_are_logged(self) -> None:
        for response, reason in (
            ("", "空内容"),
            ("普通文本", "完整 JSON 数组"),
            ("[]", "数组为空"),
            ('["不是对象"]', "新闻条目不是对象"),
            ('[{"summary":{"text":"中文摘要"},"url":"https://example.com/news"}]', "必须是字符串"),
            ('[{"summary":"中文摘要"}]', "必须是字符串"),
        ):
            with self.subTest(response=response):
                plugin = configured_plugin()
                self.assertEqual(plugin._normalize_llm_news_response_items(response, news_groups(), 1), [])
                self.assertTrue(any(reason in call.args[0] for call in plugin.ctx.logger.warning.call_args_list))

    async def test_parser_preserves_external_cancellation(self) -> None:
        plugin = configured_plugin()
        plugin.ctx.llm.generate.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await plugin._generate_news_summary_items(news_groups(), 1)
        plugin.ctx.logger.warning.assert_not_called()

    async def test_prompt_explains_quotes_and_contains_valid_json_example(self) -> None:
        plugin = configured_plugin()
        prompt = plugin._build_news_prompt(news_groups(), 1)
        self.assertIn('转义为 \\"', prompt)
        example = prompt.split("合法示例：", 1)[1].splitlines()[0]
        self.assertEqual(json.loads(example)[0]["summary"], "车队领队称此次碰撞「不可接受」。")


if __name__ == "__main__":
    unittest.main()
