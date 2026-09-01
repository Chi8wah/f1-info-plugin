from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from f1_info_plugin.config import (
    DriverContextConfig,
    DriverProfileConfig,
    F1InfoPluginConfig,
)
from f1_info_plugin.core import F1InfoPlugin
from f1_info_plugin.driver_context import DriverContextMixin


def build_profile(
    driver_id: str,
    name: str,
    aliases: list[str],
    *,
    team: str = "Test Team",
    info: str = "测试群聊上下文",
    number: int | None = None,
    enabled: bool = True,
) -> DriverProfileConfig:
    return DriverProfileConfig(
        driver_id=driver_id,
        enabled=enabled,
        name=name,
        number=number,
        aliases=aliases,
        team=team,
        info=info,
    )


class DriverContextHarness(DriverContextMixin):
    config: SimpleNamespace
    _driver_context_session_states: dict[str, Any]

    def __init__(
        self,
        *,
        enabled: bool = True,
        max_matched_drivers: int = 2,
        recent_user_message_limit: int = 4,
        profiles: list[DriverProfileConfig] | None = None,
    ) -> None:
        context_config = DriverContextConfig(
            enabled=enabled,
            max_matched_drivers=max_matched_drivers,
            recent_user_message_limit=recent_user_message_limit,
            profiles=profiles if profiles is not None else DriverContextConfig().profiles,
        )
        self.config = SimpleNamespace(
            plugin=SimpleNamespace(enabled=True),
            driver_context=context_config,
        )
        self._driver_context_session_states = {}


class DriverContextConfigTest(unittest.TestCase):
    def test_defaults_are_materialized_as_editable_profiles(self) -> None:
        config = F1InfoPluginConfig()
        default_config = F1InfoPlugin().get_default_config()

        self.assertEqual(
            list(F1InfoPluginConfig.model_fields),
            ["plugin", "api", "schedule_context", "news", "model", "driver_context"],
        )
        self.assertEqual(DriverContextConfig.__ui_order__, 6)
        self.assertEqual(config.plugin.config_version, "1.2.0")
        self.assertFalse(config.driver_context.enabled)
        self.assertEqual(config.driver_context.max_matched_drivers, 2)
        self.assertEqual(config.driver_context.recent_user_message_limit, 4)
        self.assertFalse(config.driver_context.reset_profiles_on_next_start)
        self.assertFalse(
            default_config["driver_context"]["reset_profiles_on_next_start"]
        )
        self.assertEqual(len(config.driver_context.profiles), 23)
        self.assertEqual(len(default_config["driver_context"]["profiles"]), 23)
        self.assertEqual(
            default_config["driver_context"]["profiles"][0]["driver_id"],
            "george_russell",
        )
        self.assertEqual(
            default_config["driver_context"]["profiles"][0]["number"],
            63,
        )
        self.assertEqual(
            {profile.team for profile in config.driver_context.profiles},
            {
                "Alpine",
                "Aston Martin",
                "Audi",
                "Cadillac",
                "Ferrari",
                "Haas F1 Team",
                "McLaren",
                "Mercedes",
                "Racing Bulls",
                "Red Bull Racing",
                "Williams",
                "Cadillac (Reserve Driver)",
            },
        )
        profiles_by_id = {
            profile.driver_id: profile for profile in config.driver_context.profiles
        }
        self.assertIn("老四", profiles_by_id["charles_leclerc"].aliases)
        self.assertEqual(profiles_by_id["charles_leclerc"].number, 16)
        self.assertEqual(profiles_by_id["lando_norris"].number, 1)
        self.assertIn("Dudududu", profiles_by_id["max_verstappen"].aliases)
        self.assertEqual(profiles_by_id["max_verstappen"].number, 3)
        self.assertEqual(
            profiles_by_id["zhou_guanyu"].team,
            "Cadillac (Reserve Driver)",
        )
        self.assertEqual(profiles_by_id["zhou_guanyu"].number, 24)
        self.assertTrue(
            all(
                not profile.info.startswith("2026 赛季")
                for profile in config.driver_context.profiles
            )
        )

    def test_match_limits_are_validated(self) -> None:
        self.assertIn(
            "1-5",
            DriverContextConfig.model_fields["max_matched_drivers"].json_schema_extra[
                "hint"
            ],
        )
        self.assertIn(
            "1-20",
            DriverContextConfig.model_fields["recent_user_message_limit"].json_schema_extra[
                "hint"
            ],
        )
        self.assertIn(
            "1-99",
            DriverProfileConfig.model_fields["number"].json_schema_extra["hint"],
        )
        with self.assertRaises(ValueError):
            DriverContextConfig(max_matched_drivers=0)
        with self.assertRaises(ValueError):
            DriverContextConfig(recent_user_message_limit=21)
        with self.assertRaises(ValueError):
            build_profile("invalid_number", "Invalid Number", [], number=0)


