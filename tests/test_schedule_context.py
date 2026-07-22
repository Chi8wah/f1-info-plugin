from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from f1_info_plugin.config import ScheduleContextConfig
from f1_info_plugin.constants import UTC
from f1_info_plugin.core import F1InfoPlugin
from f1_info_plugin.schedule import ScheduleMixin
from f1_info_plugin.schedule_context import ScheduleContextMixin


class ScheduleContextHarness(ScheduleContextMixin, ScheduleMixin):
    config: SimpleNamespace
    _schedule_context_snapshot: dict[str, Any]
    _schedule_context_last_attempt_at: float | None
    _schedule_context_retry_not_before: float | None

    def __init__(
        self, *, enabled: bool = True, refresh_interval_hours: int = 24
    ) -> None:
        self.config = SimpleNamespace(
            plugin=SimpleNamespace(enabled=True),
            schedule_context=SimpleNamespace(
                enabled=enabled,
                refresh_interval_hours=refresh_interval_hours,
            ),
        )
        self._schedule_context_snapshot = {}
        self._schedule_context_last_attempt_at = None
        self._schedule_context_retry_not_before = None


def build_snapshot(updated_at: datetime, first_session_at: datetime) -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": updated_at.isoformat(),
        "races": [
            {
                "season": "2026",
                "round": "13",
                "race_name": "比利时大奖赛",
                "sessions": [
                    {
                        "name": "一练",
                        "kind": "practice",
                        "start_at": first_session_at.isoformat(),
                        "end_at": (first_session_at + timedelta(hours=1)).isoformat(),
                    },
                    {
                        "name": "排位赛",
                        "kind": "other",
                        "start_at": (
                            first_session_at + timedelta(days=1, hours=2)
                        ).isoformat(),
                        "end_at": (
                            first_session_at + timedelta(days=1, hours=3)
                        ).isoformat(),
                    },
                    {
                        "name": "正赛",
                        "kind": "other",
                        "start_at": (
                            first_session_at + timedelta(days=2, hours=1)
                        ).isoformat(),
                        "end_at": (
                            first_session_at + timedelta(days=2, hours=3)
                        ).isoformat(),
                    },
                ],
            }
        ],
    }


class ScheduleContextConfigTest(unittest.TestCase):
    def test_defaults_are_opt_in_and_twenty_four_hours(self) -> None:
        config = ScheduleContextConfig()

        self.assertFalse(config.enabled)
        self.assertEqual(config.refresh_interval_hours, 24)

    def test_refresh_interval_rejects_high_frequency_and_stale_values(self) -> None:
        with self.assertRaises(ValueError):
            ScheduleContextConfig(refresh_interval_hours=5)
        with self.assertRaises(ValueError):
            ScheduleContextConfig(refresh_interval_hours=169)


class ScheduleContextRenderingTest(unittest.TestCase):
    def test_non_race_week_only_renders_non_practice_sessions(self) -> None:
        harness = ScheduleContextHarness()
        first_session = datetime(2026, 7, 24, 11, 30, tzinfo=UTC)
        harness._schedule_context_snapshot = build_snapshot(
            datetime(2026, 7, 10, 0, 0, tzinfo=UTC),
            first_session,
        )

        text = harness._replyer_schedule_context_text(
            datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
        )

        self.assertIn("下一站：比利时大奖赛", text)
        self.assertNotIn("一练", text)
        self.assertIn("排位赛", text)
        self.assertIn("正赛", text)

    def test_race_week_renders_the_full_schedule_without_next_session_wording(
        self,
    ) -> None:
        harness = ScheduleContextHarness()
        first_session = datetime(2026, 7, 24, 11, 30, tzinfo=UTC)
        harness._schedule_context_snapshot = build_snapshot(
            datetime(2026, 7, 22, 0, 0, tzinfo=UTC),
            first_session,
        )

        text = harness._replyer_schedule_context_text(
            datetime(2026, 7, 22, 2, 0, tzinfo=UTC)
        )

        self.assertIn("F1 比赛周赛历", text)
        self.assertIn("本周：比利时大奖赛", text)
        self.assertIn("一练", text)
        self.assertIn("排位赛", text)
        self.assertIn("正赛", text)
        self.assertNotIn("下一场", text)

    def test_planner_adds_tool_guidance_but_replyer_only_gets_schedule_facts(
        self,
    ) -> None:
        harness = ScheduleContextHarness()
        first_session = datetime(2026, 7, 24, 11, 30, tzinfo=UTC)
        harness._schedule_context_snapshot = build_snapshot(
            datetime(2026, 7, 22, 0, 0, tzinfo=UTC),
            first_session,
        )
        now = datetime(2026, 7, 22, 2, 0, tzinfo=UTC)

        planner_text = harness._planner_schedule_context_text(now)
        replyer_text = harness._replyer_schedule_context_text(now)

        self.assertIn("F1 Tool", planner_text)
        self.assertNotIn("F1 Tool", replyer_text)
        self.assertNotIn("核实", replyer_text)


