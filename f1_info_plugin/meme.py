"""F1 Meme 采集、发送和生命周期编排。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import asyncio
import base64
import hashlib
import time

from .constants import BEIJING_TZ
from .meme_client import FETCH_BUDGET_SECONDS, FxEmbedClient, MemeClientError
from .meme_config import MemeConfig, MemeSendJobConfig
from .meme_scheduler import MemeSchedulerMixin
from .meme_store import MemeStorageFull, MemeStore


_STOP_LABELS = {
    "limit": "达到 X", "history": "遇到历史帖子", "end": "来源已结束",
    "timeout": "总耗时已到", "pagination": "分页未推进", "storage": "存储已满",
    "error": "采集失败", "cancelled": "任务已停止", "expired": "已到有效期边界",
}
_SEND_LABELS = {"sent": "成功", "failed": "失败", "uncertain": "结果不明", "empty": "队列为空", "skipped": "已跳过", "running": "发送中"}


class MemeMixin(MemeSchedulerMixin):
    def _init_meme_state(self) -> None:
        self._meme_store: MemeStore | None = None
        self._meme_client: FxEmbedClient | None = None
        self._meme_loops: list[asyncio.Task[None]] = []
        self._meme_workers: dict[asyncio.Task[None], str] = {}
        self._meme_stop = asyncio.Event()
        self._meme_fetch_locks: dict[str, asyncio.Lock] = {}
        self._meme_send_locks: dict[str, asyncio.Lock] = {}
        self._meme_send_slots = asyncio.Semaphore(4)
        self._meme_config_lock = asyncio.Lock()
        self._meme_active_config: MemeConfig | None = None

    def _meme_enabled(self) -> bool:
        return bool(self.config.plugin.enabled and self.config.meme.enabled)

    async def _meme_db(self, method: str, *args: Any) -> Any:
        store = self._meme_store
        if store is None:
            raise RuntimeError("Meme 队列尚未初始化")
        operation = asyncio.create_task(asyncio.to_thread(getattr(store, method), *args))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # SQLite / 原子文件提交结束后再响应取消，防止停机后继续写入。
            await operation
            raise

    async def _reconfigure_meme(self, *, catch_up_empty: bool = False) -> None:
        async with self._meme_config_lock:
            config = self.config.meme.model_copy(deep=True)
            reuse_tasks = (
                self._meme_enabled() and self._meme_active_config == config
                and not self._meme_stop.is_set() and bool(self._meme_loops)
                and all(not task.done() for task in self._meme_loops)
            )
            # 相同配置重复保存时保留正在进行的采集，不因保存取消后重新请求上游。
            if not reuse_tasks:
                await self._stop_meme_tasks()
                self._meme_active_config = None
                if not self._meme_enabled():
                    return
            if self._meme_store is None:
                store = MemeStore(Path(self.ctx.paths.data_dir) / "meme")
                await asyncio.to_thread(store.initialize)
                self._meme_store = store
                self._meme_client = FxEmbedClient()
            await self._meme_maintain(config)
            if not reuse_tasks:
                await self._start_meme_tasks(config)
                self._meme_active_config = config
            if catch_up_empty:
                await self._meme_catch_up_empty_queues(config)

    async def _meme_catch_up_empty_queues(self, config: MemeConfig) -> None:
        if not config.targets:
            return
        now = datetime.now(BEIJING_TZ)
        hour, minute = map(int, max(config.fetch_times).split(":"))
        last_fetch = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now <= last_fetch:
            return
        if not await self._meme_db("has_empty_queue", config.targets, config.source_names,
                                   time.time() - config.max_age_days * 86400):
            return
        for source in config.source_names:
            lock = self._meme_fetch_locks.get(source)
            if lock is not None and lock.locked():
                continue
            # 补采复用常规采集的 X、去重和下载预算；用保存时刻区分当天的定时任务。
            self._launch_meme_worker(lambda source=source, due=now: self._meme_fetch(source, due, config), "fetch")
            self.ctx.logger.info("F1 Meme 配置保存后补采：@%s 有群队列为空，已过当天最后一次采集时间 %s", source, last_fetch.strftime("%H:%M"))

    async def _meme_maintain(self, config: MemeConfig) -> None:
        cutoff = time.time() - config.max_age_days * 86400
        await self._meme_db("cleanup", cutoff)
        await self._meme_db("sync_targets", config.targets, config.source_names, cutoff, config.max_pending_per_group)

    async def _meme_collect_items(self, items: list[dict[str, Any]], remaining: int, run_key: str,
                                  config: MemeConfig, deadline: float, attempted: set[int]) -> int:
        added = 0
        client = self._meme_client
        if client is None:
            raise RuntimeError("Meme 客户端尚未初始化")
        # 顺序下载按成功入库数累计 X，失败和哈希重复不占额度，多图帖也不超额。
        for item in items:
            if added >= remaining or self._meme_stop.is_set():
                break
            if item["id"] in attempted:
                continue
            attempted.add(item["id"])
            try:
                raw, extension, mime = await client.image(item["url"], config.max_image_mb * 1024 * 1024, deadline)
            except TimeoutError:
                raise
            except (MemeClientError, OSError, ValueError) as exc:
                await self._meme_db("download_failed", item["id"], str(exc))
                self.ctx.logger.warning("F1 Meme 图片下载失败：source=%s post=%s error=%s", item["source"], item["post_id"], exc)
                continue
            if self._meme_stop.is_set() or not self._meme_enabled():
                break
            await self._meme_db("ensure_space", len(raw), config.max_storage_mb * 1024 * 1024, hashlib.sha256(raw).hexdigest())
            accepted = await self._meme_db("accept_image", item["id"], raw, extension, mime, run_key,
                                            config.targets, config.source_names, time.time() - config.max_age_days * 86400,
                                            config.max_pending_per_group, config.max_storage_mb * 1024 * 1024)
            added += int(accepted)
        return added

    async def _meme_fetch(self, source: str, due: datetime, config: MemeConfig) -> None:
        lock = self._meme_fetch_locks.setdefault(source, asyncio.Lock())
        if lock.locked() or self._meme_stop.is_set() or not self._meme_enabled():
            return
        async with lock:
            key = f"fetch:{source}:{due.isoformat()}"
            if not await self._meme_db("start_run", key, "fetch", source, due.timestamp()):
                return
            self.ctx.logger.info("F1 Meme 采集开始：@%s 计划时间=%s 本轮上限=%s 张", source, due.strftime("%Y-%m-%d %H:%M:%S"), config.fetch_image_limit)
            added = pages = 0
            reason, error = "end", ""
            cutoff = time.time() - config.max_age_days * 86400
            deadline = time.monotonic() + FETCH_BUDGET_SECONDS
            attempted: set[int] = set()
            try:
                await self._meme_maintain(config)
                # 固定本轮开始前的历史集合，本轮响应内或页间重复不算历史边界。
                known = await self._meme_db("known_posts", source)
                # 待重试图片与新图共享本轮 X；分页游标只在本轮使用，下轮从首页开始。
                retries = await self._meme_db("pending_items", source, cutoff)
                added += await self._meme_collect_items(retries, config.fetch_image_limit, key, config, deadline, attempted)
                cursor: str | None = None
                cursors: set[str] = set()
                page_ids: set[str] = set()
                client = self._meme_client
                if client is None:
                    raise RuntimeError("Meme 客户端尚未初始化")
                while added < config.fetch_image_limit and not self._meme_stop.is_set():
                    page = await client.page(source, cursor, deadline)
                    pages += 1
                    ids = {post.post_id for post in page.posts}
                    if not ids:
                        reason = "end"
                        break
                    if ids <= page_ids:
                        reason = "pagination"
                        error = "上游返回了重复页"
                        break
                    page_ids.update(ids)
                    # 历史帖子触发停止翻页，但仍在剩余额度内处理完当前页其他新图。
                    # 图片哈希重复只影响入库与计数，不触发历史边界。
                    found_history = bool(ids & known)
                    for post in page.posts:
                        if added >= config.fetch_image_limit:
                            break
                        await self._meme_db("record_post", source, post, cutoff)
                        items = await self._meme_db("pending_items", source, cutoff, post.post_id)
                        added += await self._meme_collect_items(items, config.fetch_image_limit - added, key, config, deadline, attempted)
                    if added >= config.fetch_image_limit:
                        reason = "limit"
                        break
                    if found_history:
                        reason = "history"
                        break
                    if all(post.published_at < cutoff for post in page.posts):
                        reason = "expired"
                        break
                    if not page.cursor:
                        reason = "end"
                        break
                    if page.cursor == cursor or page.cursor in cursors:
                        reason, error = "pagination", "上游分页游标未推进"
                        break
                    cursors.add(page.cursor)
                    cursor = page.cursor
                if added >= config.fetch_image_limit:
                    reason = "limit"
                elif self._meme_stop.is_set():
                    reason = "cancelled"
            except asyncio.CancelledError:
                added = await self._meme_db("finish_fetch", key, source, pages, "cancelled", "配置更新或插件卸载中止采集")
                self.ctx.logger.info("F1 Meme 采集取消：@%s 新增=%s/%s 页数=%s", source, added, config.fetch_image_limit, pages)
                raise
            except TimeoutError:
                reason, error = "timeout", "本轮采集超过总耗时限制"
            except MemeStorageFull as exc:
                reason, error = "storage", str(exc)
            except Exception as exc:
                reason, error = "error", str(exc)
                self.ctx.logger.exception("F1 Meme 账号采集失败：%s", source)
            added = await self._meme_db("finish_fetch", key, source, pages, reason, error)
            log = self.ctx.logger.warning if error else self.ctx.logger.info
            log("F1 Meme 采集结束：@%s 新增=%s/%s 页数=%s 原因=%s 错误=%s", source, added, config.fetch_image_limit, pages, _STOP_LABELS.get(reason, reason), error or "无")

    async def _meme_resolve_stream(self, job: MemeSendJobConfig) -> str:
        result = await asyncio.wait_for(self.ctx.chat.get_stream_by_group_id(job.group_id, platform=job.platform), timeout=10)
        if not isinstance(result, dict):
            return ""
        stream = result.get("stream", result)
        if not isinstance(stream, dict) or result.get("success") is False:
            return ""
        if "group_id" in stream and str(stream["group_id"]) != job.group_id:
            return ""
        if "platform" in stream and stream["platform"] != job.platform:
            return ""
        return str(stream.get("session_id") or stream.get("stream_id") or "")

    @staticmethod
    def _meme_send_result(result: Any) -> tuple[bool, str | None]:
        if isinstance(result, bool):
            return result, None
        if isinstance(result, dict):
            sent = result.get("sent", result.get("success"))
            if not isinstance(sent, bool):
                raise RuntimeError("发图接口没有返回明确的成功状态")
            message_id = result.get("message_id")
            return sent, str(message_id) if message_id else None
        raise RuntimeError("发图接口返回了无法确认的结果")

    async def _meme_send(self, job: MemeSendJobConfig, due: datetime, config: MemeConfig) -> None:
        # 每群每个 B 时段只领取一张；明确失败留给后续 B，不在本时段改发另一张。
        lock = self._meme_send_locks.setdefault(job.target_key, asyncio.Lock())
        async with lock, self._meme_send_slots:
            if self._meme_stop.is_set() or not self._meme_enabled():
                return
            key = f"send:{job.target_key}:{due.isoformat()}"
            if not await self._meme_db("start_run", key, "send", job.target_key, due.timestamp()):
                return
            self.ctx.logger.info("F1 Meme 发送检查：target=%s 计划时间=%s", job.target_key, due.strftime("%Y-%m-%d %H:%M:%S"))
            if (datetime.now(BEIJING_TZ) - due).total_seconds() > 60:
                await self._meme_db("finish_run", key, "skipped", "时段已错过")
                self.ctx.logger.warning("F1 Meme 发送跳过：target=%s 原因=时段已错过", job.target_key)
                return
            try:
                stream_id = await self._meme_resolve_stream(job)
                if not stream_id:
                    raise RuntimeError("未找到目标群的现有会话")
                await self._meme_maintain(config)
                if self._meme_stop.is_set() or not self._meme_enabled():
                    await self._meme_db("finish_run", key, "skipped", "发送前模块已停止")
                    self.ctx.logger.info("F1 Meme 发送跳过：target=%s 原因=发送前模块已停止", job.target_key)
                    return
                delivery = await self._meme_db("claim", job.target_key, key, config.source_names, time.time() - config.max_age_days * 86400)
                if delivery is None:
                    await self._meme_db("finish_run", key, "empty", "没有有效待发送图片")
                    self.ctx.logger.info("F1 Meme 发送跳过：target=%s 原因=队列为空，没有有效待发送图片", job.target_key)
                    return
                try:
                    raw = await self._meme_db("read_image", delivery)
                except (OSError, RuntimeError) as exc:
                    await self._meme_db("mark_missing", delivery["digest"])
                    await self._meme_db("finish_delivery", job.target_key, delivery["digest"], key, "failed", None, str(exc))
                    self.ctx.logger.warning("F1 Meme 图片读取失败：target=%s image=%s error=%s", job.target_key, delivery["digest"][:12], exc)
                    return
            except Exception as exc:
                await self._meme_db("finish_run", key, "failed", str(exc))
                self.ctx.logger.warning("F1 Meme 发送准备失败：target=%s error=%s", job.target_key, exc)
                return
            if self._meme_stop.is_set() or not self._meme_enabled():
                await self._meme_db("release_unstarted", job.target_key, delivery["digest"], key)
                self.ctx.logger.info("F1 Meme 发送跳过：target=%s 原因=发起发送前模块已停止，图片已放回队列", job.target_key)
                return
            self.ctx.logger.info("F1 Meme 发送开始：target=%s author=@%s image=%s", job.target_key, delivery["author_handle"], delivery["digest"][:12])
            try:
                # 图片与采集时保存的作者署名共用一次发送和一份回执，避免署名与图片分离。
                segments = [
                    {"type": "image", "content": base64.b64encode(raw).decode("ascii")},
                    {"type": "text", "content": f"\n——by @{delivery['author_handle']}"},
                ]
                result = await asyncio.wait_for(self.ctx.send.hybrid(segments, stream_id,
                                                                     return_details=True, timeout_ms=30000), timeout=32)
                sent, message_id = self._meme_send_result(result)
            except BaseException as exc:
                # 调用已经可能到达平台，任何无法获得明确回执的异常都不能自动重发。
                await asyncio.shield(self._meme_db("finish_delivery", job.target_key, delivery["digest"], key, "uncertain", None, f"发送结果不明：{type(exc).__name__}: {exc}"))
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                self.ctx.logger.warning("F1 Meme 发送结果不明：target=%s error=%s", job.target_key, exc)
                return
            await self._meme_db("finish_delivery", job.target_key, delivery["digest"], key, "sent" if sent else "failed", message_id, "" if sent else "SDK 明确返回发送失败")
            if sent:
                self.ctx.logger.info("F1 Meme 发送成功：target=%s author=@%s message_id=%s", job.target_key, delivery["author_handle"], message_id or "未返回")
            else:
                self.ctx.logger.warning("F1 Meme 发送失败：target=%s SDK 明确返回发送失败，本次尝试=%s/3", job.target_key, delivery["attempts"] + 1)

    async def _meme_status_text(self, stream_id: str) -> str:
        if not self._meme_enabled():
            return "F1 Meme 未启用。请在插件配置中启用并填写采集时间和目标群。"
        if self._meme_store is None:
            return "F1 Meme 队列尚未初始化，请查看插件日志。"
        config = self.config.meme.model_copy(deep=True)
        jobs = [job for job in config.send_jobs if job.enabled and job.group_id]
        streams = await asyncio.gather(*(self._meme_resolve_stream(job) for job in jobs), return_exceptions=True)
        job = next((job for job, resolved in zip(jobs, streams) if resolved == stream_id), None)
        if job is None:
            return "当前会话不是已配置的 Meme 发送群。"
        data = await self._meme_db("stats", job.target_key, config.source_names, time.time() - config.max_age_days * 86400)
        counts = data["counts"]
        now = datetime.now(BEIJING_TZ)
        next_fetch = min(self._next_scheduled_run(value, now) for value in config.fetch_times)
        send_times = job.effective_times
        next_send = min(self._next_scheduled_run(value, now) for value in send_times)
        lines = ["F1 Meme 状态", f"待发送：{counts.get('pending', 0)} 张；结果不明：{counts.get('uncertain', 0)}；失败：{counts.get('failed', 0)}",
                 f"每账号每轮 X：{config.fetch_image_limit} 张", f"下次采集：{next_fetch:%m-%d %H:%M}；本群下次发送：{next_send:%m-%d %H:%M}（北京时间）",
                 f"本群发送规则：{job.schedule_description}；每天 {len(send_times)} 次，每次一张",
                 f"图片存储：{data['storage'] / 1024 / 1024:.2f}/{config.max_storage_mb} MiB"]
        statuses = {entry["source"]: entry for entry in data["sources"]}
        for source in config.source_names:
            entry = statuses.get(source)
            if entry is None:
                lines.append(f"@{source}：尚未采集")
            else:
                stamp = datetime.fromtimestamp(entry["last_run_at"], BEIJING_TZ)
                lines.append(f"@{source}：{stamp:%m-%d %H:%M}，新增 {entry['added']} 张，{_STOP_LABELS.get(entry['reason'], entry['reason'])}" + (f"；{entry['error']}" if entry["error"] else ""))
        last = data["last_send"]
        if last:
            lines.append(f"本群最近发送：{_SEND_LABELS.get(last['state'], last['state'])}" + (f"；{last['detail']}" if last["detail"] else ""))
        return "\n".join(lines)
