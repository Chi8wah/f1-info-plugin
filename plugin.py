"""F1 资讯插件。

提供赛历、赛果和每日 F1 新闻摘要查询。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import ssl
import time
import tomllib
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
NEWS_FALLBACK_NOTICE = "中文摘要生成失败或超时，以下显示 RSS 原始标题/导语和来源 URL："
LLM_GENERATE_WAIT_GRACE_SECONDS = 5
NEWS_COMMAND_OUTER_GRACE_SECONDS = 5
OPENF1_RESULT_SESSION_TYPES = {"Practice", "Qualifying", "Sprint", "Race"}
OPENF1_RESULTS_SESSION_NAME_BY_SESSION = {"race": "Race", "qualifying": "Qualifying", "sprint": "Sprint"}
OPENF1_LATEST_RESULT_PROBE_LIMIT = 12
OPENF1_LATEST_RESULT_TIMEOUT_SECONDS = 45.0
JOLPICA_RESULT_SESSION_BY_KEY = {"race": "Race", "sprint": "Sprint", "qualifying": "Qualifying"}
JOLPICA_RESULT_SESSION_LABEL = {"race": "正赛", "sprint": "冲刺赛", "qualifying": "排位赛"}


class OpenF1UnavailableError(RuntimeError):
    """Raised when OpenF1 is temporarily inaccessible for the current caller."""


class F1ExternalApiError(RuntimeError):
    """External data source failure with safe user-facing metadata."""

    def __init__(
        self,
        message: str,
        *,
        source: str,
        category: str,
        redacted_url: str = "",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.category = category
        self.redacted_url = redacted_url
        self.status_code = status_code


EXTERNAL_SOURCE_LABELS = {
    "jolpica": "Jolpica",
    "openf1": "OpenF1",
    "rss": "RSS 新闻源",
    "unknown": "外部数据源",
}

EXTERNAL_CONTEXT_LABELS = {
    "schedule": "赛历",
    "results": "赛果",
    "latest_results": "最新结果",
    "news": "新闻",
    "scheduled_news": "定时新闻",
}

EXTERNAL_CATEGORY_PHRASES = {
    "rate_limited": "请求过于频繁",
    "upstream_unavailable": "当前不可用或源站响应超时",
    "timeout": "响应超时",
    "network": "网络连接失败",
    "invalid_response": "返回内容异常",
    "http_error": "请求失败",
    "unknown": "请求失败",
}


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

    enabled: bool = Field(
        default=True,
        description="是否启用 F1 资讯插件",
        json_schema_extra={"label": "启用插件", "hint": "关闭后命令、Tool 和定时发布都会返回未启用"},
    )
    config_version: str = Field(
        default="1.0.0",
        description="配置版本",
        json_schema_extra={"label": "配置版本", "hint": "用于未来配置迁移，通常不需要手动修改"},
    )


class ApiConfig(PluginConfigBase):
    """结构化数据源配置。"""

    __ui_label__ = "赛历与赛果"
    __ui_icon__ = "calendar"
    __ui_order__ = 1

    jolpica_base_url: str = Field(
        default="https://api.jolpi.ca/ergast/f1",
        description="Jolpica F1 API 基础地址",
        json_schema_extra={"label": "Jolpica API 地址", "hint": "用于查询 F1 赛历和赛果", "placeholder": "https://api.jolpi.ca/ergast/f1"},
    )
    openf1_base_url: str = Field(
        default="https://api.openf1.org/v1",
        description="OpenF1 API 基础地址",
        json_schema_extra={"label": "OpenF1 API 地址", "hint": "用于补充分站 session 时间和最近 session 结果", "placeholder": "https://api.openf1.org/v1"},
    )
    request_timeout_seconds: int = Field(
        default=20,
        description="HTTP 请求超时时间",
        ge=3,
        le=120,
        json_schema_extra={"label": "HTTP 超时时间", "hint": "抓取赛历、赛果和 RSS 时的单次请求超时秒数"},
    )
    retry_count: int = Field(
        default=2,
        description="HTTP 请求失败重试次数",
        ge=0,
        le=5,
        json_schema_extra={"label": "HTTP 重试次数", "hint": "网络请求失败后的额外重试次数"},
    )


class ScheduledNewsJobConfig(PluginConfigBase):
    """定时新闻发布任务配置。"""

    __ui_label__ = "定时新闻任务"
    __ui_icon__ = "clock"
    __ui_order__ = 0

    platform: str = Field(default="", description="平台")
    item_id: str = Field(default="", description="聊天流 ID（群号或用户 ID）")
    rule_type: str = Field(default="group", description="聊天类型（group/private）")
    time: str = Field(default="09:00", description="发布时间（北京时间 HH:MM）")
    limit: int = Field(default=10, description="新闻条数", ge=1, le=20)
    include_urls: bool = Field(default=True, description="是否显示来源 URL")


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
        json_schema_extra={"label": "RSS 新闻源", "hint": "每行格式：来源名|RSS URL|权重；权重越高排序越靠前"},
    )
    lookback_hours: int = Field(
        default=48,
        description="新闻候选时间窗口",
        ge=6,
        le=168,
        json_schema_extra={"label": "新闻候选时间窗口", "hint": "只汇总最近多少小时内发布的 RSS 新闻"},
    )
    max_candidates_per_feed: int = Field(
        default=30,
        description="每个源最多读取多少条候选",
        ge=5,
        le=100,
        json_schema_extra={"label": "单源候选上限", "hint": "每个 RSS 源最多读取多少条新闻候选"},
    )
    daily_limit: int = Field(
        default=10,
        description="默认每日新闻条数",
        ge=1,
        le=20,
        json_schema_extra={"label": "默认新闻条数", "hint": "用户没有指定条数时默认输出多少条新闻"},
    )
    include_urls_in_command: bool = Field(
        default=True,
        description="显式 /f1 新闻命令是否显示来源 URL",
        json_schema_extra={"label": "命令显示来源 URL", "hint": "关闭后 /f1 新闻 输出不带 URL；Tool 和缓存仍保留 URL"},
    )
    scheduled_jobs: list[ScheduledNewsJobConfig] = Field(
        default_factory=list,
        description="定时发布任务列表",
        json_schema_extra={"label": "定时发布任务", "hint": "点击添加后逐项填写平台、聊天流 ID、聊天类型、发布时间、条数和 URL 显示开关"},
    )
    cache_ttl_minutes: int = Field(
        default=1440,
        description="新闻摘要缓存时间（分钟）",
        ge=5,
        le=1440,
        json_schema_extra={"label": "新闻缓存时间（分钟）", "hint": "单位：分钟；缓存未过期时复用摘要，过期后重新抓取并按 URL 去重"},
    )


class ModelConfig(PluginConfigBase):
    """LLM 摘要配置。"""

    __ui_label__ = "模型"
    __ui_icon__ = "brain"
    __ui_order__ = 3

    model_name: str = Field(
        default="utils",
        description="用于生成中文一句话摘要的模型任务名",
        json_schema_extra={"label": "摘要模型任务名", "hint": "对应 MaiBot 模型配置中的任务名"},
    )
    temperature: float = Field(
        default=1,
        description="摘要生成温度",
        ge=0.0,
        le=2.0,
        json_schema_extra={"label": "摘要生成温度", "hint": "越高越发散，越低越稳定"},
    )
    max_tokens: int = Field(
        default=28000,
        description="摘要生成最大 token；0 表示使用任务默认值",
        ge=0,
        le=1000000,
        json_schema_extra={"label": "摘要最大 token", "hint": "0 表示使用模型任务默认值；过小可能导致 JSON 摘要截断"},
    )
    llm_timeout_seconds: int = Field(
        default=60,
        description="LLM 摘要超时时间",
        ge=5,
        le=300,
        json_schema_extra={"label": "摘要超时时间", "hint": "LLM 生成新闻摘要的最长等待秒数"},
    )


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

    def _news_llm_timeout_seconds(self) -> int:
        return int(self.config.model.llm_timeout_seconds)

    def _news_summary_wait_seconds(self) -> int:
        return self._news_llm_timeout_seconds() + LLM_GENERATE_WAIT_GRACE_SECONDS

    def _news_component_llm_timeout_seconds(self) -> int:
        try:
            return self._news_llm_timeout_seconds()
        except RuntimeError:
            config_path = PLUGIN_ROOT / "config.toml"
            if config_path.exists():
                config_data = tomllib.loads(config_path.read_text(encoding="utf-8"))
                model_data = config_data.get("model", {})
                if isinstance(model_data, dict) and "llm_timeout_seconds" in model_data:
                    return int(model_data["llm_timeout_seconds"])
            return int(ModelConfig().llm_timeout_seconds)

    def _news_command_timeout_ms(self) -> int:
        timeout_seconds = self._news_component_llm_timeout_seconds()
        return (timeout_seconds + LLM_GENERATE_WAIT_GRACE_SECONDS + NEWS_COMMAND_OUTER_GRACE_SECONDS) * 1000

    def get_components(self) -> list[dict[str, Any]]:
        components = super().get_components()
        for component in components:
            if component.get("name") == "f1_news_command":
                component["metadata"]["timeout_ms"] = self._news_command_timeout_ms()
                break
        return components

    def _external_source_from_url(self, url: str) -> str:
        host = (urlsplit(str(url or "")).hostname or "").lower()
        if "jolpi.ca" in host or "ergast.com" in host:
            return "jolpica"
        if "openf1.org" in host:
            return "openf1"
        if host:
            return "rss"
        return "unknown"

    @staticmethod
    def _external_category_from_exception(exc: BaseException | None) -> tuple[str, int | None]:
        if isinstance(exc, HTTPError):
            status_code = int(exc.code)
            if status_code == 429:
                return "rate_limited", status_code
            if status_code in {500, 502, 503, 504, 521, 522, 523, 524}:
                return "upstream_unavailable", status_code
            return "http_error", status_code
        if isinstance(exc, TimeoutError):
            return "timeout", None
        if isinstance(exc, (URLError, OSError)):
            message = str(exc).lower()
            if "timed out" in message or "timeout" in message:
                return "timeout", None
            return "network", None
        return "unknown", None

    def _external_api_error_from_exception(self, url: str, exc: BaseException | None) -> F1ExternalApiError:
        source = self._external_source_from_url(url)
        category, status_code = self._external_category_from_exception(exc)
        source_label = EXTERNAL_SOURCE_LABELS.get(source, EXTERNAL_SOURCE_LABELS["unknown"])
        phrase = EXTERNAL_CATEGORY_PHRASES.get(category, EXTERNAL_CATEGORY_PHRASES["unknown"])
        return F1ExternalApiError(
            f"{source_label} {phrase}，请稍后重试。",
            source=source,
            category=category,
            redacted_url=self._redact_url_for_log(url),
            status_code=status_code,
        )

    def _context_error_message(self, context: str, exc: BaseException) -> str:
        context_label = EXTERNAL_CONTEXT_LABELS.get(context, "查询")
        if isinstance(exc, F1ExternalApiError):
            source_label = EXTERNAL_SOURCE_LABELS.get(exc.source, EXTERNAL_SOURCE_LABELS["unknown"])
            phrase = EXTERNAL_CATEGORY_PHRASES.get(exc.category, EXTERNAL_CATEGORY_PHRASES["unknown"])
            return f"F1 {context_label}数据源 {source_label} {phrase}，请稍后重试。"
        return f"F1 {context_label}查询执行异常，请稍后重试。"

    def _log_external_exception(self, context: str, exc: BaseException) -> None:
        if isinstance(exc, F1ExternalApiError):
            self._log_warning(
                "F1 %s 外部接口失败: source=%s category=%s status=%s url=%s error=%s",
                context,
                exc.source,
                exc.category,
                exc.status_code if exc.status_code is not None else "-",
                exc.redacted_url or "-",
                exc.__cause__ or exc,
            )
            return
        logger_obj = getattr(getattr(self, "ctx", None), "logger", None)
        if logger_obj is not None:
            logger_obj.exception("F1 %s 执行异常: %s", context, exc)

    def _tool_error_result(self, name: str, context: str, exc: BaseException) -> dict[str, str]:
        self._log_external_exception(context, exc)
        return {"name": name, "content": self._context_error_message(context, exc)}

    async def _send_command_error(self, stream_id: str, context: str, exc: BaseException) -> tuple[bool, str, bool]:
        self._log_external_exception(context, exc)
        message = self._context_error_message(context, exc)
        if not stream_id:
            return False, message, True
        try:
            await self.ctx.send.text(message, stream_id)
        except Exception as send_exc:
            self._log_warning("发送 F1 %s 错误提示失败: %s", context, send_exc)
            return False, message, True
        return True, message, True

    async def _send_scheduled_news_error(self, batch: list[dict[str, Any]], date_key: str, exc: BaseException) -> None:
        self._log_external_exception("scheduled_news", exc)
        message = f"定时 F1 新闻发布失败：{self._context_error_message('news', exc)}"
        for job in batch:
            for stream_id in job["stream_ids"]:
                publish_key = f"{date_key}:{job['time']}:{stream_id}"
                try:
                    await self.ctx.send.text(message, stream_id)
                    self._published_schedule_keys.add(publish_key)
                except Exception as send_exc:
                    self._log_warning("发送定时 F1 新闻错误提示失败: %s", send_exc)

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
        reserved_keys: set[str] = set()
        for job in jobs:
            stream_ids = self._resolve_scheduled_job_stream_ids(job)
            pending_stream_ids: list[str] = []
            for stream_id in stream_ids:
                publish_key = f"{date_key}:{job['time']}:{stream_id}"
                if publish_key in self._published_schedule_keys or publish_key in reserved_keys:
                    continue
                reserved_keys.add(publish_key)
                pending_stream_ids.append(stream_id)
            if not pending_stream_ids:
                continue
            scheduled_dispatch = dict(job)
            scheduled_dispatch["stream_ids"] = pending_stream_ids
            batches.setdefault((int(job["limit"]), bool(job["include_urls"])), []).append(scheduled_dispatch)
        for (limit, include_urls), batch in batches.items():
            try:
                text = await self._news_text(limit=limit, force_refresh=False, include_urls=include_urls)
            except Exception as exc:
                await self._send_scheduled_news_error(batch, date_key, exc)
                continue
            for job in batch:
                for stream_id in job["stream_ids"]:
                    publish_key = f"{date_key}:{job['time']}:{stream_id}"
                    try:
                        await self.ctx.send.text(text, stream_id)
                        self._published_schedule_keys.add(publish_key)
                    except Exception as exc:
                        self.ctx.logger.warning("定时发送 F1 新闻失败: %s", exc)

    def _scheduled_jobs(self) -> list[dict[str, Any]]:
        raw_jobs = self.config.news.scheduled_jobs
        if not isinstance(raw_jobs, list):
            return []
        jobs: list[dict[str, Any]] = []
        for raw in raw_jobs:
            platform = str(self._scheduled_job_value(raw, "platform") or "").strip()
            item_id = str(self._scheduled_job_value(raw, "item_id") or "").strip()
            rule_type = str(self._scheduled_job_value(raw, "rule_type") or "group").strip().lower()
            stream_id = str(self._scheduled_job_value(raw, "stream_id") or "").strip()
            time_text = str(self._scheduled_job_value(raw, "time") or "").strip()
            if rule_type not in {"group", "private"}:
                continue
            if not stream_id and not (platform and item_id and rule_type):
                continue
            if not re.fullmatch(r"\d{1,2}:\d{2}", time_text):
                continue
            hour_text, minute_text = time_text.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                continue
            try:
                limit = int(self._scheduled_job_value(raw, "limit") or self.config.news.daily_limit)
            except (TypeError, ValueError):
                limit = self.config.news.daily_limit
            include_urls = self._config_bool(
                self._scheduled_job_value(raw, "include_urls"),
                self.config.news.include_urls_in_command,
            )
            jobs.append(
                {
                    "platform": platform,
                    "item_id": item_id,
                    "rule_type": rule_type,
                    "stream_id": stream_id,
                    "time": f"{hour:02d}:{minute:02d}",
                    "limit": max(1, min(limit, 20)),
                    "include_urls": include_urls,
                }
            )
        return jobs

    @staticmethod
    def _scheduled_job_value(raw: Any, key: str) -> Any:
        if isinstance(raw, dict):
            return raw.get(key)
        return getattr(raw, key, None)

    def _resolve_scheduled_job_stream_ids(self, job: dict[str, Any]) -> list[str]:
        platform = str(job.get("platform") or "").strip()
        item_id = str(job.get("item_id") or "").strip()
        rule_type = str(job.get("rule_type") or "").strip()
        if platform and item_id and rule_type:
            try:
                chat_manager_module = importlib.import_module("src.chat.message_receive.chat_manager")
                sessions = chat_manager_module.chat_manager.resolve_sessions_by_target(
                    platform=platform,
                    target_id=item_id,
                    chat_type=rule_type,
                )
                stream_ids = [str(session.session_id) for session in sessions if getattr(session, "session_id", "")]
                if stream_ids:
                    return stream_ids
                self.ctx.logger.warning(
                    "定时 F1 新闻未找到目标会话: platform=%s item_id=%s rule_type=%s",
                    platform,
                    item_id,
                    rule_type,
                )
            except Exception as exc:
                self.ctx.logger.warning("定时 F1 新闻解析目标会话失败: %s", exc)
        stream_id = str(job.get("stream_id") or "").strip()
        return [stream_id] if stream_id else []

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
        description="查询 F1 下一站或相对分站赛历，返回练习、冲刺、排位、正赛等 session 的北京时间安排",
        parameters=[
            ToolParameterInfo(name="round", param_type=ToolParamType.STRING, description="相对分站：0 当前/最近分站，-1 上一站，-2 上两站，负数不限；留空或 next 表示下一站；也兼容官方轮次", required=False),
            ToolParameterInfo(name="season", param_type=ToolParamType.STRING, description="赛季年份，如 2026；默认 current", required=False),
        ],
    )
    async def handle_schedule_tool(self, round: str = "next", season: str = "current", **kwargs: Any) -> dict[str, str]:
        del kwargs
        try:
            content = await self._schedule_text(round_value=round, season=season)
        except Exception as exc:
            return self._tool_error_result("f1_schedule", "schedule", exc)
        return {"name": "f1_schedule", "content": content}

    @Tool(
        "f1_results",
        description="查询最近已完成的 F1 正赛、排位赛或冲刺赛结果；也可用相对分站指定某一站。不包含练习赛，练习请用 f1_latest_results",
        parameters=[
            ToolParameterInfo(name="session", param_type=ToolParamType.STRING, description="结果类型：race 正赛、qualifying 排位赛、sprint 冲刺赛；默认 race", required=False),
            ToolParameterInfo(name="round", param_type=ToolParamType.STRING, description="相对分站：0 当前/最近分站，-1 上一站，-2 上两站，负数不限；留空或 last 表示最近已完成的同类型结果；也兼容官方轮次", required=False),
            ToolParameterInfo(name="season", param_type=ToolParamType.STRING, description="赛季年份，如 2026；默认 current", required=False),
        ],
    )
    async def handle_results_tool(
        self, session: str = "race", round: str = "last", season: str = "current", **kwargs: Any
    ) -> dict[str, str]:
        del kwargs
        try:
            content = await self._results_text(session=session, round_value=round, season=season)
        except Exception as exc:
            return self._tool_error_result("f1_results", "results", exc)
        return {"name": "f1_results", "content": content}

    @Tool(
        "f1_latest_results",
        description="查询最近一个已结束 F1 session 的结果，覆盖练习、排位、冲刺和正赛；适合不知道刚结束的是哪类 session 时使用",
        parameters=[
            ToolParameterInfo(name="season", param_type=ToolParamType.STRING, description="赛季年份，如 2026；默认 current", required=False),
        ],
    )
    async def handle_latest_results_tool(self, season: str = "current", **kwargs: Any) -> dict[str, str]:
        del kwargs
        try:
            content = await self._latest_results_text(season=season)
        except Exception as exc:
            return self._tool_error_result("f1_latest_results", "latest_results", exc)
        return {"name": "f1_latest_results", "content": content}

    @Tool(
        "f1_daily_news",
        description="查询近期最重要的 F1 新闻，返回中文摘要和来源 URL；适合新闻、转会、处罚、车队动态等资讯问题",
        parameters=[
            ToolParameterInfo(name="limit", param_type=ToolParamType.INTEGER, description="返回条数，1-20；默认 10", required=False),
            ToolParameterInfo(name="force_refresh", param_type=ToolParamType.BOOLEAN, description="是否跳过当天缓存并重新抓取 RSS；默认 false", required=False),
        ],
    )
    async def handle_news_tool(self, limit: int = 10, force_refresh: bool = False, **kwargs: Any) -> dict[str, str]:
        del kwargs
        try:
            content = await self._news_text(limit=limit, force_refresh=force_refresh)
        except Exception as exc:
            return self._tool_error_result("f1_daily_news", "news", exc)
        return {"name": "f1_daily_news", "content": content}

    @Command("f1_schedule_command", description="查询 F1 下一站赛历", pattern=r"^(?:(?:/(?:f1_schedule|f1赛历)(?:\s+(?P<round_legacy>\S+))?)|(?:/f1\s+(?:schedule|赛历|日程|下一站)(?:\s+(?P<round_f1>\S+))?))$")
    async def handle_schedule_command(self, stream_id: str = "", matched_groups: dict[str, str] | None = None, **kwargs: Any):
        del kwargs
        groups = matched_groups or {}
        round_value = groups.get("round_legacy") or groups.get("round_f1") or "next"
        try:
            text = await self._schedule_text(round_value=round_value, season="current")
        except Exception as exc:
            return await self._send_command_error(stream_id, "schedule", exc)
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
        try:
            text = await self._results_text(session=session, round_value=round_value, season="current")
        except Exception as exc:
            return await self._send_command_error(stream_id, "results", exc)
        await self.ctx.send.text(text, stream_id)
        return True, "已发送 F1 赛果", True

    @Command(
        "f1_latest_results_command",
        description="查询最近一个 F1 session 结果",
        pattern=r"^(?:(?:/(?:f1_latest_results|f1_latest_result|f1最新结果|f1最新赛果|f1最近结果|f1最近赛果))|(?:/f1\s+(?:latest|latest_result|latest_results|最新结果|最新赛果|最近结果|最近赛果)))$",
    )
    async def handle_latest_results_command(self, stream_id: str = "", **kwargs: Any):
        del kwargs
        try:
            text = await self._latest_results_text(season="current")
        except Exception as exc:
            return await self._send_command_error(stream_id, "latest_results", exc)
        await self.ctx.send.text(text, stream_id)
        return True, "已发送 F1 最新结果", True

    @Command("f1_news_command", description="查询每日 F1 新闻摘要", pattern=r"^(?:(?:/(?:f1_news|f1新闻)(?:\s+(?P<limit_legacy>\d{1,2}))?)|(?:/f1\s+(?:news|新闻|资讯)(?:\s+(?P<limit_f1>\d{1,2}))?))$")
    async def handle_news_command(self, stream_id: str = "", matched_groups: dict[str, str] | None = None, **kwargs: Any):
        del kwargs
        groups = matched_groups or {}
        raw_limit = groups.get("limit_legacy") or groups.get("limit_f1") or ""
        limit = int(raw_limit) if raw_limit.isdigit() else self.config.news.daily_limit
        try:
            text = await self._news_text(
                limit=limit,
                force_refresh=False,
                include_urls=bool(self.config.news.include_urls_in_command),
            )
        except Exception as exc:
            return await self._send_command_error(stream_id, "news", exc)
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
            "/f1 赛历 [下一站|0|-1|8]：查询下一站、相对分站或官方轮次赛历\n"
            "/f1 赛果 [正赛|排位|冲刺] [0|-1|8]：查询最近已完成赛果，或指定相对分站/官方轮次\n"
            "/f1_latest_results 或 /f1 最新结果：查询最近一个已结束 session 的结果（含练习/排位/冲刺/正赛）\n"
            "/f1_news [条数] 或 /f1 新闻 [条数]：查询每日重要 F1 新闻\n"
            "/f1_clear_cache 或 /f1 清缓存：清除插件缓存，下次查询新闻会重新抓取"
        )
        await self.ctx.send.text(text, stream_id)
        return True, "已发送 F1 插件帮助", True

    async def _schedule_text(self, round_value: str = "next", season: str = "current") -> str:
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
        lines = [f"{title}", f"举办地：{place or '未知'}", f"赛道：{circuit}", "时间安排（北京时间）："]
        for session in sessions:
            lines.append(f"- {session['name']}：{session['start_text']}")
        return "\n".join(lines)

    async def _results_text(self, session: str = "race", round_value: str = "last", season: str = "current") -> str:
        if not self.config.plugin.enabled:
            return "F1 资讯插件未启用。"
        result_session = self._normalize_session(session)
        relative_offset = self._relative_station_offset(round_value, include_previous_aliases=False)
        if relative_offset is not None:
            relative_race = await self._get_relative_station_race(season, relative_offset)
            if relative_race is None:
                return "没有查询到对应分站。"
            try:
                text = await self._openf1_result_text_for_race_session(relative_race, result_session)
            except Exception as exc:
                self._log_warning("OpenF1 %s 相对分站结果查询失败: %s", result_session, exc)
                text = ""
            if text:
                return text
            round_part = str(relative_race.get("round") or "").strip()
            if not round_part:
                return "没有查询到对应赛果。"
            return await self._jolpica_results_text(result_session, round_part, season)
        if self._is_latest_results_round(round_value):
            try:
                text = await self._latest_openf1_result_text_for_session(result_session, season=season)
            except Exception as exc:
                self._log_warning("OpenF1 %s 最近结果查询失败: %s", result_session, exc)
                text = ""
            if text:
                return text
        round_part = str(round_value or "").strip() if not self._is_latest_results_round(round_value) else "last"
        return await self._jolpica_results_text(result_session, round_part, season)

    async def _jolpica_results_text(self, result_session: str, round_part: str, season: str) -> str:
        endpoint = {"race": "results", "qualifying": "qualifying", "sprint": "sprint"}[result_session]
        data = await self._fetch_json(f"{self.config.api.jolpica_base_url.rstrip('/')}/{season}/{round_part}/{endpoint}.json?limit=100")
        races = (((data or {}).get("MRData") or {}).get("RaceTable") or {}).get("Races") or []
        if not races:
            return "没有查询到对应赛果。"
        race = races[0]
        title_map = {"race": "正赛", "qualifying": "排位赛", "sprint": "冲刺赛"}
        title = f"{race.get('season', '')} F1 {race.get('raceName', '未知分站')} {title_map[result_session]}结果"
        lines = [title]
        if result_session == "qualifying":
            results = race.get("QualifyingResults") or []
            for row in results:
                driver = row.get("Driver") or {}
                constructor = row.get("Constructor") or {}
                times = " / ".join(x for x in [row.get("Q1"), row.get("Q2"), row.get("Q3")] if x)
                lines.append(
                    f"{row.get('position', '-')}. {driver.get('code') or self._driver_name(driver)} "
                    f"({constructor.get('name', '-')}) {times or '无成绩'}"
                )
        else:
            results = race.get("Results") or race.get("SprintResults") or []
            for row in results:
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

    async def _latest_jolpica_result_text(self, season: str, openf1_unavailable: bool) -> str:
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
                text = await self._jolpica_results_text(result_session, round_part, season)
            except Exception as exc:
                self._log_warning("Jolpica %s 最近结果回退失败: round=%s error=%s", result_session, round_part, exc)
                continue
            if text and text != "没有查询到对应赛果。":
                notices = []
                if openf1_unavailable:
                    notices.append("OpenF1 当前不可用，以下显示 Jolpica 最近可用的正式 session 结果。")
                if latest_missing is not None:
                    missing_session, missing_round = latest_missing
                    label = JOLPICA_RESULT_SESSION_LABEL[missing_session]
                    notices.append(f"最新已开始的正式 session（第 {missing_round} 站{label}）结果尚未发布。")
                if notices:
                    return "\n".join(notices) + "\n" + text
                return text
            if latest_missing is None:
                latest_missing = (result_session, round_part)
        if openf1_unavailable:
            return "OpenF1 当前不可用，且 Jolpica 暂无最近已完成的正赛、排位赛或冲刺赛结果；练习赛结果无法通过 Jolpica 恢复。"
        if latest_missing is not None:
            missing_session, missing_round = latest_missing
            label = JOLPICA_RESULT_SESSION_LABEL[missing_session]
            return f"最新已开始的正式 session（第 {missing_round} 站{label}）结果尚未发布。"
        return ""

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

    async def _latest_results_text(self, season: str = "current") -> str:
        if not self.config.plugin.enabled:
            return "F1 资讯插件未启用。"
        try:
            text = await self._latest_openf1_session_result_text(season=season)
        except Exception as exc:
            self._log_warning("OpenF1 最新结果查询失败: %s", exc)
            if self._is_openf1_unavailable_error(exc):
                return await self._latest_jolpica_result_text(season, openf1_unavailable=True)
            text = ""
        if text:
            return text
        fallback_text = await self._latest_jolpica_result_text(season, openf1_unavailable=False)
        return fallback_text or "没有查询到最近 session 结果。"

    async def _latest_openf1_result_text_for_session(self, result_session: str, season: str = "current") -> str:
        session_name = OPENF1_RESULTS_SESSION_NAME_BY_SESSION[result_session]
        return await self._latest_openf1_session_result_text(season=season, target_session_names={session_name})

    async def _openf1_result_text_for_race_session(self, race: dict[str, Any], result_session: str) -> str:
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
            return self._format_openf1_session_results(session, results, drivers)
        return ""

    async def _latest_openf1_session_result_text(
        self,
        season: str = "current",
        target_session_names: set[str] | None = None,
    ) -> str:
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
                return self._format_openf1_session_results(session, results, drivers)
        return ""

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


    def _format_openf1_session_results(
        self,
        session: dict[str, Any],
        results: list[dict[str, Any]],
        drivers: dict[str, dict[str, Any]],
    ) -> str:
        session_name = self._zh_session_name(str(session.get("session_name") or ""))
        location = str(session.get("location") or session.get("circuit_short_name") or "未知分站")
        year = str(session.get("year") or "")
        title = " ".join(part for part in [year, "F1", location, f"{session_name}结果"] if part)
        lines = [title]
        ended_at = self._parse_datetime(str(session.get("date_end") or ""))
        if ended_at:
            lines.append(f"结束时间：{self._format_beijing(ended_at)}")
        for row in sorted(results, key=self._openf1_result_position):
            driver_number = str(row.get("driver_number") or "")
            driver = drivers.get(driver_number, {})
            driver_label = str(driver.get("name_acronym") or driver.get("broadcast_name") or driver_number or "未知车手")
            team_name = str(driver.get("team_name") or "-")
            detail = self._format_openf1_result_detail(row)
            detail_suffix = f" {detail}" if detail else ""
            lines.append(f"{row.get('position') or '-'}. {driver_label} ({team_name}){detail_suffix}")
        return "\n".join(lines)

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
                for formatted in (F1InfoPlugin._format_openf1_duration(item) for item in value)
                if formatted
            )
        if isinstance(value, (int, float)):
            return F1InfoPlugin._format_seconds(float(value))
        text = str(value or "").strip()
        return text

    @staticmethod
    def _format_openf1_gap(value: Any) -> str:
        if isinstance(value, list):
            return " / ".join(
                formatted
                for formatted in (F1InfoPlugin._format_openf1_gap(item) for item in value)
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

    def _log_warning(self, message: str, *args: Any) -> None:
        logger_obj = getattr(getattr(self, "ctx", None), "logger", None)
        if logger_obj is not None:
            logger_obj.warning(message, *args)

    async def _news_text(self, limit: int = 10, force_refresh: bool = False, include_urls: bool = True) -> str:
        if not self.config.plugin.enabled:
            return "F1 资讯插件未启用。"
        limit = max(1, min(int(limit or self.config.news.daily_limit), 20))
        cache_key = f"news:{datetime.now(BEIJING_TZ).date().isoformat()}:{limit}"
        cache_row = self._get_cache_row(cache_key)
        stale_urls: set[str] = set()
        if cache_row:
            cache_expired = self._cache_expired(cache_row)
            cached_text = str(cache_row.get("value") or "")
            if not force_refresh and not cache_expired:
                if cached_text and not self._is_raw_news_fallback(cached_text):
                    return cached_text if include_urls else self._remove_news_urls(cached_text)
                cached_groups = self._cache_news_groups(cache_row)
                if cached_groups:
                    return await self._news_text_from_groups(cache_key, cached_groups, limit, include_urls)
            elif cache_expired or force_refresh:
                stale_urls = self._cache_urls(cache_row)

        items = await self._collect_news_items()
        if not items:
            return "暂时没有抓取到 F1 新闻候选。"
        if stale_urls:
            items = [item for item in items if self._normalize_news_url(item.url) not in stale_urls]
            if not items:
                return "暂时没有抓取到新的 F1 新闻。"
        groups = self._rank_news_groups(items)
        return await self._news_text_from_groups(cache_key, groups[:limit], limit, include_urls)

    async def _news_text_from_groups(
        self,
        cache_key: str,
        groups: list[dict[str, Any]],
        limit: int,
        include_urls: bool,
    ) -> str:
        selected = groups[:limit]
        summary = await self._generate_news_summary(selected, limit)
        using_raw_fallback = not summary
        if using_raw_fallback:
            summary = self._fallback_news_summary(selected, limit)
        text = f"今日 F1 重要新闻\n{summary}"
        cache_urls = self._news_group_urls(selected)
        if not using_raw_fallback:
            cache_urls.update(self._extract_news_urls(text))
        self._set_cache(
            cache_key,
            "" if using_raw_fallback else text,
            ttl_seconds=self.config.news.cache_ttl_minutes * 60,
            urls=cache_urls,
            news_groups=selected,
        )
        await self._save_cache_async()
        if using_raw_fallback:
            return text
        return text if include_urls else self._remove_news_urls(text)

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
                dt = F1InfoPlugin._parse_jolpica_datetime(raw.get("date"), raw.get("time"))
                if dt:
                    candidates.append(dt)
        race_dt = F1InfoPlugin._parse_jolpica_datetime(race.get("date"), race.get("time"))
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

    async def _collect_news_items(self) -> list[NewsItem]:
        feed_specs = self._parse_feed_specs()
        tasks = [self._fetch_feed_items(source, url, weight) for source, url, weight in feed_specs]
        chunks = await asyncio.gather(*tasks, return_exceptions=True)
        items: list[NewsItem] = []
        cutoff = time.time() - self.config.news.lookback_hours * 3600
        for (source, url, _weight), chunk in zip(feed_specs, chunks):
            if isinstance(chunk, BaseException):
                if isinstance(chunk, F1ExternalApiError):
                    self._log_external_exception("news_rss", chunk)
                else:
                    self.ctx.logger.warning("RSS 抓取失败: source=%s url=%s error=%s", source, self._redact_url_for_log(url), chunk)
                continue
            for item in chunk:
                if item.published_at and item.published_at.timestamp() < cutoff:
                    continue
                items.append(item)
        return items

    async def _fetch_feed_items(self, source: str, url: str, weight: float) -> list[NewsItem]:
        text = await self._fetch_text(url)
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError as exc:
            raise F1ExternalApiError(
                "RSS 新闻源返回内容异常，请稍后重试。",
                source="rss",
                category="invalid_response",
                redacted_url=self._redact_url_for_log(url),
            ) from exc
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
        timeout_seconds = int(self.config.model.llm_timeout_seconds)
        try:
            raw = await asyncio.wait_for(
                self.ctx.llm.generate(
                    prompt=prompt,
                    model=str(self.config.model.model_name or "utils"),
                    temperature=float(self.config.model.temperature),
                    max_tokens=max_tokens,
                    rpc_timeout_ms=timeout_seconds * 1000,
                ),
                timeout=self._news_summary_wait_seconds(),
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

    @staticmethod
    def _is_raw_news_fallback(text: str) -> bool:
        return NEWS_FALLBACK_NOTICE in text

    def _fallback_news_summary(self, groups: list[dict[str, Any]], limit: int) -> str:
        lines = [NEWS_FALLBACK_NOTICE]
        lines.extend(
            self._fallback_news_line(idx, group)
            for idx, group in enumerate(groups[:limit], 1)
        )
        return "\n".join(lines)

    def _fallback_news_line(self, idx: int, group: dict[str, Any]) -> str:
        best = self._best_item(group)
        title = self._clean_text(best.title) or "无标题"
        description = self._clean_text(best.description)
        lines = [f"{idx}. {best.source}: {title}"]
        if description and description != title:
            lines.append(f"   导语：{description[:300]}")
        lines.append(f"   URL：{best.url}")
        return "\n".join(lines)

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

    async def _fetch_json(self, url: str, deadline: float | None = None) -> Any:
        text = await self._fetch_text(url, deadline=deadline)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            error = F1ExternalApiError(
                "外部数据源返回内容异常，请稍后重试。",
                source=self._external_source_from_url(url),
                category="invalid_response",
                redacted_url=self._redact_url_for_log(url),
            )
            self._log_warning(
                "外部接口 JSON 解析失败: source=%s url=%s error=%s",
                error.source,
                error.redacted_url,
                exc,
            )
            raise error from exc

    @staticmethod
    def _validated_api_base_url(raw_url: Any, label: str) -> str:
        url = str(raw_url or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError(f"{label} API 地址必须是 http/https URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError(f"{label} API 地址不能包含用户信息、查询参数或片段")
        if any(ord(char) < 32 for char in url):
            raise ValueError(f"{label} API 地址包含非法控制字符")
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))

    @staticmethod
    def _redact_url_for_log(url: str) -> str:
        parts = urlsplit(str(url or ""))
        if not parts.scheme or not parts.netloc:
            return "<invalid-url>"
        try:
            host = parts.hostname or ""
            port = parts.port
        except ValueError:
            host = parts.netloc.rsplit("@", 1)[-1]
            port = None
        netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
        if port is not None:
            netloc = f"{netloc}:{port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "<query>", ""))

    async def _fetch_text(self, url: str, deadline: float | None = None) -> str:
        return await asyncio.to_thread(self._fetch_text_sync, url, deadline)

    def _fetch_text_sync(self, url: str, deadline: float | None = None) -> str:
        last_exc: Exception | None = None
        attempts = int(self.config.api.retry_count) + 1
        for attempt in range(attempts):
            try:
                timeout = float(self.config.api.request_timeout_seconds)
                if deadline is not None:
                    remaining_seconds = deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        raise TimeoutError("请求超时")
                    timeout = min(timeout, remaining_seconds)
                request = Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; MaiBotF1InfoPlugin/1.0)",
                        "Accept": "application/json, application/rss+xml, application/atom+xml, text/xml, */*",
                    },
                )
                with urlopen(request, timeout=timeout, context=self._ssl_context) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    return response.read(1_500_000).decode(charset, errors="replace")
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    sleep_seconds = 0.5 * (attempt + 1)
                    if deadline is not None:
                        remaining_seconds = deadline - time.monotonic()
                        if remaining_seconds <= sleep_seconds:
                            break
                    time.sleep(sleep_seconds)
        error = self._external_api_error_from_exception(url, last_exc)
        self._log_warning(
            "外部接口请求失败: source=%s category=%s status=%s url=%s attempts=%s timeout=%ss error=%s",
            error.source,
            error.category,
            error.status_code if error.status_code is not None else "-",
            error.redacted_url or "-",
            attempts,
            self.config.api.request_timeout_seconds,
            last_exc,
        )
        raise error from last_exc

    @staticmethod
    def _is_openf1_unavailable_error(exc: BaseException) -> bool:
        unavailable_statuses = (401, 403, 429, 500, 502, 503, 504)
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, OpenF1UnavailableError):
                return True
            if isinstance(current, F1ExternalApiError) and current.category in {"rate_limited", "upstream_unavailable", "timeout", "network", "invalid_response"}:
                return True
            if isinstance(current, HTTPError) and current.code in unavailable_statuses:
                return True
            if isinstance(current, (TimeoutError, URLError, OSError)):
                return True
            message = str(current).lower()
            if any(f"http error {code}" in message for code in unavailable_statuses):
                return True
            if any(marker in message for marker in ("unauthorized", "forbidden", "too many requests", "timed out", "timeout", "请求超时", "urlopen error")):
                return True
            current = current.__cause__ or current.__context__
        return False

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

    def _cache_news_groups(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        raw_groups = row.get("news_groups")
        if not isinstance(raw_groups, list):
            return []
        groups: list[dict[str, Any]] = []
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                continue
            raw_items = raw_group.get("items")
            if not isinstance(raw_items, list):
                continue
            items: list[NewsItem] = []
            for raw_item in raw_items:
                item = self._cached_news_item(raw_item)
                if item is not None:
                    items.append(item)
            if not items:
                continue
            groups.append(
                {
                    "topic": str(raw_group.get("topic") or self._topic_key(items[0])),
                    "items": items,
                    "score": self._safe_float(raw_group.get("score")),
                }
            )
        return groups

    def _cached_news_item(self, raw_item: Any) -> NewsItem | None:
        if not isinstance(raw_item, dict):
            return None
        title = self._clean_text(str(raw_item.get("title") or ""))
        url = str(raw_item.get("url") or "").strip()
        if not title or not url:
            return None
        return NewsItem(
            source=self._clean_text(str(raw_item.get("source") or "RSS")),
            title=title,
            url=url,
            description=self._clean_text(str(raw_item.get("description") or "")),
            published_at=self._parse_cached_datetime(raw_item.get("published_at")),
            weight=self._safe_float(raw_item.get("weight"), 1.0),
        )

    def _news_group_urls(self, groups: list[dict[str, Any]]) -> set[str]:
        urls: set[str] = set()
        for group in groups:
            for item in group.get("items", []):
                if not isinstance(item, NewsItem):
                    continue
                normalized = self._normalize_news_url(item.url)
                if normalized:
                    urls.add(normalized)
        return urls

    def _serialize_news_groups(self, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        serialized_groups: list[dict[str, Any]] = []
        for group in groups:
            serialized_items = []
            for item in group.get("items", []):
                if not isinstance(item, NewsItem):
                    continue
                serialized_items.append(
                    {
                        "source": item.source,
                        "title": item.title,
                        "url": item.url,
                        "description": item.description,
                        "published_at": item.published_at.isoformat() if item.published_at else None,
                        "weight": item.weight,
                    }
                )
            if serialized_items:
                serialized_groups.append(
                    {
                        "topic": str(group.get("topic") or ""),
                        "score": self._safe_float(group.get("score")),
                        "items": serialized_items,
                    }
                )
        return serialized_groups

    @staticmethod
    def _parse_cached_datetime(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _set_cache(
        self,
        key: str,
        value: Any,
        ttl_seconds: int,
        urls: set[str] | None = None,
        news_groups: list[dict[str, Any]] | None = None,
    ) -> None:
        row = {
            "value": value,
            "expires_at": time.time() + ttl_seconds,
            "urls": sorted(urls or set()),
        }
        if news_groups is not None:
            row["news_groups"] = self._serialize_news_groups(news_groups)
        self._cache[key] = row

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
