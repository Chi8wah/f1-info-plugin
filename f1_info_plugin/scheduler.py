from __future__ import annotations
# pyright: reportAttributeAccessIssue=false, reportUninitializedInstanceVariable=false

import asyncio
import importlib
import re
from datetime import datetime, timedelta
from typing import Any

from .constants import BEIJING_TZ


class SchedulerMixin:

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
                page = await self._news_page_data(limit=limit, force_refresh=False)
            except Exception as exc:
                await self._send_scheduled_news_error(batch, date_key, exc)
                continue
            if isinstance(page, str) and not include_urls:
                page = self._remove_news_urls(page)
            output_mode = self._output_mode()
            image_base64: str | None = None
            render_on_missing = True
            render_text = lambda news_page: self._render_news_text(news_page, include_urls=include_urls)
            if output_mode in {"image", "both"} and not isinstance(page, str):
                try:
                    image_base64 = await self._render_html_image(self._render_news_html(page))
                except Exception as exc:
                    render_on_missing = False
                    self._log_warning("渲染定时 F1 新闻图片失败，降级为文本: %s", exc)
            for job in batch:
                for stream_id in job["stream_ids"]:
                    publish_key = f"{date_key}:{job['time']}:{stream_id}"
                    try:
                        await self._send_page_output(
                            stream_id,
                            "F1 定时新闻",
                            page,
                            render_text,
                            self._render_news_html,
                            mode=output_mode,
                            image_base64=image_base64,
                            render_on_missing=render_on_missing,
                        )
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
