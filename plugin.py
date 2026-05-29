"""F1 资讯插件。

提供赛历、赛果和每日 F1 新闻摘要查询。
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

PLUGIN_ROOT = Path(__file__).resolve().parent
CACHE_PATH = PLUGIN_ROOT / "data" / "cache.json"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


@dataclass
class NewsItem:
    source: str
    title: str
    url: str
    description: str
    published_at: datetime | None
    weight: float


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "flag"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用 F1 资讯插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class ApiConfig(PluginConfigBase):
    """结构化数据源配置。"""

    __ui_label__ = "赛历与赛果"
    __ui_icon__ = "calendar"
    __ui_order__ = 1

    jolpica_base_url: str = Field(default="https://api.jolpi.ca/ergast/f1", description="Jolpica F1 API 基础地址")
    openf1_base_url: str = Field(default="https://api.openf1.org/v1", description="OpenF1 API 基础地址")
    request_timeout_seconds: int = Field(default=20, description="HTTP 请求超时时间", ge=3, le=120)
    retry_count: int = Field(default=2, description="HTTP 请求失败重试次数", ge=0, le=5)


class NewsConfig(PluginConfigBase):
    """新闻聚合配置。"""

    __ui_label__ = "每日新闻"
    __ui_icon__ = "newspaper"
    __ui_order__ = 2

    feeds: list[str] = Field(
        default_factory=lambda: [
            "Formula1|https://www.formula1.com/en/latest/all.xml|1.35",
            "Autosport|https://www.autosport.com/rss/f1/news/|1.10",
            "Motorsport|https://www.motorsport.com/rss/f1/news/|1.05",
            "The Race|https://www.the-race.com/rss/|1.10",
            "PlanetF1|https://www.planetf1.com/rss/|0.95",
            "BBC|https://feeds.bbci.co.uk/sport/formula1/rss.xml|1.05",
            "Guardian|https://www.theguardian.com/sport/formulaone/rss|1.00",
        ],
        description="RSS 源，格式为 来源名|URL|权重",
    )
    lookback_hours: int = Field(default=48, description="新闻候选时间窗口", ge=6, le=168)
    max_candidates_per_feed: int = Field(default=30, description="每个源最多读取多少条候选", ge=5, le=100)
    daily_limit: int = Field(default=10, description="默认每日新闻条数", ge=1, le=20)
    include_urls_in_command: bool = Field(default=True, description="显式 /f1 新闻命令是否显示来源 URL")
    scheduled_jobs: list[dict[str, Any]] = Field(
        default_factory=list,
        description="定时发布任务列表，每项包含 stream_id、time、limit、include_urls",
    )
    cache_ttl_minutes: int = Field(default=45, description="新闻摘要缓存时间", ge=5, le=1440)


class ModelConfig(PluginConfigBase):
    """LLM 摘要配置。"""

    __ui_label__ = "模型"
    __ui_icon__ = "brain"
    __ui_order__ = 3

    model_name: str = Field(default="utils", description="用于生成中文一句话摘要的模型任务名")
    temperature: float = Field(default=1, description="摘要生成温度", ge=0.0, le=2.0)
    max_tokens: int = Field(default=28000, description="摘要生成最大 token；0 表示使用任务默认值", ge=0, le=1000000)
    llm_timeout_seconds: int = Field(default=60, description="LLM 摘要超时时间", ge=5, le=300)


class F1InfoPluginConfig(PluginConfigBase):
    """F1 资讯插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)