class DriverContextMatchingTest(unittest.TestCase):
    def test_matches_chinese_name_english_name_nickname_and_acronym(self) -> None:
        harness = DriverContextHarness()

        cases = {
            "乐扣最近怎么样": "charles_leclerc",
            "老四又P4了吗": "charles_leclerc",
            "Leclerc 这场策略如何": "charles_leclerc",
            "聊聊夏尔·勒克莱尔": "charles_leclerc",
            "LEC 这圈真快": "charles_leclerc",
            "dudududu响起来了": "max_verstappen",
            "小周今年在忙什么": "zhou_guanyu",
        }
        for text, expected_driver_id in cases.items():
            with self.subTest(text=text):
                matched = harness._match_driver_context_profiles(
                    [{"role": "user", "content": text}]
                )
                self.assertEqual(
                    [profile.driver_id for profile in matched],
                    [expected_driver_id],
                )

    def test_three_letter_acronym_is_case_insensitive_and_requires_boundaries(
        self,
    ) -> None:
        harness = DriverContextHarness()

        lower = harness._match_driver_context_profiles(
            [{"role": "user", "content": "gas 今年表现如何"}]
        )
        no_boundary = harness._match_driver_context_profiles(
            [{"role": "user", "content": "vegas 今年的赛事安排"}]
        )

        self.assertEqual(
            [profile.driver_id for profile in lower],
            ["pierre_gasly"],
        )
        self.assertEqual(no_boundary, [])

    def test_driver_numbers_match_as_tokens_but_not_inside_phone_numbers(
        self,
    ) -> None:
        harness = DriverContextHarness()

        matched = harness._match_driver_context_profiles(
            [{"role": "user", "content": "我真得磕3 16的cp吧"}]
        )
        phone_number = harness._match_driver_context_profiles(
            [{"role": "user", "content": "我的电话号码是 1331633434343"}]
        )

        self.assertEqual(
            [profile.driver_id for profile in matched],
            ["max_verstappen", "charles_leclerc"],
        )
        rendered = harness._render_driver_context(matched, planner=True)
        self.assertIn("车手：Max Verstappen\n车手号码：3", rendered)
        self.assertIn("车手：Charles Leclerc\n车手号码：16", rendered)
        self.assertEqual(phone_number, [])

    def test_only_recent_user_messages_are_scanned(self) -> None:
        harness = DriverContextHarness(recent_user_message_limit=1)
        messages = [
            {"role": "user", "content": "之前聊过乐扣"},
            {"role": "assistant", "content": "HAM 和 VER"},
            {"role": "system", "content": "皮鸭"},
            {"role": "user", "content": "现在只聊普通话题"},
        ]

        matched = harness._match_driver_context_profiles(messages)

        self.assertEqual(matched, [])

    def test_embedded_maibot_history_skips_self_and_metadata_messages(self) -> None:
        harness = DriverContextHarness(max_matched_drivers=3, recent_user_message_limit=6)
        messages = [
            {
                "role": "user",
                "content": """
<message msg_id="old-self" user="冠宇迷妹" is_self_message="true">
安东内利和诺里斯刚刚在聊。
""",
            },
            {
                "role": "user",
                "content": "时间：2026-07-29 12:01:32",
            },
            {
                "role": "user",
                "content": """
<message msg_id="current-user" user="WebUI用户">
3 和 16 你喜欢谁
""",
            },
            {
                "role": "user",
                "content": """
<system-reminder>
工具列表：1、2、3、4、5。
</system-reminder>
""",
            }
        ]

        matched = harness._match_driver_context_profiles(messages)

        self.assertEqual(
            [profile.driver_id for profile in matched],
            ["max_verstappen", "charles_leclerc"],
        )

    def test_newest_mentions_win_and_match_count_is_limited(self) -> None:
        harness = DriverContextHarness(max_matched_drivers=2)
        messages = [
            {"role": "user", "content": "先聊乐扣"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "现在比较 HAM 和 VER"},
        ]

        matched = harness._match_driver_context_profiles(messages)

        self.assertEqual(
            [profile.driver_id for profile in matched],
            ["lewis_hamilton", "max_verstappen"],
        )

    def test_structured_text_content_is_supported(self) -> None:
        harness = DriverContextHarness()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": "ignored"},
                    {"type": "text", "text": "可乐瓶今天怎么样"},
                ],
            }
        ]

        matched = harness._match_driver_context_profiles(messages)

        self.assertEqual(
            [profile.driver_id for profile in matched],
            ["franco_colapinto"],
        )

    def test_alias_shared_by_multiple_profiles_matches_all_owners(self) -> None:
        harness = DriverContextHarness(
            profiles=[
                build_profile("driver_a", "Driver A", ["共同外号"]),
                build_profile("driver_b", "Driver B", ["共同外号"]),
            ]
        )

        matched = harness._match_driver_context_profiles(
            [{"role": "user", "content": "共同外号今天怎么样"}]
        )

        self.assertEqual(
            [profile.driver_id for profile in matched],
            ["driver_a", "driver_b"],
        )

    def test_disabled_profile_does_not_match(self) -> None:
        harness = DriverContextHarness(
            profiles=[
                build_profile(
                    "disabled_driver",
                    "Disabled Driver",
                    ["隐藏车手"],
                    enabled=False,
                )
            ]
        )

        matched = harness._match_driver_context_profiles(
            [{"role": "user", "content": "隐藏车手"}]
        )

        self.assertEqual(matched, [])


