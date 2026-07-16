from __future__ import annotations
# pyright: reportAny=false, reportExplicitAny=false, reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false

import importlib.util
import sys
import types
import unittest
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

_ = load_sdk_free_module("f1_info_plugin.constants", PLUGIN_PACKAGE / "constants.py")
models_module = load_sdk_free_module("f1_info_plugin.models", PLUGIN_PACKAGE / "models.py")
schedule_module = load_sdk_free_module("f1_info_plugin.schedule", PLUGIN_PACKAGE / "schedule.py")

ScheduleMixin = schedule_module.ScheduleMixin


class ScheduleHarness(ScheduleMixin):
    config: types.SimpleNamespace
    jolpica_round_value: str
    relative_offset: int | None

    def __init__(self) -> None:
        super().__init__()
        plugin_config = types.SimpleNamespace(enabled=True)
        self.config = types.SimpleNamespace(plugin=plugin_config)
        self.jolpica_round_value = ""
        self.relative_offset = None

    async def _get_jolpica_race(self, season: str, round_value: str) -> dict[str, Any] | None:
        del season
        self.jolpica_round_value = round_value
        return {
            "season": "2026",
            "raceName": "Belgian Grand Prix",
            "Circuit": {
                "circuitName": "Circuit de Spa-Francorchamps",
                "Location": {"locality": "Spa", "country": "Belgium"},
            },
        }

    async def _get_relative_station_race(self, season: str, offset: int) -> dict[str, Any] | None:
        del season
        self.relative_offset = offset
        return None

    async def _get_openf1_sessions_for_race(self, race: dict[str, Any]) -> list[dict[str, str]]:
        del race
        return [{"name": "一练", "start_text": "2026-07-17 19:30"}]


class ScheduleRoundTest(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_round_zero_uses_next_round_when_llm_sends_zero(self) -> None:
        harness = ScheduleHarness()

        page = await harness._schedule_page_data(round_value="0", season="2026")

        self.assertIsInstance(page, models_module.SchedulePageData)
        self.assertEqual(harness.jolpica_round_value, "next")
        self.assertIsNone(harness.relative_offset)


if __name__ == "__main__":
    _ = unittest.main()
