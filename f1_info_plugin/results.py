from __future__ import annotations
# pyright: reportAttributeAccessIssue=false

import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from .constants import (
    JOLPICA_RESULT_SESSION_BY_KEY,
    JOLPICA_RESULT_SESSION_LABEL,
    OPENF1_LATEST_RESULT_PROBE_LIMIT,
    OPENF1_LATEST_RESULT_TIMEOUT_SECONDS,
    OPENF1_RESULTS_SESSION_NAME_BY_SESSION,
    OPENF1_RESULT_SESSION_TYPES,
    UTC,
)
from .models import F1ExternalApiError, OpenF1UnavailableError, ResultRowData, ResultsPageData


class ResultsMixin:

    async def _results_page_data(self, session: str = "race", round_value: str = "last", season: str = "current") -> ResultsPageData | str:
        if not self.config.plugin.enabled:
            return "F1 资讯插件未启用。"
        result_session = self._normalize_session(session)
        relative_offset = self._relative_station_offset(round_value, include_previous_aliases=False)
        if relative_offset is not None:
            relative_race = await self._get_relative_station_race(season, relative_offset)
            if relative_race is None:
                return "没有查询到对应分站。"
            try:
                page = await self._openf1_result_page_for_race_session(relative_race, result_session)
            except Exception as exc:
                self._log_warning("OpenF1 %s 相对分站结果查询失败: %s", result_session, exc)
                page = ""
            if isinstance(page, ResultsPageData):
                return page
            round_part = str(relative_race.get("round") or "").strip()
            if not round_part:
                return "没有查询到对应赛果。"
            return await self._jolpica_results_page_data(result_session, round_part, season)
        if self._is_latest_results_round(round_value):
            try:
                page = await self._latest_openf1_result_page_for_session(result_session, season=season)
            except Exception as exc:
                self._log_warning("OpenF1 %s 最近结果查询失败: %s", result_session, exc)
                page = ""
            if isinstance(page, ResultsPageData):
                return page
        round_part = str(round_value or "").strip() if not self._is_latest_results_round(round_value) else "last"
        return await self._jolpica_results_page_data(result_session, round_part, season)

    async def _results_text(self, session: str = "race", round_value: str = "last", season: str = "current") -> str:
        page = await self._results_page_data(session=session, round_value=round_value, season=season)
        return page if isinstance(page, str) else self._render_results_text(page)

    async def _jolpica_results_page_data(self, result_session: str, round_part: str, season: str) -> ResultsPageData | str:
        endpoint = {"race": "results", "qualifying": "qualifying", "sprint": "sprint"}[result_session]
        data = await self._fetch_json(f"{self.config.api.jolpica_base_url.rstrip('/')}/{season}/{round_part}/{endpoint}.json?limit=100")
        races = (((data or {}).get("MRData") or {}).get("RaceTable") or {}).get("Races") or []
        if not races:
            return "没有查询到对应赛果。"
        race = races[0]
        title_map = {"race": "正赛", "qualifying": "排位赛", "sprint": "冲刺赛"}
        title = f"{race.get('season', '')} F1 {race.get('raceName', '未知分站')} {title_map[result_session]}结果"
        rows: list[ResultRowData] = []
        if result_session == "qualifying":
            results = race.get("QualifyingResults") or []
            for row in results:
                driver = row.get("Driver") or {}
                constructor = row.get("Constructor") or {}
                times = " / ".join(x for x in [row.get("Q1"), row.get("Q2"), row.get("Q3")] if x)
                rows.append(
                    ResultRowData(
                        position=str(row.get("position", "-")),
                        driver=str(driver.get("code") or self._driver_name(driver)),
                        constructor=str(constructor.get("name", "-")),
                        primary=str(times or "无成绩"),
                    )
                )
        else:
            results = race.get("Results") or race.get("SprintResults") or []
            for row in results:
                driver = row.get("Driver") or {}
                constructor = row.get("Constructor") or {}
                time_info = row.get("Time") or {}
                race_time = time_info.get("time") or row.get("status") or ""
                points = row.get("points", "0")
                rows.append(
                    ResultRowData(
                        position=str(row.get("positionText") or row.get("position") or "-"),
                        driver=str(driver.get("code") or self._driver_name(driver)),
                        constructor=str(constructor.get("name", "-")),
                        primary=str(race_time),
                        meta=f"积分 {points}",
                        status=str(row.get("status") or ""),
                    )
                )
        return ResultsPageData(title=title, session=result_session, rows=rows)

    async def _jolpica_results_text(self, result_session: str, round_part: str, season: str) -> str:
        page = await self._jolpica_results_page_data(result_session, round_part, season)
        return page if isinstance(page, str) else self._render_results_text(page)

    async def _latest_jolpica_result_page_data(self, season: str, openf1_unavailable: bool) -> ResultsPageData | str:
        try:
            candidates = await self._latest_jolpica_result_candidates(season)
        except Exception as exc:
            self._log_warning("Jolpica 最近结果候选查询失败: %s", exc)
            if openf1_unavailable:
                return "OpenF1 当前不可用，且 Jolpica 回退查询失败；请稍后重试。"
            return "Jolpica 最近正式 session 回退查询失败；请稍后重试。"
        latest_missing: tuple[str, str] | None = None
        for result_session, round_part in candidates:
            try:
                page = await self._jolpica_results_page_data(result_session, round_part, season)
            except Exception as exc:
                self._log_warning("Jolpica %s 最近结果回退失败: round=%s error=%s", result_session, round_part, exc)
                continue
            if isinstance(page, ResultsPageData):
                notices = []
                if openf1_unavailable:
                    notices.append("OpenF1 当前不可用，以下显示 Jolpica 最近可用的正式 session 结果。")
                if latest_missing is not None:
                    missing_session, missing_round = latest_missing
                    label = JOLPICA_RESULT_SESSION_LABEL[missing_session]
                    notices.append(f"最新已开始的正式 session（第 {missing_round} 站{label}）结果尚未发布。")
                page.notices.extend(notices)
                return page
            if page != "没有查询到对应赛果。":
                return page
            if latest_missing is None:
                latest_missing = (result_session, round_part)
        if openf1_unavailable:
            return "OpenF1 当前不可用，且 Jolpica 暂无最近已完成的正赛、排位赛或冲刺赛结果；练习赛结果无法通过 Jolpica 恢复。"
        if latest_missing is not None:
            missing_session, missing_round = latest_missing
            label = JOLPICA_RESULT_SESSION_LABEL[missing_session]
            return f"最新已开始的正式 session（第 {missing_round} 站{label}）结果尚未发布。"
        return ""

    async def _latest_jolpica_result_text(self, season: str, openf1_unavailable: bool) -> str:
        page = await self._latest_jolpica_result_page_data(season, openf1_unavailable)
        return page if isinstance(page, str) else self._render_results_text(page)

    async def _latest_jolpica_result_candidates(self, season: str) -> list[tuple[str, str]]:
        races = await self._fetch_jolpica_races_for_season(season)
        now = datetime.now(UTC)
        candidates: list[tuple[datetime, str, str]] = []
        for race in races:
            round_part = str(race.get("round") or "").strip()
            if not round_part:
                continue
            for result_session, session_key in JOLPICA_RESULT_SESSION_BY_KEY.items():
                raw = {"date": race.get("date"), "time": race.get("time")} if session_key == "Race" else race.get(session_key)
                if not isinstance(raw, dict):
                    continue
                started_at = self._parse_jolpica_datetime(raw.get("date"), raw.get("time"))
                if started_at is not None and started_at <= now:
                    candidates.append((started_at, result_session, round_part))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [(result_session, round_part) for _, result_session, round_part in candidates]

    async def _latest_results_page_data(self, season: str = "current") -> ResultsPageData | str:
        if not self.config.plugin.enabled:
            return "F1 资讯插件未启用。"
        try:
            page = await self._latest_openf1_session_result_page(season=season)
        except Exception as exc:
            self._log_warning("OpenF1 最新结果查询失败: %s", exc)
            if self._is_openf1_unavailable_error(exc):
                return await self._latest_jolpica_result_page_data(season, openf1_unavailable=True)
            page = ""
        if isinstance(page, ResultsPageData):
            return page
        fallback_page = await self._latest_jolpica_result_page_data(season, openf1_unavailable=False)
        return fallback_page or "没有查询到最近 session 结果。"

    async def _latest_results_text(self, season: str = "current") -> str:
        page = await self._latest_results_page_data(season=season)
        return page if isinstance(page, str) else self._render_results_text(page)

    async def _latest_openf1_result_page_for_session(self, result_session: str, season: str = "current") -> ResultsPageData | str:
        session_name = OPENF1_RESULTS_SESSION_NAME_BY_SESSION[result_session]
        return await self._latest_openf1_session_result_page(season=season, target_session_names={session_name})

    async def _latest_openf1_result_text_for_session(self, result_session: str, season: str = "current") -> str:
        page = await self._latest_openf1_result_page_for_session(result_session, season=season)
        return page if isinstance(page, str) else self._render_results_text(page)

    async def _openf1_result_page_for_race_session(self, race: dict[str, Any], result_session: str) -> ResultsPageData | str:
        deadline = time.monotonic() + OPENF1_LATEST_RESULT_TIMEOUT_SECONDS
        target_session_name = OPENF1_RESULTS_SESSION_NAME_BY_SESSION[result_session]
        sessions = await self._fetch_openf1_sessions_for_race(race, deadline)
        checked_count = 0
        for session in self._openf1_completed_result_sessions(sessions):
            if str(session.get("session_name") or "") != target_session_name:
                continue
            if checked_count >= OPENF1_LATEST_RESULT_PROBE_LIMIT:
                return ""
            session_key = str(session.get("session_key") or "").strip()
            if not session_key:
                continue
            checked_count += 1
            results = await self._fetch_openf1_session_results(session_key, deadline)
            if not results:
                continue
            try:
                drivers = await self._fetch_openf1_driver_map(session_key, deadline)
            except Exception as exc:
                self._log_warning("OpenF1 drivers 查询失败: session_key=%s error=%s", session_key, exc)
                drivers = {}
            return self._format_openf1_session_results_page(session, results, drivers)
        return ""

    async def _openf1_result_text_for_race_session(self, race: dict[str, Any], result_session: str) -> str:
        page = await self._openf1_result_page_for_race_session(race, result_session)
        return page if isinstance(page, str) else self._render_results_text(page)

    async def _latest_openf1_session_result_page(
        self,
        season: str = "current",
        target_session_names: set[str] | None = None,
    ) -> ResultsPageData | str:
        deadline = time.monotonic() + OPENF1_LATEST_RESULT_TIMEOUT_SECONDS
        checked_count = 0
        seen_session_keys: set[str] = set()
        for year in self._openf1_result_year_candidates(season):
            try:
                sessions = await self._fetch_openf1_sessions_for_year(year, deadline)
            except Exception as exc:
                self._log_warning("OpenF1 sessions 查询失败: year=%s error=%s", year, exc)
                if self._is_openf1_unavailable_error(exc):
                    raise OpenF1UnavailableError("OpenF1 当前不可用或未授权") from exc
                continue
            for session in self._openf1_completed_result_sessions(sessions):
                session_name = str(session.get("session_name") or "")
                if target_session_names is not None and session_name not in target_session_names:
                    continue
                if checked_count >= OPENF1_LATEST_RESULT_PROBE_LIMIT:
                    return ""
                session_key = str(session.get("session_key") or "").strip()
                if not session_key or session_key in seen_session_keys:
                    continue
                seen_session_keys.add(session_key)
                checked_count += 1
                try:
                    results = await self._fetch_openf1_session_results(session_key, deadline)
                except Exception as exc:
                    self._log_warning("OpenF1 session_result 查询失败: session_key=%s error=%s", session_key, exc)
                    if self._is_openf1_unavailable_error(exc):
                        raise OpenF1UnavailableError("OpenF1 当前不可用或未授权") from exc
                    continue
                if not results:
                    continue
                try:
                    drivers = await self._fetch_openf1_driver_map(session_key, deadline)
                except Exception as exc:
                    self._log_warning("OpenF1 drivers 查询失败: session_key=%s error=%s", session_key, exc)
                    drivers = {}
                return self._format_openf1_session_results_page(session, results, drivers)
        return ""

    async def _latest_openf1_session_result_text(
        self,
        season: str = "current",
        target_session_names: set[str] | None = None,
    ) -> str:
        page = await self._latest_openf1_session_result_page(season=season, target_session_names=target_session_names)
        return page if isinstance(page, str) else self._render_results_text(page)

    async def _fetch_openf1_sessions_for_year(self, year: int, deadline: float | None = None) -> list[dict[str, Any]]:
        data = await self._fetch_json_with_deadline(
            self._openf1_api_url("sessions", {"year": year}),
            deadline,
        )
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    async def _fetch_openf1_session_results(self, session_key: str, deadline: float | None = None) -> list[dict[str, Any]]:
        data = await self._fetch_json_with_deadline(
            self._openf1_api_url("session_result", {"session_key": session_key}),
            deadline,
        )
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    async def _fetch_openf1_driver_map(self, session_key: str, deadline: float | None = None) -> dict[str, dict[str, Any]]:
        data = await self._fetch_json_with_deadline(
            self._openf1_api_url("drivers", {"session_key": session_key}),
            deadline,
        )
        if not isinstance(data, list):
            return {}
        return {
            str(item.get("driver_number") or ""): item
            for item in data
            if isinstance(item, dict) and str(item.get("driver_number") or "")
        }

    async def _fetch_json_with_deadline(self, url: str, deadline: float | None = None) -> Any:
        if deadline is None:
            return await self._fetch_json(url)
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise F1ExternalApiError(
                "外部数据源响应超时，请稍后重试。",
                source=self._external_source_from_url(url),
                category="timeout",
                redacted_url=self._redact_url_for_log(url),
            )
        return await self._fetch_json(url, deadline=deadline)

    def _openf1_api_url(self, endpoint: str, params: dict[str, Any]) -> str:
        base_url = self._validated_api_base_url(self.config.api.openf1_base_url, "OpenF1")
        return f"{base_url}/{endpoint}?{urlencode(params)}"

    def _format_openf1_session_results_page(
        self,
        session: dict[str, Any],
        results: list[dict[str, Any]],
        drivers: dict[str, dict[str, Any]],
    ) -> ResultsPageData:
        session_name = self._zh_session_name(str(session.get("session_name") or ""))
        location = str(session.get("location") or session.get("circuit_short_name") or "未知分站")
        year = str(session.get("year") or "")
        title = " ".join(part for part in [year, "F1", location, f"{session_name}结果"] if part)
        ended_at = self._parse_datetime(str(session.get("date_end") or ""))
        rows: list[ResultRowData] = []
        for row in sorted(results, key=self._openf1_result_position):
            driver_number = str(row.get("driver_number") or "")
            driver = drivers.get(driver_number, {})
            driver_label = str(driver.get("name_acronym") or driver.get("broadcast_name") or driver_number or "未知车手")
            team_name = str(driver.get("team_name") or "-")
            detail = self._format_openf1_result_detail(row)
            rows.append(
                ResultRowData(
                    position=str(row.get("position") or "-"),
                    driver=driver_label,
                    constructor=team_name,
                    primary=detail,
                )
            )
        return ResultsPageData(
            title=title,
            session=str(session.get("session_name") or ""),
            rows=rows,
            end_time_text=self._format_beijing(ended_at) if ended_at else "",
        )

    def _format_openf1_session_results(
        self,
        session: dict[str, Any],
        results: list[dict[str, Any]],
        drivers: dict[str, dict[str, Any]],
    ) -> str:
        return self._render_results_text(self._format_openf1_session_results_page(session, results, drivers))

    def _openf1_completed_result_sessions(self, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        completed: list[tuple[datetime, dict[str, Any]]] = []
        for session in sessions:
            if bool(session.get("is_cancelled")):
                continue
            session_type = str(session.get("session_type") or "")
            if session_type not in OPENF1_RESULT_SESSION_TYPES:
                continue
            ended_at = self._parse_datetime(str(session.get("date_end") or ""))
            if ended_at is None or ended_at > now:
                continue
            completed.append((ended_at, session))
        return [session for _, session in sorted(completed, key=lambda item: item[0], reverse=True)]

    @staticmethod
    def _openf1_result_year_candidates(season: str) -> list[int]:
        value = str(season or "current").strip().lower()
        if value not in {"", "current", "当前"}:
            try:
                return [int(value)]
            except ValueError:
                pass
        current_year = datetime.now(UTC).year
        return [current_year, current_year - 1]

    @staticmethod
    def _is_latest_results_round(round_value: Any) -> bool:
        return str(round_value or "").strip().lower() in {"", "last", "上一站", "最近", "最近完成"}

    @staticmethod
    def _openf1_result_position(row: dict[str, Any]) -> int:
        try:
            return int(row.get("position") or 999)
        except (TypeError, ValueError):
            return 999

    def _format_openf1_result_detail(self, row: dict[str, Any]) -> str:
        parts = []
        duration = self._format_openf1_duration(row.get("duration"))
        gap = self._format_openf1_gap(row.get("gap_to_leader"))
        if duration:
            parts.append(duration)
        if gap:
            parts.append(gap)
        laps = row.get("number_of_laps")
        if laps not in (None, ""):
            parts.append(f"{laps}圈")
        points = row.get("points")
        if points not in (None, ""):
            points_text = f"{float(points):g}" if isinstance(points, (int, float)) else str(points)
            parts.append(f"积分 {points_text}")
        flags = [name.upper() for name in ("dnf", "dns", "dsq") if bool(row.get(name))]
        if flags:
            parts.append("/".join(flags))
        return "，".join(parts)

    @staticmethod
    def _format_openf1_duration(value: Any) -> str:
        if isinstance(value, list):
            return " / ".join(
                formatted
                for formatted in (ResultsMixin._format_openf1_duration(item) for item in value)
                if formatted
            )
        if isinstance(value, (int, float)):
            return ResultsMixin._format_seconds(float(value))
        text = str(value or "").strip()
        return text

    @staticmethod
    def _format_openf1_gap(value: Any) -> str:
        if isinstance(value, list):
            return " / ".join(
                formatted
                for formatted in (ResultsMixin._format_openf1_gap(item) for item in value)
                if formatted
            )
        if isinstance(value, (int, float)):
            if abs(float(value)) < 0.001:
                return ""
            return f"+{float(value):.3f}"
        text = str(value or "").strip()
        return text if text and text != "0" else ""

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        if seconds >= 3600:
            hours = int(seconds // 3600)
            remainder = seconds - hours * 3600
            minutes = int(remainder // 60)
            secs = remainder - minutes * 60
            return f"{hours}:{minutes:02d}:{secs:06.3f}"
        minutes = int(seconds // 60)
        secs = seconds - minutes * 60
        return f"{minutes}:{secs:06.3f}"

    @staticmethod
    def _driver_name(driver: dict[str, Any]) -> str:
        return " ".join(x for x in [driver.get("givenName"), driver.get("familyName")] if x) or "未知车手"

    @staticmethod
    def _zh_session_name(name: str) -> str:
        mapping = {
            "Practice 1": "一练",
            "Practice 2": "二练",
            "Practice 3": "三练",
            "Sprint Qualifying": "冲刺排位赛",
            "Sprint Shootout": "冲刺排位赛",
            "Sprint": "冲刺赛",
            "Qualifying": "排位赛",
            "Race": "正赛",
        }
        return mapping.get(name, name or "未知 session")