class DriverContextRenderingTest(unittest.TestCase):
    def test_planner_gets_tool_guidance_and_replyer_gets_chat_guidance(self) -> None:
        harness = DriverContextHarness(
            profiles=[
                build_profile(
                    "charles_leclerc",
                    "Charles Leclerc",
                    ["乐扣", "LEC"],
                    number=16,
                    team="Ferrari",
                    info="本群把他叫乐扣",
                )
            ]
        )
        messages = [{"role": "user", "content": "乐扣最近如何"}]

        planner_text, profiles = harness._planner_driver_context_text(messages)
        harness._remember_driver_context_profiles("session-1", profiles, monotonic_now=10)
        replyer_text = harness._render_driver_context(profiles, planner=False)

        common_prefix = (
            "【F1 车手资料补充】\n"
            "以下资料仅用于补充车手信息及相关社区梗，不代表实时赛果或官方消息。"
        )
        self.assertTrue(planner_text.startswith(common_prefix))
        self.assertTrue(replyer_text.startswith(common_prefix))
        self.assertIn("Charles Leclerc", planner_text)
        self.assertIn("Ferrari", planner_text)
        self.assertIn("本群把他叫乐扣", planner_text)
        self.assertIn("车手号码：16", planner_text)
        self.assertIn("车手号码：16", replyer_text)
        self.assertIn("F1 Tool", planner_text)
        self.assertNotIn("F1 Tool", replyer_text)
        self.assertIn("自然运用", replyer_text)


class DriverContextSessionStateTest(unittest.TestCase):
    def test_session_state_is_isolated_and_expires(self) -> None:
        harness = DriverContextHarness()
        _, profiles = harness._planner_driver_context_text(
            [{"role": "user", "content": "聊聊乐扣"}]
        )
        harness._remember_driver_context_profiles(
            "session-a", profiles, monotonic_now=100
        )

        self.assertEqual(
            [
                profile.driver_id
                for profile in harness._remembered_driver_context_profiles(
                    "session-a", monotonic_now=200
                )
            ],
            ["charles_leclerc"],
        )
        self.assertEqual(
            harness._remembered_driver_context_profiles(
                "session-b", monotonic_now=200
            ),
            (),
        )
        self.assertEqual(
            harness._remembered_driver_context_profiles(
                "session-a", monotonic_now=701
            ),
            (),
        )

    def test_empty_planner_match_clears_previous_session_state(self) -> None:
        harness = DriverContextHarness()
        _, profiles = harness._planner_driver_context_text(
            [{"role": "user", "content": "聊聊乐扣"}]
        )
        harness._remember_driver_context_profiles(
            "session-a", profiles, monotonic_now=100
        )

        harness._remember_driver_context_profiles(
            "session-a", [], monotonic_now=101
        )

        self.assertEqual(
            harness._remembered_driver_context_profiles(
                "session-a", monotonic_now=102
            ),
            (),
        )


class DriverProfileResetTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _plugin_with_pending_reset() -> F1InfoPlugin:
        plugin = F1InfoPlugin()
        config_data = plugin.get_default_config()
        config_data["driver_context"]["profiles"] = [
            build_profile(
                "custom_driver",
                "Custom Driver",
                ["自定义车手"],
                team="Custom Team",
                info="不应在重置后保留",
                number=99,
            ).model_dump(mode="python")
        ]
        config_data["driver_context"]["reset_profiles_on_next_start"] = True
        plugin.set_plugin_config(config_data)
        plugin._ctx = SimpleNamespace(logger=Mock())
        return plugin

    async def test_pending_reset_overwrites_profiles_and_clears_flag(self) -> None:
        plugin = self._plugin_with_pending_reset()

        with tempfile.TemporaryDirectory() as temporary_directory:
            plugin_root = Path(temporary_directory)
            config_path = plugin_root / "config.toml"
            config_path.write_text(
                """# 保留该注释和非车手配置\n[plugin]\nenabled = true\n\n[driver_context]\nenabled = true\nreset_profiles_on_next_start = true\n\n[[driver_context.profiles]]\ndriver_id = \"custom_driver\"\nenabled = true\nname = \"Custom Driver\"\nnumber = 99\naliases = [\"自定义车手\"]\nteam = \"Custom Team\"\ninfo = \"不应在重置后保留\"\n""",
                encoding="utf-8",
            )
            with patch("f1_info_plugin.core.PLUGIN_ROOT", plugin_root):
                await plugin._reset_driver_profiles_on_start_if_requested()

            rendered_config = config_path.read_text(encoding="utf-8")
            saved_config = tomllib.loads(rendered_config)

        self.assertIn("保留该注释", rendered_config)
        self.assertFalse(saved_config["driver_context"]["reset_profiles_on_next_start"])
        self.assertEqual(len(saved_config["driver_context"]["profiles"]), 23)
        self.assertNotIn(
            "custom_driver",
            {
                profile["driver_id"]
                for profile in saved_config["driver_context"]["profiles"]
            },
        )
        self.assertFalse(plugin.config.driver_context.reset_profiles_on_next_start)
        self.assertEqual(len(plugin.config.driver_context.profiles), 23)
        plugin.ctx.logger.info.assert_called_once_with("已恢复作者默认 F1 车手资料")

    async def test_failed_reset_keeps_current_profiles_and_pending_flag(self) -> None:
        plugin = self._plugin_with_pending_reset()

        with patch.object(
            F1InfoPlugin,
            "_write_driver_profile_reset",
            side_effect=OSError("disk unavailable"),
        ):
            await plugin._reset_driver_profiles_on_start_if_requested()

        self.assertTrue(plugin.config.driver_context.reset_profiles_on_next_start)
        self.assertEqual(
            [profile.driver_id for profile in plugin.config.driver_context.profiles],
            ["custom_driver"],
        )
        plugin.ctx.logger.exception.assert_called_once()


