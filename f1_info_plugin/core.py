from __future__ import annotations

import asyncio
from copy import deepcopy
import os
import ssl
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .cache import CacheMixin
from .config import F1InfoPluginConfig, ModelConfig, load_default_driver_profiles
from .constants import CACHE_PATH, LLM_GENERATE_WAIT_GRACE_SECONDS, NEWS_COMMAND_OUTER_GRACE_SECONDS, PLUGIN_ROOT
from .driver_context import DriverContextMixin, DriverContextSessionState
from .http_client import HttpClientMixin
from .news import NewsMixin
from .output import OutputMixin
from .prompt_context import is_primary_planner_request, merge_planner_system_context
from .renderers import RendererMixin
from .results import ResultsMixin
from .schedule import ScheduleMixin
from .schedule_context import ScheduleContextMixin
from .scheduler import SchedulerMixin


def _preserved_hook_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """保留 Hook 业务参数，排除只用于 RPC 路由且目标函数不接收的字段。"""

    # Host 会用 modified_kwargs 替换而非增量合并原参数；这里只返回变更字段会
    # 丢失 tool_definitions 等后续 LLM 调用参数。hook_name 则不能回传给业务函数。
    return {key: value for key, value in kwargs.items() if key != "hook_name"}


class F1InfoPlugin(
    HttpClientMixin,
    OutputMixin,
    SchedulerMixin,
    DriverContextMixin,
    ScheduleContextMixin,
    ScheduleMixin,
    ResultsMixin,
    RendererMixin,
    NewsMixin,
    CacheMixin,
    MaiBotPlugin,
):
    """查询 F1 赛历、赛果和每日新闻摘要。"""

    config_model = F1InfoPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[str, Any] = {}
        self._cache_lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._scheduler_wakeup: asyncio.Event | None = None
        self._published_schedule_keys: set[str] = set()
        self._schedule_context_task: asyncio.Task[None] | None = None
        self._schedule_context_wakeup: asyncio.Event | None = None
        self._schedule_context_refresh_lock = asyncio.Lock()
        self._schedule_context_snapshot: dict[str, Any] = {}
        self._schedule_context_last_attempt_at: float | None = None
        self._schedule_context_retry_not_before: float | None = None
        self._driver_context_session_states: dict[str, DriverContextSessionState] = {}
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

    @staticmethod
    def _write_driver_profile_reset(
        config_path: Path,
        profiles: list[dict[str, Any]],
    ) -> None:
        """原子写回一次性重置后的车手资料，保留无关配置和注释。"""

        import tomlkit

        if not config_path.is_file():
            raise FileNotFoundError(f"插件配置文件不存在：{config_path}")

        document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        driver_context = document.get("driver_context")
        if not isinstance(driver_context, dict):
            raise ValueError("config.toml 中缺少 [driver_context] 配置表")

        profile_tables = tomlkit.aot()
        for profile in profiles:
            profile_table = tomlkit.table()
            for key, value in profile.items():
                profile_table.add(key, value)
            profile_tables.append(profile_table)

        driver_context["profiles"] = profile_tables
        driver_context["reset_profiles_on_next_start"] = False
        rendered = tomlkit.dumps(document)

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=config_path.parent,
                prefix=f".{config_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(rendered)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, config_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    async def _reset_driver_profiles_on_start_if_requested(self) -> None:
        """在插件加载阶段执行用户确认的一次性默认资料恢复。"""

        if not self.config.driver_context.reset_profiles_on_next_start:
            return

        default_profiles = load_default_driver_profiles()
        profile_data = [profile.model_dump(mode="python") for profile in default_profiles]
        updated_config_data = deepcopy(self.get_plugin_config_data())
        driver_context_data = updated_config_data.get("driver_context")
        if not isinstance(driver_context_data, dict):
            self.ctx.logger.error("F1 车手资料恢复失败：当前 driver_context 配置无效")
            return
        driver_context_data["profiles"] = profile_data
        driver_context_data["reset_profiles_on_next_start"] = False

        try:
            await asyncio.to_thread(
                self._write_driver_profile_reset,
                PLUGIN_ROOT / "config.toml",
                profile_data,
            )
        except Exception:
            self.ctx.logger.exception(
                "F1 车手资料恢复失败，保留现有资料和重置标记，将在下次启动时重试"
            )
            return

        self.set_plugin_config(updated_config_data)
        self._clear_driver_context_session_states()
        self.ctx.logger.info("已恢复作者默认 F1 车手资料")

    async def on_load(self) -> None:
        await self._reset_driver_profiles_on_start_if_requested()
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._cache = await asyncio.to_thread(self._load_cache)
        await self._load_schedule_context_cache_async()
        self._start_scheduler()
        self._start_schedule_context_task()
        self.ctx.logger.info("F1 资讯插件已加载")

    async def on_unload(self) -> None:
        self._clear_driver_context_session_states()
        await self._stop_schedule_context_task()
        await self._stop_scheduler()
        await self._save_cache_async()
        self.ctx.logger.info("F1 资讯插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data, version
        self._clear_driver_context_session_states()
        await self._save_cache_async()
        await self._restart_scheduler()
        await self._reconfigure_schedule_context_task()

    @HookHandler(
        "maisaka.planner.before_request",
        name="f1_schedule_context_planner",
        description="向 Planner 注入当前 F1 比赛周或下一站赛历信息",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_planner_schedule_context_hook(
        self,
        messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not is_primary_planner_request(kwargs.get("tool_definitions")):
            return {"action": "continue"}
        context_text = self._planner_schedule_context_text()
        if not context_text or not isinstance(messages, list):
            return {"action": "continue"}
        modified_kwargs = _preserved_hook_kwargs(kwargs)
        modified_kwargs["messages"] = merge_planner_system_context(
            messages,
            context_text,
        )
        return {
            "action": "continue",
            "modified_kwargs": modified_kwargs,
        }

    @HookHandler(
        "maisaka.replyer.before_request",
        name="f1_schedule_context_replyer",
        description="向 Replyer 注入当前 F1 比赛周或下一站赛历信息",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_replyer_schedule_context_hook(
        self,
        extra_prompt: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        context_text = self._replyer_schedule_context_text()
        if not context_text:
            return {"action": "continue"}
        blocks = [str(extra_prompt or "").strip(), context_text]
        modified_kwargs = _preserved_hook_kwargs(kwargs)
        modified_kwargs["extra_prompt"] = "\n\n".join(
            block for block in blocks if block
        )
        return {
            "action": "continue",
            "modified_kwargs": modified_kwargs,
        }

    @HookHandler(
        "maisaka.planner.before_request",
        name="f1_driver_context_planner",
        description="识别近期用户消息中的 F1 车手，并向 Planner 注入用户维护的车手资料",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_planner_driver_context_hook(
        self,
        messages: list[dict[str, Any]] | None = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        # 必须在匹配和 session 状态写入前过滤辅助任务，否则 expression/behavior/
        # emoji 等子任务会污染 F1 提示，并可能覆盖 Replyer 需要的主 Planner 命中结果。
        if not is_primary_planner_request(kwargs.get("tool_definitions")):
            return {"action": "continue"}
        if not isinstance(messages, list):
            return {"action": "continue"}
        context_text, profiles = self._planner_driver_context_text(messages)
        self._remember_driver_context_profiles(session_id, profiles)
        if not context_text:
            return {"action": "continue"}
        modified_kwargs = _preserved_hook_kwargs(kwargs)
        modified_kwargs["session_id"] = session_id
        modified_kwargs["messages"] = merge_planner_system_context(
            messages,
            context_text,
        )
        return {
            "action": "continue",
            "modified_kwargs": modified_kwargs,
        }

    @HookHandler(
        "maisaka.replyer.before_request",
        name="f1_driver_context_replyer",
        description="向 Replyer 注入本轮 Planner 命中的 F1 车手群聊上下文",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_replyer_driver_context_hook(
        self,
        extra_prompt: str = "",
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        context_text = self._replyer_driver_context_text(session_id)
        if not context_text:
            return {"action": "continue"}
        blocks = [str(extra_prompt or "").strip(), context_text]
        modified_kwargs = _preserved_hook_kwargs(kwargs)
        modified_kwargs["session_id"] = session_id
        modified_kwargs["extra_prompt"] = "\n\n".join(
            block for block in blocks if block
        )
        return {
            "action": "continue",
            "modified_kwargs": modified_kwargs,
        }

    @Tool(
        "f1_schedule",
        description="查询 F1 下一站或相对分站赛历，返回练习、冲刺、排位、正赛等 session 的北京时间安排",
        parameters=[
            ToolParameterInfo(name="round", param_type=ToolParamType.STRING, description="留空、0 或 next 表示下一站；-1 上一站，-2 上两站，负数不限；也兼容官方轮次", required=False),
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
            page = await self._schedule_page_data(round_value=round_value, season="current")
        except Exception as exc:
            return await self._send_command_error(stream_id, "schedule", exc)
        await self._send_page_output(stream_id, "F1 赛历", page, self._render_schedule_text, self._render_schedule_html)
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
            page = await self._results_page_data(session=session, round_value=round_value, season="current")
        except Exception as exc:
            return await self._send_command_error(stream_id, "results", exc)
        await self._send_page_output(stream_id, "F1 赛果", page, self._render_results_text, self._render_results_html)
        return True, "已发送 F1 赛果", True

    @Command(
        "f1_latest_results_command",
        description="查询最近一个 F1 session 结果",
        pattern=r"^(?:(?:/(?:f1_latest_results|f1_latest_result|f1最新结果|f1最新赛果|f1最近结果|f1最近赛果))|(?:/f1\s+(?:latest|latest_result|latest_results|最新结果|最新赛果|最近结果|最近赛果)))$",
    )
    async def handle_latest_results_command(self, stream_id: str = "", **kwargs: Any):
        del kwargs
        try:
            page = await self._latest_results_page_data(season="current")
        except Exception as exc:
            return await self._send_command_error(stream_id, "latest_results", exc)
        await self._send_page_output(stream_id, "F1 最新结果", page, self._render_results_text, self._render_results_html)
        return True, "已发送 F1 最新结果", True

    @Command("f1_news_command", description="查询每日 F1 新闻摘要", pattern=r"^(?:(?:/(?:f1_news|f1新闻)(?:\s+(?P<limit_legacy>\d{1,2}))?)|(?:/f1\s+(?:news|新闻|资讯)(?:\s+(?P<limit_f1>\d{1,2}))?))$")
    async def handle_news_command(self, stream_id: str = "", matched_groups: dict[str, str] | None = None, **kwargs: Any):
        del kwargs
        groups = matched_groups or {}
        raw_limit = groups.get("limit_legacy") or groups.get("limit_f1") or ""
        limit = int(raw_limit) if raw_limit.isdigit() else self.config.news.daily_limit
        include_urls = bool(self.config.news.include_urls_in_command)
        try:
            page = await self._news_page_data(limit=limit, force_refresh=False)
        except Exception as exc:
            return await self._send_command_error(stream_id, "news", exc)
        if isinstance(page, str) and not include_urls:
            page = self._remove_news_urls(page)
        await self._send_page_output(
            stream_id,
            "F1 新闻摘要",
            page,
            lambda news_page: self._render_news_text(news_page, include_urls=include_urls),
            self._render_news_html,
        )
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
            "/f1 赛历 [下一站|0|-1|8]：0/下一站查询下一站，负数查询相对分站，正数查询官方轮次赛历\n"
            "/f1 赛果 [正赛|排位|冲刺] [0|-1|8]：查询最近已完成赛果，或指定相对分站/官方轮次\n"
            "/f1_latest_results 或 /f1 最新结果：查询最近一个已结束 session 的结果（含练习/排位/冲刺/正赛）\n"
            "/f1_news [条数] 或 /f1 新闻 [条数]：查询每日重要 F1 新闻\n"
            "/f1_clear_cache 或 /f1 清缓存：清除插件缓存，下次查询新闻会重新抓取"
        )
        await self.ctx.send.text(text, stream_id)
        return True, "已发送 F1 插件帮助", True

def create_plugin() -> F1InfoPlugin:
    """创建 F1 资讯插件实例。"""

    return F1InfoPlugin()