class ScheduleContextRefreshTest(unittest.TestCase):
    def test_session_refresh_is_due_one_hour_before_start(self) -> None:
        harness = ScheduleContextHarness(refresh_interval_hours=24)
        updated_at = datetime(2026, 7, 22, 0, 0, tzinfo=UTC)
        first_session = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        harness._schedule_context_snapshot = build_snapshot(updated_at, first_session)

        delay = harness._schedule_context_next_refresh_delay_seconds(
            datetime(2026, 7, 22, 1, 0, tzinfo=UTC),
            monotonic_now=1000,
        )

        self.assertEqual(delay, 8 * 3600)

    def test_request_gap_prevents_overlapping_refreshes(self) -> None:
        harness = ScheduleContextHarness(refresh_interval_hours=6)
        updated_at = datetime(2026, 7, 21, 0, 0, tzinfo=UTC)
        first_session = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
        harness._schedule_context_snapshot = build_snapshot(updated_at, first_session)
        harness._schedule_context_last_attempt_at = 900

        delay = harness._schedule_context_next_refresh_delay_seconds(
            datetime(2026, 7, 22, 1, 0, tzinfo=UTC),
            monotonic_now=1000,
        )

        self.assertEqual(delay, 200)

    def test_cache_window_keeps_at_most_five_current_or_upcoming_races(self) -> None:
        harness = ScheduleContextHarness()
        now = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        races = []
        for index in range(7):
            race_at = now + timedelta(days=index * 7)
            races.append(
                {
                    "season": "2026",
                    "round": str(index + 1),
                    "raceName": f"第 {index + 1} 站",
                    "date": race_at.date().isoformat(),
                    "time": race_at.time().isoformat() + "Z",
                }
            )

        selected = harness._select_schedule_context_races(races, now)

        self.assertEqual(len(selected), 5)
        self.assertEqual(selected[0]["round"], "1")
        self.assertEqual(selected[-1]["round"], "5")


class ScheduleContextHookTest(unittest.IsolatedAsyncioTestCase):
    async def test_planner_hook_inserts_a_system_message(self) -> None:
        harness = ScheduleContextHarness()
        harness._schedule_context_snapshot = build_snapshot(
            datetime(2026, 7, 22, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 24, 11, 30, tzinfo=UTC),
        )
        harness._planner_schedule_context_text = lambda now=None: "planner schedule"  # type: ignore[method-assign]

        result = await F1InfoPlugin.handle_planner_schedule_context_hook(
            harness,
            messages=[
                {"role": "system", "content": "base"},
                {"role": "user", "content": "hello"},
            ],
        )

        messages = result["modified_kwargs"]["messages"]
        self.assertEqual(messages[1], {"role": "system", "content": "planner schedule"})

    async def test_replyer_hook_preserves_existing_prompt_and_adds_facts_only(
        self,
    ) -> None:
        harness = ScheduleContextHarness()
        harness._replyer_schedule_context_text = lambda now=None: "schedule facts"  # type: ignore[method-assign]

        result = await F1InfoPlugin.handle_replyer_schedule_context_hook(
            harness,
            extra_prompt="existing requirement",
        )

        extra_prompt = result["modified_kwargs"]["extra_prompt"]
        self.assertEqual(extra_prompt, "existing requirement\n\nschedule facts")


if __name__ == "__main__":
    _ = unittest.main()