class DriverContextHookTest(unittest.IsolatedAsyncioTestCase):
    async def test_planner_hook_merges_system_context_and_preserves_other_kwargs(
        self,
    ) -> None:
        harness = DriverContextHarness()

        result = await F1InfoPlugin.handle_planner_driver_context_hook(
            harness,
            messages=[
                {"role": "system", "content": "base"},
                {"role": "user", "content": "聊聊潘子"},
            ],
            session_id="session-1",
            tool_definitions=[
                {"type": "function", "function": {"name": "reply"}},
                {"type": "function", "function": {"name": "f1_results"}},
            ],
            hook_name="maisaka.planner.before_request",
        )

        modified_kwargs = result["modified_kwargs"]
        messages = modified_kwargs["messages"]
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertTrue(messages[0]["content"].startswith("base\n\n"))
        self.assertIn("Max Verstappen", messages[0]["content"])
        self.assertEqual(modified_kwargs["session_id"], "session-1")
        self.assertEqual(
            modified_kwargs["tool_definitions"],
            [
                {"type": "function", "function": {"name": "reply"}},
                {"type": "function", "function": {"name": "f1_results"}},
            ],
        )
        self.assertNotIn("hook_name", modified_kwargs)
        self.assertEqual(
            [
                profile.driver_id
                for profile in harness._remembered_driver_context_profiles(
                    "session-1"
                )
            ],
            ["max_verstappen"],
        )

    async def test_planner_hook_supports_context_items_for_driver_matching(
        self,
    ) -> None:
        harness = DriverContextHarness()

        result = await F1InfoPlugin.handle_planner_driver_context_hook(
            harness,
            items=[
                {
                    "item_type": "SystemMessageItem",
                    "meta": {
                        "item_id": "system-1",
                        "logical_turn_id": None,
                        "timestamp": "2026-08-18T12:00:00+08:00",
                    },
                    "parts": [{"type": "text", "text": "base"}],
                },
                {
                    "item_type": "UserMessageItem",
                    "meta": {
                        "item_id": "user-1",
                        "logical_turn_id": None,
                        "timestamp": "2026-08-18T12:00:01+08:00",
                    },
                    "parts": [{"type": "text", "text": "聊聊潘子"}],
                },
            ],
            item_schema_version=1,
            session_id="session-items",
            tool_definitions=[
                {"type": "function", "function": {"name": "reply"}},
                {"type": "function", "function": {"name": "f1_results"}},
            ],
            hook_name="maisaka.planner.before_request",
        )

        modified_kwargs = result["modified_kwargs"]
        self.assertEqual(modified_kwargs["item_schema_version"], 1)
        self.assertEqual(modified_kwargs["session_id"], "session-items")
        self.assertIn(
            "Max Verstappen",
            modified_kwargs["items"][0]["parts"][0]["text"],
        )
        self.assertEqual(
            [
                profile.driver_id
                for profile in harness._remembered_driver_context_profiles(
                    "session-items"
                )
            ],
            ["max_verstappen"],
        )
        self.assertNotIn("hook_name", modified_kwargs)

    async def test_replyer_hook_preserves_prompt_and_uses_same_session(self) -> None:
        harness = DriverContextHarness()
        await F1InfoPlugin.handle_planner_driver_context_hook(
            harness,
            messages=[{"role": "user", "content": "乐扣的车怎么样"}],
            session_id="session-1",
            tool_definitions=[
                {"type": "function", "function": {"name": "reply"}}
            ],
        )

        result = await F1InfoPlugin.handle_replyer_driver_context_hook(
            harness,
            extra_prompt="existing requirement",
            session_id="session-1",
            request_type="reply",
            hook_name="maisaka.replyer.before_request",
        )

        modified_kwargs = result["modified_kwargs"]
        extra_prompt = modified_kwargs["extra_prompt"]
        self.assertTrue(extra_prompt.startswith("existing requirement\n\n"))
        self.assertIn("Charles Leclerc", extra_prompt)
        self.assertNotIn("F1 Tool", extra_prompt)
        self.assertEqual(modified_kwargs["session_id"], "session-1")
        self.assertEqual(modified_kwargs["request_type"], "reply")
        self.assertNotIn("hook_name", modified_kwargs)

    async def test_replyer_hook_does_not_leak_between_sessions(self) -> None:
        harness = DriverContextHarness()
        await F1InfoPlugin.handle_planner_driver_context_hook(
            harness,
            messages=[{"role": "user", "content": "乐扣"}],
            session_id="session-1",
            tool_definitions=[
                {"type": "function", "function": {"name": "reply"}}
            ],
        )

        result = await F1InfoPlugin.handle_replyer_driver_context_hook(
            harness,
            extra_prompt="existing requirement",
            session_id="session-2",
        )

        self.assertEqual(result, {"action": "continue"})

    async def test_auxiliary_planner_task_does_not_overwrite_session_state(
        self,
    ) -> None:
        harness = DriverContextHarness()
        primary_tools = [
            {"type": "function", "function": {"name": "reply"}}
        ]
        await F1InfoPlugin.handle_planner_driver_context_hook(
            harness,
            messages=[{"role": "user", "content": "聊聊潘子"}],
            session_id="session-1",
            tool_definitions=primary_tools,
        )

        result = await F1InfoPlugin.handle_planner_driver_context_hook(
            harness,
            messages=[{"role": "system", "content": "expression selector"}],
            session_id="session-1",
            tool_definitions=[],
        )

        self.assertEqual(result, {"action": "continue"})
        self.assertEqual(
            [
                profile.driver_id
                for profile in harness._remembered_driver_context_profiles(
                    "session-1"
                )
            ],
            ["max_verstappen"],
        )


if __name__ == "__main__":
    _ = unittest.main()
