from __future__ import annotations
# pyright: reportAttributeAccessIssue=false

import re
from datetime import datetime
from typing import Any

from .constants import BEIJING_TZ, UTC
from .models import SchedulePageData, ScheduleSessionData


class ScheduleMixin:

    async def _schedule_page_data(self, round_value: str = "next", season: str = "current") -> SchedulePageData | str:
        if not self.config.plugin.enabled:
            return "F1 资讯插件未启用。"
        relative_offset = self._relative_station_offset(round_value)
        race = await self._get_relative_station_race(season, relative_offset) if relative_offset is not None else None
        if race is None and relative_offset is not None:
            return "没有查询到对应分站。"
        if race is None:
            race = await self._get_jolpica_race(season=season, round_value=round_value or "next")
        if not race:
            return "没有查询到 F1 赛历。"

        sessions = await self._get_openf1_sessions_for_race(race)
        if not sessions:
            sessions = self._sessions_from_jolpica_race(race)

        title = f"{race.get('season', '')} F1 {race.get('raceName', '未知分站')}"
        circuit = ((race.get("Circuit") or {}).get("circuitName") or "未知赛道")
        location = (race.get("Circuit") or {}).get("Location") or {}
        place = "，".join(x for x in [location.get("locality"), location.get("country")] if x)
        return SchedulePageData(
            title=title,
            place=place or "未知",
            circuit=circuit,
            sessions=[
                ScheduleSessionData(
                    name=str(session.get("name") or "未知 session"),
                    start_text=str(session.get("start_text") or "时间未知"),
                    kind=self._schedule_session_kind(str(session.get("name") or "")),
                )
                for session in sessions
            ],
        )

    async def _schedule_text(self, round_value: str = "next", season: str = "current") -> str:
        page = await self._schedule_page_data(round_value=round_value, season=season)
        return page if isinstance(page, str) else self._render_schedule_text(page)

    @staticmethod
    def _schedule_session_kind(name: str) -> str:
        if "练" in name or "Practice" in name:
            return "practice"
        if "冲刺" in name or "Sprint" in name:
            return "sprint"
        return "other"

    async def _get_jolpica_race(self, season: str, round_value: str) -> dict[str, Any] | None:
        round_part = round_value.strip() if round_value else "next"
        if round_part in {"下一站", "next"}:
            round_part = "next"
        url = f"{self.config.api.jolpica_base_url.rstrip('/')}/{season}/{round_part}.json"
        data = await self._fetch_json(url)
        races = (((data or {}).get("MRData") or {}).get("RaceTable") or {}).get("Races") or []
        return races[0] if races else None

    async def _get_relative_station_race(self, season: str, offset: int) -> dict[str, Any] | None:
        races = await self._fetch_jolpica_races_for_season(season)
        if not races:
            return None
        anchor_index = self._relative_station_anchor_index(races)
        target_index = anchor_index + offset
        if target_index < 0 or target_index >= len(races):
            return None
        return races[target_index]

    async def _fetch_jolpica_races_for_season(self, season: str) -> list[dict[str, Any]]:
        data = await self._fetch_json(f"{self.config.api.jolpica_base_url.rstrip('/')}/{season}.json?limit=100")
        races = (((data or {}).get("MRData") or {}).get("RaceTable") or {}).get("Races") or []
        if not isinstance(races, list):
            return []
        return sorted(
            [race for race in races if isinstance(race, dict)],
            key=self._jolpica_round_sort_key,
        )

    def _relative_station_anchor_index(self, races: list[dict[str, Any]]) -> int:
        now = datetime.now(UTC)
        anchor_index = 0
        for index, race in enumerate(races):
            started_at = self._jolpica_race_weekend_start(race)
            if started_at is None or started_at <= now:
                anchor_index = index
                continue
            break
        return anchor_index

    @staticmethod
    def _relative_station_offset(round_value: Any, include_previous_aliases: bool = True) -> int | None:
        value = str(round_value or "").strip().lower()
        if not value:
            return None
        aliases = {
            "0": 0,
            "本站": 0,
            "本站次": 0,
            "本轮": 0,
            "当前站": 0,
            "当前分站": 0,
            "最近站": 0,
            "最近分站": 0,
            "-1": -1,
            "上站": -1,
            "上轮": -1,
            "-2": -2,
            "上两站": -2,
            "上上站": -2,
            "上两轮": -2,
            "上上轮": -2,
        }
        if include_previous_aliases:
            aliases.update({"上一站": -1, "上一轮": -1})
        if value in aliases:
            return aliases[value]
        if re.fullmatch(r"-\d+", value):
            return int(value)
        match = re.fullmatch(r"(?:上|前)(\d+)(?:站|轮|站次|分站)", value)
        if match:
            return -int(match.group(1))
        return None

    @staticmethod
    def _jolpica_round_sort_key(race: dict[str, Any]) -> tuple[int, str]:
        try:
            return int(race.get("round") or 0), str(race.get("date") or "")
        except (TypeError, ValueError):
            return 0, str(race.get("date") or "")

    @staticmethod
    def _jolpica_race_weekend_start(race: dict[str, Any]) -> datetime | None:
        candidates = []
        for key in ("FirstPractice", "SecondPractice", "ThirdPractice", "SprintQualifying", "Sprint", "Qualifying"):
            raw = race.get(key)
            if isinstance(raw, dict):
                dt = ScheduleMixin._parse_jolpica_datetime(raw.get("date"), raw.get("time"))
                if dt:
                    candidates.append(dt)
        race_dt = ScheduleMixin._parse_jolpica_datetime(race.get("date"), race.get("time"))
        if race_dt:
            candidates.append(race_dt)
        return min(candidates) if candidates else None

    async def _get_openf1_sessions_for_race(self, race: dict[str, Any]) -> list[dict[str, str]]:
        try:
            sessions = await self._fetch_openf1_sessions_for_race(race)
            result = []
            for item in sorted(sessions, key=lambda x: str(x.get("date_start") or "")):
                start = self._parse_datetime(str(item.get("date_start") or ""))
                if not start:
                    continue
                result.append({"name": self._zh_session_name(str(item.get("session_name") or "")), "start_text": self._format_beijing(start)})
            return result
        except Exception as exc:
            self.ctx.logger.warning("OpenF1 session 补充失败: %s", exc)
            return []

    async def _fetch_openf1_sessions_for_race(self, race: dict[str, Any], deadline: float | None = None) -> list[dict[str, Any]]:
        year = str(race.get("season") or "")
        race_name = str(race.get("raceName") or "")
        location = ((race.get("Circuit") or {}).get("Location") or {})
        country = str(location.get("country") or "")
        meetings = await self._fetch_json_with_deadline(self._openf1_api_url("meetings", {"year": year}), deadline)
        if not isinstance(meetings, list):
            return []
        match = self._match_openf1_meeting(meetings, race_name, country)
        if not match:
            return []
        meeting_key = match.get("meeting_key")
        sessions = await self._fetch_json_with_deadline(self._openf1_api_url("sessions", {"meeting_key": meeting_key}), deadline)
        if not isinstance(sessions, list):
            return []
        return [item for item in sessions if isinstance(item, dict)]

    def _sessions_from_jolpica_race(self, race: dict[str, Any]) -> list[dict[str, str]]:
        mapping = [
            ("FirstPractice", "一练"),
            ("SecondPractice", "二练"),
            ("ThirdPractice", "三练"),
            ("SprintQualifying", "冲刺排位赛"),
            ("Sprint", "冲刺赛"),
            ("Qualifying", "排位赛"),
            ("Race", "正赛"),
        ]
        result: list[dict[str, str]] = []
        for key, label in mapping:
            raw = {"date": race.get("date"), "time": race.get("time")} if key == "Race" else race.get(key)
            if not isinstance(raw, dict):
                continue
            dt = self._parse_jolpica_datetime(raw.get("date"), raw.get("time"))
            if dt:
                result.append({"name": label, "start_text": self._format_beijing(dt)})
        return result

    def _match_openf1_meeting(self, meetings: list[dict[str, Any]], race_name: str, country: str) -> dict[str, Any] | None:
        race_norm = self._norm(race_name.replace("Grand Prix", ""))
        country_norm = self._norm(country)
        best: tuple[int, dict[str, Any] | None] = (0, None)
        for meeting in meetings:
            name = self._norm(str(meeting.get("meeting_name") or ""))
            official = self._norm(str(meeting.get("meeting_official_name") or ""))
            meeting_country = self._norm(str(meeting.get("country_name") or ""))
            score = 0
            if race_norm and (race_norm in name or race_norm in official):
                score += 10
            if country_norm and country_norm == meeting_country:
                score += 3
            if score > best[0]:
                best = (score, meeting)
        return best[1]

    @staticmethod
    def _normalize_session(session: str) -> str:
        value = (session or "race").strip().lower()
        if value in {"qualifying", "quali", "排位", "排位赛"}:
            return "qualifying"
        if value in {"sprint", "冲刺", "冲刺赛"}:
            return "sprint"
        return "race"

    @staticmethod
    def _parse_jolpica_datetime(date_text: Any, time_text: Any) -> datetime | None:
        if not date_text:
            return None
        raw = f"{date_text}T{time_text or '00:00:00Z'}"
        return ScheduleMixin._parse_datetime(raw)

    @staticmethod
    def _parse_datetime(raw: str) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None

    @staticmethod
    def _format_beijing(dt: datetime) -> str:
        return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