class F1InfoPlugin(MaiBotPlugin):
    """查询 F1 赛历、赛果和每日新闻摘要。"""

    config_model = F1InfoPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, Any] = {}
        self._cache_lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._scheduler_wakeup: asyncio.Event | None = None
        self._published_schedule_keys: set[str] = set()
        self._ssl_context = ssl.create_default_context()

    async def on_load(self) -> None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._cache = await asyncio.to_thread(self._load_cache)
        self._start_scheduler()
        self.ctx.logger.info("F1 资讯插件已加载")

    async def on_unload(self) -> None:
        await self._stop_scheduler()
        await self._save_cache_async()
        self.ctx.logger.info("F1 资讯插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data, version
        await self._save_cache_async()
        await self._restart_scheduler()

    def _start_scheduler(self) -> None:
        if self._scheduler_task and not self._scheduler_task.done():
            return
        if not self._scheduled_jobs():
            return
        self._scheduler_wakeup = asyncio.Event()
        self._scheduler_task = asyncio.create_task(self._scheduled_news_loop())

    async def _restart_scheduler(self) -> None:
        await self._stop_scheduler()
        self._start_scheduler()

    async def _stop_scheduler(self) -> None:
        task = self._scheduler_task
        self._scheduler_task = None
        if self._scheduler_wakeup:
            self._scheduler_wakeup.set()
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _scheduled_news_loop(self) -> None:
        while True:
            jobs = self._scheduled_jobs()
            if not jobs:
                return
            now = datetime.now(BEIJING_TZ)
            next_run = min(self._next_scheduled_run(job["time"], now) for job in jobs)
            delay = max(1.0, (next_run - now).total_seconds())
            if await self._wait_for_scheduler_wakeup(delay):
                continue
            await self._publish_scheduled_jobs(next_run.strftime("%H:%M"))

    async def _wait_for_scheduler_wakeup(self, timeout: float) -> bool:
        event = self._scheduler_wakeup
        if event is None:
            await asyncio.sleep(timeout)
            return False
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def _publish_scheduled_jobs(self, time_text: str) -> None:
        if not self.config.plugin.enabled:
            return
        jobs = [job for job in self._scheduled_jobs() if job["time"] == time_text]
        if not jobs:
            return
        date_key = datetime.now(BEIJING_TZ).date().isoformat()
        self._published_schedule_keys = {
            key for key in self._published_schedule_keys if key.startswith(f"{date_key}:")
        }
        batches: dict[tuple[int, bool], list[dict[str, Any]]] = {}
        for job in jobs:
            publish_key = f"{date_key}:{job['time']}:{job['stream_id']}"
            if publish_key in self._published_schedule_keys:
                continue
            batches.setdefault((int(job["limit"]), bool(job["include_urls"])), []).append(job)
        for (limit, include_urls), batch in batches.items():
            try:
                text = await self._news_text(limit=limit, force_refresh=False, include_urls=include_urls)
            except Exception as exc:
                self.ctx.logger.warning("定时生成 F1 新闻失败: %s", exc)
                continue
            for job in batch:
                publish_key = f"{date_key}:{job['time']}:{job['stream_id']}"
                try:
                    await self.ctx.send.text(text, str(job["stream_id"]))
                    self._published_schedule_keys.add(publish_key)
                except Exception as exc:
                    self.ctx.logger.warning("定时发送 F1 新闻失败: %s", exc)

    def _scheduled_jobs(self) -> list[dict[str, Any]]:
        raw_jobs = self.config.news.scheduled_jobs
        if not isinstance(raw_jobs, list):
            return []
        jobs: list[dict[str, Any]] = []
        for raw in raw_jobs:
            if not isinstance(raw, dict):
                continue
            stream_id = str(raw.get("stream_id") or "").strip()
            time_text = str(raw.get("time") or "").strip()
            if not stream_id or not re.fullmatch(r"\d{1,2}:\d{2}", time_text):
                continue
            hour_text, minute_text = time_text.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                continue
            try:
                limit = int(raw.get("limit") or self.config.news.daily_limit)
            except (TypeError, ValueError):
                limit = self.config.news.daily_limit
            include_urls = self._config_bool(raw.get("include_urls"), self.config.news.include_urls_in_command)
            jobs.append(
                {
                    "stream_id": stream_id,
                    "time": f"{hour:02d}:{minute:02d}",
                    "limit": max(1, min(limit, 20)),
                    "include_urls": include_urls,
                }
            )
        return jobs

    @staticmethod
    def _next_scheduled_run(time_text: str, now: datetime) -> datetime:
        hour_text, minute_text = time_text.split(":", 1)
        candidate = now.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def _config_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return default

    @Tool(
        "f1_schedule",
        description="查询 F1 下一站或指定分站赛历，并返回各节练习、冲刺、排位、正赛的北京时间安排",
        parameters=[
            ToolParameterInfo(name="round", param_type=ToolParamType.STRING, description="分站轮次；留空或 next 表示下一站", required=False),
            ToolParameterInfo(name="season", param_type=ToolParamType.STRING, description="赛季年份；默认 current", required=False),
        ],
    )
    async def handle_schedule_tool(self, round: str = "next", season: str = "current", **kwargs: Any) -> dict[str, str]:
        del kwargs
        content = await self._schedule_text(round_value=round, season=season)
        return {"name": "f1_schedule", "content": content}

    @Tool(
        "f1_results",
        description="查询 F1 排位赛、正赛或冲刺赛结果",
        parameters=[
            ToolParameterInfo(name="session", param_type=ToolParamType.STRING, description="race/qualifying/sprint，默认 race", required=False),
            ToolParameterInfo(name="round", param_type=ToolParamType.STRING, description="分站轮次；留空或 last 表示上一站", required=False),
            ToolParameterInfo(name="season", param_type=ToolParamType.STRING, description="赛季年份；默认 current", required=False),
        ],
    )
    async def handle_results_tool(
        self, session: str = "race", round: str = "last", season: str = "current", **kwargs: Any
    ) -> dict[str, str]:
        del kwargs
        content = await self._results_text(session=session, round_value=round, season=season)
        return {"name": "f1_results", "content": content}

    @Tool(
        "f1_daily_news",
        description="查询每日最重要的 F1 新闻，一句话中文摘要加来源 URL",
        parameters=[
            ToolParameterInfo(name="limit", param_type=ToolParamType.INTEGER, description="返回条数，默认 10", required=False),
            ToolParameterInfo(name="force_refresh", param_type=ToolParamType.BOOLEAN, description="是否跳过缓存重新抓取", required=False),
        ],
    )
    async def handle_news_tool(self, limit: int = 10, force_refresh: bool = False, **kwargs: Any) -> dict[str, str]:
        del kwargs
        content = await self._news_text(limit=limit, force_refresh=force_refresh)
        return {"name": "f1_daily_news", "content": content}

    @Command("f1_schedule_command", description="查询 F1 下一站赛历", pattern=r"^(?:(?:/(?:f1_schedule|f1赛历)(?:\s+(?P<round_legacy>\S+))?)|(?:/f1\s+(?:schedule|赛历|日程|下一站)(?:\s+(?P<round_f1>\S+))?))$")
    async def handle_schedule_command(self, stream_id: str = "", matched_groups: dict[str, str] | None = None, **kwargs: Any):
        del kwargs
        groups = matched_groups or {}
        round_value = groups.get("round_legacy") or groups.get("round_f1") or "next"
        text = await self._schedule_text(round_value=round_value, season="current")
        await self.ctx.send.text(text, stream_id)
        return True, "已发送 F1 赛历", True

    @Command(
        "f1_results_command",
        description="查询 F1 赛果",
        pattern=r"^(?:(?:/(?:f1_results|f1赛果)(?:\s+(?P<session_legacy>race|qualifying|sprint|正赛|排位|冲刺))?(?:\s+(?P<round_legacy>\S+))?)|(?:/f1\s+(?:(?:results|赛果)(?:\s+(?P<session_named>race|qualifying|sprint|正赛|排位|冲刺))?|(?P<session_direct>race|qualifying|sprint|正赛|排位|冲刺))(?:\s+(?P<round_f1>\S+))?))$",
    )
    async def handle_results_command(self, stream_id: str = "", matched_groups: dict[str, str] | None = None, **kwargs: Any):
        del kwargs
        groups = matched_groups or {}
        session = groups.get("session_legacy") or groups.get("session_named") or groups.get("session_direct") or "race"
        round_value = groups.get("round_legacy") or groups.get("round_f1") or "last"
        text = await self._results_text(session=session, round_value=round_value, season="current")
        await self.ctx.send.text(text, stream_id)
        return True, "已发送 F1 赛果", True

    @Command("f1_news_command", description="查询每日 F1 新闻摘要", pattern=r"^(?:(?:/(?:f1_news|f1新闻)(?:\s+(?P<limit_legacy>\d{1,2}))?)|(?:/f1\s+(?:news|新闻|资讯)(?:\s+(?P<limit_f1>\d{1,2}))?))$")
    async def handle_news_command(self, stream_id: str = "", matched_groups: dict[str, str] | None = None, **kwargs: Any):
        del kwargs
        groups = matched_groups or {}
        raw_limit = groups.get("limit_legacy") or groups.get("limit_f1") or ""
        limit = int(raw_limit) if raw_limit.isdigit() else self.config.news.daily_limit
        text = await self._news_text(
            limit=limit,
            force_refresh=False,
            include_urls=bool(self.config.news.include_urls_in_command),
        )
        await self.ctx.send.text(text, stream_id)
        return True, "已发送 F1 新闻摘要", True

    @Command("f1_clear_cache_command", description="清除 F1 插件缓存", pattern=r"^/(?:f1_clear_cache|f1清缓存|f1\s+(?:clear_cache|clear-cache|清缓存|清除缓存|刷新缓存))$")
    async def handle_clear_cache_command(self, stream_id: str = "", **kwargs: Any):
        del kwargs
        async with self._cache_lock:
            cache_count = len(self._cache)
            self._cache.clear()
            await asyncio.to_thread(self._save_cache)
        text = f"已清除 F1 插件缓存（{cache_count} 条）。下次查询新闻会重新抓取。"
        await self.ctx.send.text(text, stream_id)
        return True, "已清除 F1 插件缓存", True

    @Command("f1_help_command", description="显示 F1 插件帮助", pattern=r"^/(?:f1|f1_help|f1帮助|f1\s+(?:help|帮助))$")
    async def handle_help_command(self, stream_id: str = "", **kwargs: Any):
        del kwargs
        text = (
            "F1 资讯插件命令：\n"
            "/f1_schedule 或 /f1 赛历：查询下一站赛历和各 session 北京时间\n"
            "/f1_results [race|qualifying|sprint] 或 /f1 排位：查询上一站正赛/排位/冲刺赛果\n"
            "/f1_news [条数] 或 /f1 新闻 [条数]：查询每日重要 F1 新闻\n"
            "/f1_clear_cache 或 /f1 清缓存：清除插件缓存，下次查询新闻会重新抓取"
        )
        await self.ctx.send.text(text, stream_id)
        return True, "已发送 F1 插件帮助", True

    async def _schedule_text(self, round_value: str = "next", season: str = "current") -> str:
        if not self.config.plugin.enabled:
            return "F1 资讯插件未启用。"
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
        lines = [f"{title}", f"举办地：{place or '未知'}", f"赛道：{circuit}", "时间安排（北京时间）："]
        for session in sessions:
            lines.append(f"- {session['name']}：{session['start_text']}")
        return "\n".join(lines)

    async def _results_text(self, session: str = "race", round_value: str = "last", season: str = "current") -> str:
        if not self.config.plugin.enabled:
            return "F1 资讯插件未启用。"
        session_key = self._normalize_session(session)
        endpoint = {"race": "results", "qualifying": "qualifying", "sprint": "sprint"}[session_key]
        round_part = round_value if round_value and round_value not in {"last", "上一站"} else "last"
        data = await self._fetch_json(f"{self.config.api.jolpica_base_url.rstrip('/')}/{season}/{round_part}/{endpoint}.json?limit=100")
        races = (((data or {}).get("MRData") or {}).get("RaceTable") or {}).get("Races") or []
        if not races:
            return "没有查询到对应赛果。"
        race = races[0]
        title_map = {"race": "正赛", "qualifying": "排位赛", "sprint": "冲刺赛"}
        title = f"{race.get('season', '')} F1 {race.get('raceName', '未知分站')} {title_map[session_key]}结果"
        lines = [title]
        if session_key == "qualifying":
            results = race.get("QualifyingResults") or []
            for row in results[:20]:
                driver = row.get("Driver") or {}
                constructor = row.get("Constructor") or {}
                times = " / ".join(x for x in [row.get("Q1"), row.get("Q2"), row.get("Q3")] if x)
                lines.append(
                    f"{row.get('position', '-')}. {driver.get('code') or self._driver_name(driver)} "
                    f"({constructor.get('name', '-')}) {times or '无成绩'}"
                )
        else:
            results = race.get("Results") or race.get("SprintResults") or []
            for row in results[:20]:
                driver = row.get("Driver") or {}
                constructor = row.get("Constructor") or {}
                time_info = row.get("Time") or {}
                race_time = time_info.get("time") or row.get("status") or ""
                points = row.get("points", "0")
                lines.append(
                    f"{row.get('positionText') or row.get('position') or '-'}. "
                    f"{driver.get('code') or self._driver_name(driver)} ({constructor.get('name', '-')}) "
                    f"{race_time}，积分 {points}"
                )
        return "\n".join(lines)

    async def _news_text(self, limit: int = 10, force_refresh: bool = False, include_urls: bool = True) -> str:
        if not self.config.plugin.enabled:
            return "F1 资讯插件未启用。"
        limit = max(1, min(int(limit or self.config.news.daily_limit), 20))
        cache_key = f"news:{datetime.now(BEIJING_TZ).date().isoformat()}:{limit}"
        cache_row = self._get_cache_row(cache_key)
        if cache_row:
            cached_text = str(cache_row.get("value") or "")
            if cached_text and not force_refresh and not self._cache_expired(cache_row):
                return cached_text if include_urls else self._remove_news_urls(cached_text)
            stale_urls = self._cache_urls(cache_row)
        else:
            stale_urls = set()

        items = await self._collect_news_items()
        if not items:
            return "暂时没有抓取到 F1 新闻候选。"
        if stale_urls:
            items = [item for item in items if self._normalize_news_url(item.url) not in stale_urls]
            if not items:
                return "暂时没有抓取到新的 F1 新闻。"
        groups = self._rank_news_groups(items)
        selected = groups[:limit]
        summary = await self._generate_news_summary(selected, limit)
        if not summary:
            summary = self._fallback_news_summary(selected, limit)
        text = f"今日 F1 重要新闻 Top {limit}\n{summary}"
        self._set_cache(
            cache_key,
            text,
            ttl_seconds=self.config.news.cache_ttl_minutes * 60,
            urls=self._extract_news_urls(text),
        )
        await self._save_cache_async()
        return text if include_urls else self._remove_news_urls(text)

    async def _get_jolpica_race(self, season: str, round_value: str) -> dict[str, Any] | None:
        round_part = round_value.strip() if round_value else "next"
        if round_part in {"下一站", "next"}:
            round_part = "next"
        url = f"{self.config.api.jolpica_base_url.rstrip('/')}/{season}/{round_part}.json"
        data = await self._fetch_json(url)
        races = (((data or {}).get("MRData") or {}).get("RaceTable") or {}).get("Races") or []
        return races[0] if races else None

    async def _get_openf1_sessions_for_race(self, race: dict[str, Any]) -> list[dict[str, str]]:
        try:
            year = str(race.get("season") or "")
            race_name = str(race.get("raceName") or "")
            location = ((race.get("Circuit") or {}).get("Location") or {})
            country = str(location.get("country") or "")
            meetings = await self._fetch_json(f"{self.config.api.openf1_base_url.rstrip('/')}/meetings?{urlencode({'year': year})}")
            if not isinstance(meetings, list):
                return []
            match = self._match_openf1_meeting(meetings, race_name, country)
            if not match:
                return []
            meeting_key = match.get("meeting_key")
            sessions = await self._fetch_json(
                f"{self.config.api.openf1_base_url.rstrip('/')}/sessions?{urlencode({'meeting_key': meeting_key})}"
            )
            if not isinstance(sessions, list):
                return []
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

    async def _collect_news_items(self) -> list[NewsItem]:
        feed_specs = self._parse_feed_specs()
        tasks = [self._fetch_feed_items(source, url, weight) for source, url, weight in feed_specs]
        chunks = await asyncio.gather(*tasks, return_exceptions=True)
        items: list[NewsItem] = []
        cutoff = time.time() - self.config.news.lookback_hours * 3600
        for chunk in chunks:
            if isinstance(chunk, BaseException):
                self.ctx.logger.warning("RSS 抓取失败: %s", chunk)
                continue
            for item in chunk:
                if item.published_at and item.published_at.timestamp() < cutoff:
                    continue
                items.append(item)
        return items

    async def _fetch_feed_items(self, source: str, url: str, weight: float) -> list[NewsItem]:
        text = await self._fetch_text(url)
        root = ET.fromstring(text.encode("utf-8"))
        if root.tag.endswith("feed"):
            return self._parse_atom_feed(root, source, weight)
        return self._parse_rss_feed(root, source, weight)

    def _parse_rss_feed(self, root: ET.Element, source: str, weight: float) -> list[NewsItem]:
        items = root.findall("./channel/item") or root.findall(".//item")
        result: list[NewsItem] = []
        for item in items[: self.config.news.max_candidates_per_feed]:
            title = self._child_text(item, ["title"])
            url = self._child_text(item, ["link"])
            description = self._child_text(item, ["description", "{http://purl.org/rss/1.0/modules/content/}encoded"])
            published = self._parse_feed_datetime(self._child_text(item, ["pubDate", "{http://purl.org/dc/elements/1.1/}date"]))
            if title and url:
                result.append(NewsItem(source, title, url, description, published, weight))
        return result

    def _parse_atom_feed(self, root: ET.Element, source: str, weight: float) -> list[NewsItem]:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        result: list[NewsItem] = []
        for entry in root.findall("a:entry", ns)[: self.config.news.max_candidates_per_feed]:
            title = self._child_text_ns(entry, ["a:title"], ns)
            link_node = entry.find("a:link[@rel='alternate']", ns) or entry.find("a:link", ns)
            url = link_node.attrib.get("href", "") if link_node is not None else ""
            description = self._child_text_ns(entry, ["a:summary", "a:content"], ns)
            published = self._parse_feed_datetime(self._child_text_ns(entry, ["a:published", "a:updated"], ns))
            if title and url:
                result.append(NewsItem(source, title, url, description, published, weight))
        return result

    def _rank_news_groups(self, items: list[NewsItem]) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for item in items:
            topic = self._topic_key(item)
            group = groups.setdefault(topic, {"topic": topic, "items": [], "sources": set(), "latest": None, "score": 0.0})
            group["items"].append(item)
            group["sources"].add(item.source)
            if item.published_at and (group["latest"] is None or item.published_at > group["latest"]):
                group["latest"] = item.published_at

        now = datetime.now(UTC)
        terms = {
            "win": 16,
            "wins": 16,
            "victory": 16,
            "retire": 18,
            "dnf": 18,
            "penalty": 16,
            "investigation": 14,
            "fine": 12,
            "title": 14,
            "championship": 14,
            "points": 10,
            "lead": 10,
            "qualifying": 12,
            "sprint": 10,
            "race": 8,
            "collision": 10,
            "strategy": 8,
            "upgrade": 8,
            "rules": 10,
            "fia": 10,
        }
        for group in groups.values():
            text = " ".join((i.title + " " + i.description).lower() for i in group["items"])
            score = len(group["sources"]) * 22 + len(group["items"]) * 2 + sum(i.weight for i in group["items"]) * 8
            for term, value in terms.items():
                if term in text:
                    score += value
            latest = group["latest"]
            if isinstance(latest, datetime):
                age_hours = max(0.0, (now - latest).total_seconds() / 3600)
                score += max(0.0, 24 - age_hours) * 0.7
            group["score"] = round(score, 1)
        return sorted(groups.values(), key=lambda g: g["score"], reverse=True)

    async def _generate_news_summary(self, groups: list[dict[str, Any]], limit: int) -> str:
        prompt = self._build_news_prompt(groups, limit)
        max_tokens = int(self.config.model.max_tokens or 0) or None
        try:
            raw = await asyncio.wait_for(
                self.ctx.llm.generate(
                    prompt=prompt,
                    model=str(self.config.model.model_name or "utils"),
                    temperature=float(self.config.model.temperature),
                    max_tokens=max_tokens,
                ),
                timeout=int(self.config.model.llm_timeout_seconds),
            )
            result = self._peel_envelope(raw)
            if not isinstance(result, dict) or not result.get("success"):
                return ""
            response = str(result.get("response") or "").strip()
            return self._normalize_llm_news_response(response, groups, limit)
        except Exception as exc:
            self.ctx.logger.warning("生成 F1 新闻摘要失败: %s", exc)
            return ""

    def _build_news_prompt(self, groups: list[dict[str, Any]], limit: int) -> str:
        blocks = []
        for idx, group in enumerate(groups[:limit], 1):
            lines = [f"候选 {idx}，重要性分数 {group['score']}，主题 {group['topic']}："]
            for item in group["items"][:4]:
                lines.append(f"来源：{item.source}")
                lines.append(f"标题：{item.title}")
                if item.description:
                    lines.append(f"导语：{item.description[:500]}")
                lines.append(f"URL：{item.url}")
            blocks.append("\n".join(lines))
        return (
            "你是 F1 中文资讯编辑。请基于下面候选新闻，输出今日最重要的 F1 新闻摘要。\n"
            "要求：\n"
            "1. 每条必须是一句话中文新闻摘要，summary 字段必须使用中文，不要只是翻译标题。\n"
            "2. 每条必须附一个来源 URL。\n"
            "3. 合并重复报道，不要编造候选材料之外的信息。\n"
            "4. 输出严格 JSON 数组，每个对象包含 summary 和 url 两个字符串字段。\n"
            f"5. 最多输出 {limit} 条。\n\n"
            + "\n\n".join(blocks)
        )

    def _normalize_llm_news_response(self, response: str, groups: list[dict[str, Any]], limit: int) -> str:
        if not response:
            return ""
        data: Any = None
        try:
            start = response.find("[")
            end = response.rfind("]")
            if start >= 0 and end > start:
                data = json.loads(response[start : end + 1])
        except json.JSONDecodeError:
            data = None
        selected_groups = groups[:limit]
        allowed_urls = {
            item.url
            for group in selected_groups
            for item in group.get("items", [])
            if isinstance(item, NewsItem)
        }
        lines: list[str] = []
        if isinstance(data, list):
            for idx, row in enumerate(data[:limit], 1):
                if not isinstance(row, dict):
                    continue
                group = selected_groups[idx - 1] if idx - 1 < len(selected_groups) else {}
                summary = self._clean_text(str(row.get("summary") or ""))
                url = str(row.get("url") or "").strip()
                if url not in allowed_urls:
                    if not group.get("items"):
                        continue
                    url = self._best_item(group).url
                if summary and self._contains_chinese(summary) and url:
                    lines.append(f"{idx}. {summary} {url}")
                elif group.get("items"):
                    lines.append(self._fallback_news_line(idx, group))
        if lines:
            return "\n".join(lines)

        return ""
    @staticmethod
    def _contains_chinese(text: str) -> bool:
        return any("\u4e00" <= char <= "\u9fff" for char in text)

    def _fallback_news_summary(self, groups: list[dict[str, Any]], limit: int) -> str:
        return "\n".join(
            self._fallback_news_line(idx, group)
            for idx, group in enumerate(groups[:limit], 1)
        )

    def _fallback_news_line(self, idx: int, group: dict[str, Any]) -> str:
        best = self._best_item(group)
        source_count = len(group.get("items") or [])
        source_suffix = f"等 {source_count} 个来源" if source_count > 1 else ""
        return (
            f"{idx}. 据 {best.source}{source_suffix} 报道，这是一条最新 F1 相关资讯；"
            f"中文摘要暂时生成失败，请打开来源链接查看详情：{best.url}"
        )

    def _best_item(self, group: dict[str, Any]) -> NewsItem:
        items = list(group.get("items") or [])
        return max(items, key=lambda item: (item.weight, item.published_at or datetime.min.replace(tzinfo=UTC)))

    def _topic_key(self, item: NewsItem) -> str:
        text = f"{item.title} {item.description}".lower()
        if "antonelli" in text and ("win" in text or "title" in text or "russell" in text):
            return "antonelli win title russell"
        if "russell" in text and ("retire" in text or "exit" in text or "heartbreak" in text or "fine" in text):
            return "russell retirement fallout"
        if "hamilton" in text and "verstappen" in text:
            return "hamilton verstappen duel"
        if "piastri" in text and ("collision" in text or "penalty" in text or "strategy" in text):
            return "piastri collision penalty"
        if "winners and losers" in text or "key moments" in text or "conclusions" in text:
            return "race analysis key moments"
        words = re.sub(r"[^a-z0-9]+", " ", text).split()
        stop = {"f1", "formula", "1", "the", "a", "an", "to", "of", "in", "on", "and", "as", "from", "after", "gp", "grand", "prix"}
        return " ".join([word for word in words if word not in stop][:10]) or item.title[:60]

    async def _fetch_json(self, url: str) -> Any:
        text = await self._fetch_text(url)
        return json.loads(text)

    async def _fetch_text(self, url: str) -> str:
        return await asyncio.to_thread(self._fetch_text_sync, url)

    def _fetch_text_sync(self, url: str) -> str:
        last_exc: Exception | None = None
        attempts = int(self.config.api.retry_count) + 1
        for attempt in range(attempts):
            try:
                request = Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; MaiBotF1InfoPlugin/1.0)",
                        "Accept": "application/json, application/rss+xml, application/atom+xml, text/xml, */*",
                    },
                )
                with urlopen(request, timeout=int(self.config.api.request_timeout_seconds), context=self._ssl_context) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    return response.read(1_500_000).decode(charset, errors="replace")
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"请求失败: {url} ({last_exc})")

    def _parse_feed_specs(self) -> list[tuple[str, str, float]]:
        specs = []
        for raw in self.config.news.feeds:
            parts = [part.strip() for part in str(raw).split("|")]
            if len(parts) < 2:
                continue
            weight = 1.0
            if len(parts) >= 3:
                try:
                    weight = float(parts[2])
                except ValueError:
                    weight = 1.0
            specs.append((parts[0], parts[1], weight))
        return specs

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

    @staticmethod
    def _parse_jolpica_datetime(date_text: Any, time_text: Any) -> datetime | None:
        if not date_text:
            return None
        raw = f"{date_text}T{time_text or '00:00:00Z'}"
        return F1InfoPlugin._parse_datetime(raw)

    @staticmethod
    def _parse_datetime(raw: str) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None

    @staticmethod
    def _parse_feed_datetime(raw: str) -> datetime | None:
        if not raw:
            return None
        try:
            return parsedate_to_datetime(raw).astimezone(UTC)
        except (TypeError, ValueError, IndexError):
            return F1InfoPlugin._parse_datetime(raw)

    @staticmethod
    def _format_beijing(dt: datetime) -> str:
        return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _child_text(element: ET.Element, names: list[str]) -> str:
        for name in names:
            node = element.find(name)
            if node is not None and node.text:
                return F1InfoPlugin._clean_text(node.text)
        return ""

    @staticmethod
    def _child_text_ns(element: ET.Element, names: list[str], namespace: dict[str, str]) -> str:
        for name in names:
            node = element.find(name, namespace)
            if node is not None and node.text:
                return F1InfoPlugin._clean_text(node.text)
        return ""

    def _extract_news_urls(self, text: str) -> set[str]:
        return {
            normalized
            for url in re.findall(r"https?://\S+", text)
            if (normalized := self._normalize_news_url(url))
        }

    @staticmethod
    def _normalize_news_url(url: str) -> str:
        cleaned = url.strip().rstrip(".,;:，。；：)）]】>")
        if not cleaned:
            return ""
        parts = urlsplit(cleaned)
        if not parts.scheme or not parts.netloc:
            return cleaned
        query_items = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid", "ref"}
        ]
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path.rstrip("/"),
                urlencode(query_items, doseq=True),
                "",
            )
        )

    @staticmethod
    def _remove_news_urls(text: str) -> str:
        lines = []
        for line in text.splitlines():
            line = re.sub(r"，?请打开来源链接查看详情[:：]\s*https?://\S+", "", line)
            line = re.sub(r"\s*https?://\S+", "", line).rstrip()
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _clean_text(text: str) -> str:
        text = unescape(text or "")
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    @staticmethod
    def _peel_envelope(value: Any) -> Any:
        while isinstance(value, dict) and value.get("success") is True and isinstance(value.get("result"), dict):
            value = value["result"]
        return value

    def _get_cache(self, key: str) -> Any:
        row = self._get_cache_row(key)
        if row is None:
            return None
        if self._cache_expired(row):
            self._cache.pop(key, None)
            return None
        return row.get("value")

    def _get_cache_row(self, key: str) -> dict[str, Any] | None:
        row = self._cache.get(key)
        return row if isinstance(row, dict) else None

    @staticmethod
    def _cache_expired(row: dict[str, Any]) -> bool:
        expires_at = float(row.get("expires_at") or 0)
        return bool(expires_at and expires_at < time.time())

    def _cache_urls(self, row: dict[str, Any]) -> set[str]:
        urls: set[str] = set()
        raw_urls = row.get("urls")
        if isinstance(raw_urls, list):
            urls.update(
                normalized
                for raw_url in raw_urls
                if (normalized := self._normalize_news_url(str(raw_url)))
            )
        value = row.get("value")
        if isinstance(value, str):
            urls.update(self._extract_news_urls(value))
        return urls

    def _set_cache(self, key: str, value: Any, ttl_seconds: int, urls: set[str] | None = None) -> None:
        self._cache[key] = {
            "value": value,
            "expires_at": time.time() + ttl_seconds,
            "urls": sorted(urls or set()),
        }

    @staticmethod
    def _load_cache() -> dict[str, Any]:
        if not CACHE_PATH.exists():
            return {}
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    async def _save_cache_async(self) -> None:
        async with self._cache_lock:
            await asyncio.to_thread(self._save_cache)

    def _save_cache(self) -> None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")


def create_plugin() -> F1InfoPlugin:
    """创建 F1 资讯插件实例。"""

    return F1InfoPlugin()
