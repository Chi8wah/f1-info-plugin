from __future__ import annotations
# pyright: reportAny=false, reportExplicitAny=false, reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false

import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any


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

constants_module = load_sdk_free_module(
    "f1_info_plugin.constants", PLUGIN_PACKAGE / "constants.py"
)
_ = load_sdk_free_module("f1_info_plugin.models", PLUGIN_PACKAGE / "models.py")
schedule_module = load_sdk_free_module(
    "f1_info_plugin.schedule", PLUGIN_PACKAGE / "schedule.py"
)

ScheduleMixin = schedule_module.ScheduleMixin
UTC = constants_module.UTC


def race(round_number: int, start: str) -> dict[str, Any]:
    date_text, time_text = start.split("T", 1)
    return {
        "season": "2026",
        "round": str(round_number),
        "raceName": f"Race {round_number}",
        "date": date_text,
        "time": time_text,
    }


class ScheduleHarness(ScheduleMixin):
    config: types.SimpleNamespace

    def __init__(self, races: list[dict[str, Any]]) -> None:
        super().__init__()
        self.config = types.SimpleNamespace(plugin=types.SimpleNamespace(enabled=True))
        self.races = races

    async def _fetch_jolpica_races_for_season(
        self, season: str
    ) -> list[dict[str, Any]]:
        del season
        return self.races


class ScheduleRoundTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.races = [
            race(10, "2026-08-16T13:00:00Z"),
            race(11, "2026-08-30T13:00:00Z"),
            race(12, "2026-09-13T13:00:00Z"),
        ]
        self.harness = ScheduleHarness(self.races)

    async def test_zero_selects_current_race_before_race_start(self) -> None:
        selected = await self.harness._get_schedule_offset_race(
            "2026", 0, now=datetime(2026, 8, 30, 12, 59, 59, tzinfo=UTC)
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["round"], "11")

    async def test_zero_switches_to_next_race_at_race_start(self) -> None:
        selected = await self.harness._get_schedule_offset_race(
            "2026", 0, now=datetime(2026, 8, 30, 13, 0, 0, tzinfo=UTC)
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["round"], "12")

    async def test_zero_selects_next_race_between_race_weekends(self) -> None:
        selected = await self.harness._get_schedule_offset_race(
            "2026", 0, now=datetime(2026, 8, 24, 0, 0, 0, tzinfo=UTC)
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["round"], "11")

    async def test_one_selects_the_race_after_zero(self) -> None:
        selected = await self.harness._get_schedule_offset_race(
            "2026", 1, now=datetime(2026, 8, 24, 0, 0, 0, tzinfo=UTC)
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["round"], "12")

    async def test_minus_one_is_relative_to_the_same_zero_anchor(self) -> None:
        selected = await self.harness._get_schedule_offset_race(
            "2026", -1, now=datetime(2026, 8, 30, 13, 0, 0, tzinfo=UTC)
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["round"], "11")

    def test_all_signed_numbers_are_parsed_as_relative_offsets(self) -> None:
        self.assertEqual(self.harness._schedule_station_offset("0"), 0)
        self.assertEqual(self.harness._schedule_station_offset("1"), 1)
        self.assertEqual(self.harness._schedule_station_offset("+2"), 2)
        self.assertEqual(self.harness._schedule_station_offset("-3"), -3)
        self.assertIsNone(self.harness._schedule_station_offset("round-8"))


if __name__ == "__main__":
    _ = unittest.main()
