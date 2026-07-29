from __future__ import annotations
# pyright: reportAttributeAccessIssue=false, reportUninitializedInstanceVariable=false

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .constants import (
    BEIJING_TZ,
    SCHEDULE_CONTEXT_FAILURE_BACKOFF_MINUTES,
    SCHEDULE_CONTEXT_MIN_REQUEST_GAP_SECONDS,
    SCHEDULE_CONTEXT_PRE_SESSION_REFRESH_MINUTES,
    SCHEDULE_CONTEXT_RACE_LIMIT,
    SCHEDULE_CONTEXT_RACE_RETENTION_HOURS,
    SCHEDULE_CONTEXT_REFRESH_MAX_HOURS,
    SCHEDULE_CONTEXT_REFRESH_MIN_HOURS,
    UTC,
)
from .models import ScheduleContextData, ScheduleContextSessionData


class ScheduleContextMixin:
    """维护赛历感知缓存，并向 Planner/Replyer 提供统一上下文。"""

    _SCHEDULE_CONTEXT_CACHE_VERSION = 1
    _SCHEDULE_CONTEXT_CACHE_FILENAME = "schedule_context.json"

    def _schedule_context_enabled(self) -> bool:
        return bool(self.config.plugin.enabled and self.config.schedule_context.enabled)

    def _schedule_context_refresh_interval_hours(self) -> int:
        configured = int(self.config.schedule_context.refresh_interval_hours)
        return max(
            SCHEDULE_CONTEXT_REFRESH_MIN_HOURS,
            min(configured, SCHEDULE_CONTEXT_REFRESH_MAX_HOURS),
        )

    def _schedule_context_cache_path(self) -> Path:
        return Path(self.ctx.paths.data_dir) / self._SCHEDULE_CONTEXT_CACHE_FILENAME

    async def _load_schedule_context_cache_async(self) -> None:
        self._schedule_context_snapshot = await asyncio.to_thread(
            self._load_schedule_context_cache
        )

    def _load_schedule_context_cache(self) -> dict[str, Any]:
        path = self._schedule_context_cache_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.ctx.logger.warning("读取 F1 赛历感知缓存失败: %s", exc)
            return {}
        return self._normalize_schedule_context_snapshot(raw)

    @classmethod
    def _normalize_schedule_context_snapshot(cls, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        if int(raw.get("version") or 0) != cls._SCHEDULE_CONTEXT_CACHE_VERSION:
            return {}
        updated_at = cls._parse_cached_datetime(raw.get("updated_at"))
        races = raw.get("races")
        if updated_at is None or not isinstance(races, list):
            return {}

        normalized_races: list[dict[str, Any]] = []
        for race in races[:SCHEDULE_CONTEXT_RACE_LIMIT]:
            if not isinstance(race, dict):
                continue
            sessions = race.get("sessions")
            if not isinstance(sessions, list):
                continue
            normalized_sessions: list[dict[str, str]] = []
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                start_at = cls._parse_cached_datetime(session.get("start_at"))
                if start_at is None:
                    continue
                end_at = cls._parse_cached_datetime(session.get("end_at"))
                normalized_sessions.append(
                    {
                        "name": str(session.get("name") or "未知 session"),
                        "kind": str(session.get("kind") or "other"),
                        "start_at": start_at.isoformat(),
                        "end_at": end_at.isoformat() if end_at else "",
                    }
                )
            if not normalized_sessions:
                continue
            normalized_sessions.sort(key=lambda item: item["start_at"])
            normalized_races.append(
                {
                    "season": str(race.get("season") or ""),
                    "round": str(race.get("round") or ""),
                    "race_name": str(race.get("race_name") or "未知分站"),
                    "sessions": normalized_sessions,
                }
            )
        return {
            "version": cls._SCHEDULE_CONTEXT_CACHE_VERSION,
            "updated_at": updated_at.isoformat(),
            "races": normalized_races,
        }

    async def _save_schedule_context_cache_async(
        self, snapshot: dict[str, Any]
    ) -> None:
        await asyncio.to_thread(self._save_schedule_context_cache, snapshot)

    def _save_schedule_context_cache(self, snapshot: dict[str, Any]) -> None:
        path = self._schedule_context_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(f"{path.suffix}.tmp")
        temporary_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(path)

    def _start_schedule_context_task(self) -> None:
        if not self._schedule_context_enabled():
            return
        if self._schedule_context_task and not self._schedule_context_task.done():
            if self._schedule_context_wakeup:
                self._schedule_context_wakeup.set()
            return
        self._schedule_context_wakeup = asyncio.Event()
        self._schedule_context_task = asyncio.create_task(
            self._schedule_context_refresh_loop()
        )

    async def _stop_schedule_context_task(self) -> None:
        task = self._schedule_context_task
        self._schedule_context_task = None
        if self._schedule_context_wakeup:
            self._schedule_context_wakeup.set()
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _reconfigure_schedule_context_task(self) -> None:
        if self._schedule_context_enabled():
            self._start_schedule_context_task()
            return
        await self._stop_schedule_context_task()

    async def _schedule_context_refresh_loop(self) -> None:
        while self._schedule_context_enabled():
            delay = self._schedule_context_next_refresh_delay_seconds()
            if delay <= 0:
                await self._refresh_schedule_context_cache()
                continue
            if await self._wait_for_schedule_context_wakeup(delay):
                continue

    async def _wait_for_schedule_context_wakeup(self, timeout: float) -> bool:
        event = self._schedule_context_wakeup
        if event is None:
            await asyncio.sleep(timeout)
            return False
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    def _schedule_context_next_refresh_delay_seconds(
        self,
        now: datetime | None = None,
        monotonic_now: float | None = None,
    ) -> float:
        current = self._ensure_utc(now or datetime.now(UTC))
        updated_at = self._parse_cached_datetime(
            self._schedule_context_snapshot.get("updated_at")
        )
        due_times = [
            current
            if updated_at is None
            else updated_at
            + timedelta(hours=self._schedule_context_refresh_interval_hours())
        ]

        if updated_at is not None:
            lead_time = timedelta(minutes=SCHEDULE_CONTEXT_PRE_SESSION_REFRESH_MINUTES)
            for session in self._iter_cached_sessions():
                starts_at = self._parse_cached_datetime(session.get("start_at"))
                if starts_at is None or starts_at <= current:
                    continue
                refresh_at = starts_at - lead_time
                if updated_at < refresh_at:
                    due_times.append(max(refresh_at, current))

        wall_delay = max(0.0, (min(due_times) - current).total_seconds())
        current_monotonic = time.monotonic() if monotonic_now is None else monotonic_now

        if self._schedule_context_last_attempt_at is not None:
            request_gap_remaining = (
                self._schedule_context_last_attempt_at
                + SCHEDULE_CONTEXT_MIN_REQUEST_GAP_SECONDS
                - current_monotonic
            )
            wall_delay = max(wall_delay, request_gap_remaining)
        if self._schedule_context_retry_not_before is not None:
            retry_delay = self._schedule_context_retry_not_before - current_monotonic
            wall_delay = max(wall_delay, retry_delay)
        return max(0.0, wall_delay)

    async def _refresh_schedule_context_cache(self) -> bool:
        async with self._schedule_context_refresh_lock:
            monotonic_now = time.monotonic()
            if self._schedule_context_last_attempt_at is not None:
                elapsed = monotonic_now - self._schedule_context_last_attempt_at
                if elapsed < SCHEDULE_CONTEXT_MIN_REQUEST_GAP_SECONDS:
                    return False
            self._schedule_context_last_attempt_at = monotonic_now

            try:
                snapshot = await self._fetch_schedule_context_snapshot()
                await self._save_schedule_context_cache_async(snapshot)
            except Exception as exc:
                self._schedule_context_retry_not_before = monotonic_now + (
                    SCHEDULE_CONTEXT_FAILURE_BACKOFF_MINUTES * 60
                )
                self.ctx.logger.warning("刷新 F1 赛历感知缓存失败: %s", exc)
                return False

            self._schedule_context_snapshot = snapshot
            self._schedule_context_retry_not_before = None
            self.ctx.logger.info(
                "F1 赛历感知缓存已刷新: races=%s",
                len(snapshot.get("races") or []),
            )
            return True

    async def _fetch_schedule_context_snapshot(self) -> dict[str, Any]:
        races = await self._fetch_jolpica_races_for_season("current")
        selected_races = self._select_schedule_context_races(races, datetime.now(UTC))
        meetings_by_year = await self._fetch_schedule_context_meetings(selected_races)

        cached_races: list[dict[str, Any]] = []
        for race in selected_races:
            sessions = await self._schedule_context_sessions_for_race(
                race, meetings_by_year
            )
            if not sessions:
                continue
            cached_races.append(
                {
                    "season": str(race.get("season") or ""),
                    "round": str(race.get("round") or ""),
                    "race_name": str(race.get("raceName") or "未知分站"),
                    "sessions": sessions,
                }
            )

        return {
            "version": self._SCHEDULE_CONTEXT_CACHE_VERSION,
            "updated_at": datetime.now(UTC).isoformat(),
            "races": cached_races,
        }

    def _select_schedule_context_races(
        self,
        races: list[dict[str, Any]],
        now: datetime,
    ) -> list[dict[str, Any]]:
        current = self._ensure_utc(now)
        eligible: list[dict[str, Any]] = []
        for race in races:
            race_start = self._parse_jolpica_datetime(
                race.get("date"), race.get("time")
            )
            if race_start is None:
                race_start = self._jolpica_race_weekend_start(race)
            if race_start is None:
                continue
            if (
                race_start + timedelta(hours=SCHEDULE_CONTEXT_RACE_RETENTION_HOURS)
                >= current
            ):
                eligible.append(race)
        eligible.sort(key=self._jolpica_round_sort_key)
        return eligible[:SCHEDULE_CONTEXT_RACE_LIMIT]

    async def _fetch_schedule_context_meetings(
        self,
        races: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        meetings_by_year: dict[str, list[dict[str, Any]]] = {}
        for year in sorted(
            {str(race.get("season") or "") for race in races if race.get("season")}
        ):
            try:
                data = await self._fetch_json(
                    self._openf1_api_url("meetings", {"year": year})
                )
            except Exception as exc:
                self.ctx.logger.warning(
                    "OpenF1 赛历感知会议列表获取失败，将使用 Jolpica: year=%s error=%s",
                    year,
                    exc,
                )
                meetings_by_year[year] = []
                continue
            meetings_by_year[year] = (
                [item for item in data if isinstance(item, dict)]
                if isinstance(data, list)
                else []
            )
        return meetings_by_year

    async def _schedule_context_sessions_for_race(
        self,
        race: dict[str, Any],
        meetings_by_year: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, str]]:
        year = str(race.get("season") or "")
        location = (race.get("Circuit") or {}).get("Location") or {}
        meetings = meetings_by_year.get(year) or []
        meeting = self._match_openf1_meeting(
            meetings,
            str(race.get("raceName") or ""),
            str(location.get("country") or ""),
        )
        if meeting and meeting.get("meeting_key") is not None:
            try:
                data = await self._fetch_json(
                    self._openf1_api_url(
                        "sessions", {"meeting_key": meeting["meeting_key"]}
                    )
                )
                sessions = self._schedule_context_sessions_from_openf1(data)
                if sessions:
                    return sessions
            except Exception as exc:
                self.ctx.logger.warning(
                    "OpenF1 赛历感知 session 获取失败，将使用 Jolpica: race=%s error=%s",
                    race.get("raceName") or "未知分站",
                    exc,
                )
        return self._schedule_context_sessions_from_jolpica(race)

    def _schedule_context_sessions_from_openf1(
        self, raw_sessions: Any
    ) -> list[dict[str, str]]:
        if not isinstance(raw_sessions, list):
            return []
        sessions: list[dict[str, str]] = []
        for raw in raw_sessions:
            if not isinstance(raw, dict):
                continue
            start_at = self._parse_datetime(str(raw.get("date_start") or ""))
            if start_at is None:
                continue
            end_at = self._parse_datetime(str(raw.get("date_end") or ""))
            name = self._zh_session_name(str(raw.get("session_name") or ""))
            sessions.append(
                {
                    "name": name,
                    "kind": self._schedule_session_kind(name),
                    "start_at": start_at.isoformat(),
                    "end_at": end_at.isoformat() if end_at else "",
                }
            )
        sessions.sort(key=lambda item: item["start_at"])
        return sessions

    def _schedule_context_sessions_from_jolpica(
        self, race: dict[str, Any]
    ) -> list[dict[str, str]]:
        mapping = [
            ("FirstPractice", "一练"),
            ("SecondPractice", "二练"),
            ("ThirdPractice", "三练"),
            ("SprintQualifying", "冲刺排位赛"),
            ("Sprint", "冲刺赛"),
            ("Qualifying", "排位赛"),
            ("Race", "正赛"),
        ]
        sessions: list[dict[str, str]] = []
        for key, name in mapping:
            raw = (
                {"date": race.get("date"), "time": race.get("time")}
                if key == "Race"
                else race.get(key)
            )
            if not isinstance(raw, dict):
                continue
            start_at = self._parse_jolpica_datetime(raw.get("date"), raw.get("time"))
            if start_at is None:
                continue
            sessions.append(
                {
                    "name": name,
                    "kind": self._schedule_session_kind(name),
                    "start_at": start_at.isoformat(),
                    "end_at": "",
                }
            )
        sessions.sort(key=lambda item: item["start_at"])
        return sessions

    def _build_schedule_context(
        self, now: datetime | None = None
    ) -> ScheduleContextData | None:
        snapshot = self._schedule_context_snapshot
        updated_at = self._parse_cached_datetime(snapshot.get("updated_at"))
        races = snapshot.get("races")
        if updated_at is None or not isinstance(races, list):
            return None

        current = self._ensure_utc(now or datetime.now(UTC))
        active_race: tuple[dict[str, Any], list[dict[str, Any]]] | None = None
        next_race: tuple[dict[str, Any], list[dict[str, Any]]] | None = None

        for race in races:
            if not isinstance(race, dict):
                continue
            sessions = [
                session
                for session in race.get("sessions") or []
                if isinstance(session, dict)
            ]
            sessions = [
                session
                for session in sessions
                if self._parse_cached_datetime(session.get("start_at"))
            ]
            sessions.sort(key=lambda session: str(session.get("start_at") or ""))
            if not sessions:
                continue

            first_start = self._parse_cached_datetime(sessions[0].get("start_at"))
            if first_start is None:
                continue
            week_start = self._race_week_start(first_start)
            weekend_end = max(self._cached_session_end(session) for session in sessions)
            if week_start <= current <= weekend_end:
                active_race = (race, sessions)
                break
            if next_race is None and any(
                (self._parse_cached_datetime(session.get("start_at")) or current)
                > current
                for session in sessions
            ):
                next_race = (race, sessions)

        selected = active_race or next_race
        if selected is None:
            return None
        race, sessions = selected
        is_race_week = active_race is not None
        if not is_race_week:
            sessions = [
                session
                for session in sessions
                if str(session.get("kind") or "") != "practice"
            ]

        context_sessions: list[ScheduleContextSessionData] = []
        for session in sessions:
            start_at = self._parse_cached_datetime(session.get("start_at"))
            if start_at is None:
                continue
            context_sessions.append(
                ScheduleContextSessionData(
                    name=str(session.get("name") or "未知 session"),
                    start_at=start_at,
                )
            )
        if not context_sessions:
            return None
        return ScheduleContextData(
            is_race_week=is_race_week,
            race_name=str(race.get("race_name") or "未知分站"),
            sessions=context_sessions,
            updated_at=updated_at,
        )

    @staticmethod
    def _render_schedule_context(context: ScheduleContextData) -> str:
        header = (
            "【F1 比赛周赛历｜北京时间】"
            if context.is_race_week
            else "【F1 赛历信息｜北京时间】"
        )
        race_label = "本周" if context.is_race_week else "下一站"
        lines = [header, f"{race_label}：{context.race_name}"]
        lines.extend(
            f"{session.name}：{session.start_at.astimezone(BEIJING_TZ).strftime('%m月%d日 %H:%M')}"
            for session in context.sessions
        )
        return "\n".join(lines)

    def _replyer_schedule_context_text(self, now: datetime | None = None) -> str:
        if not self._schedule_context_enabled():
            return ""
        context = self._build_schedule_context(now)
        return self._render_schedule_context(context) if context else ""

    def _planner_schedule_context_text(self, now: datetime | None = None) -> str:
        schedule_text = self._replyer_schedule_context_text(now)
        if not schedule_text:
            return ""
        return (
            f"{schedule_text}\n"
            "当对话涉及 F1 比赛、车手、车队或赛事时间，并需要更详细或实时的信息时，可以使用相应 F1 Tool。"
        )

    def _iter_cached_sessions(self) -> list[dict[str, Any]]:
        races = self._schedule_context_snapshot.get("races")
        if not isinstance(races, list):
            return []
        sessions: list[dict[str, Any]] = []
        for race in races:
            if isinstance(race, dict) and isinstance(race.get("sessions"), list):
                sessions.extend(
                    session for session in race["sessions"] if isinstance(session, dict)
                )
        return sessions

    @staticmethod
    def _race_week_start(first_session_start: datetime) -> datetime:
        beijing_start = first_session_start.astimezone(BEIJING_TZ)
        monday = beijing_start - timedelta(days=beijing_start.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)

    def _cached_session_end(self, session: dict[str, Any]) -> datetime:
        start_at = self._parse_cached_datetime(
            session.get("start_at")
        ) or datetime.min.replace(tzinfo=UTC)
        end_at = self._parse_cached_datetime(session.get("end_at"))
        if end_at is not None and end_at >= start_at:
            return end_at
        name = str(session.get("name") or "")
        duration_hours = 4 if "正赛" in name or name == "Race" else 2
        return start_at + timedelta(hours=duration_hours)

    @staticmethod
    def _parse_cached_datetime(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _ensure_utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )
