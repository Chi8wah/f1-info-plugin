"""Meme 独立调度：北京时间、持久化时段去重、采集与发送互不阻塞。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

import asyncio

from .constants import BEIJING_TZ
from .meme_config import MemeConfig


class MemeSchedulerMixin:
    # 这些成员由 F1InfoPlugin 的 MemeMixin 初始化。
    _meme_loops: list[asyncio.Task[None]]
    _meme_workers: dict[asyncio.Task[None], str]
    _meme_stop: asyncio.Event

    async def _start_meme_tasks(self, config: MemeConfig) -> None:
        self._meme_stop = asyncio.Event()
        self._meme_loops = [asyncio.create_task(self._meme_clock("fetch", config))]
        if config.targets:
            self._meme_loops.append(asyncio.create_task(self._meme_clock("send", config)))

    async def _stop_meme_tasks(self) -> None:
        self._meme_stop.set()
        for task in self._meme_loops:
            task.cancel()
        await asyncio.gather(*self._meme_loops, return_exceptions=True)
        self._meme_loops.clear()
        workers = list(self._meme_workers.items())
        for task, kind in workers:
            if kind == "fetch":
                task.cancel()
        # 已发起的发送等待有界 RPC 结束，避免配置更新直接中断回执处理。
        await asyncio.gather(*(task for task, _ in workers), return_exceptions=True)

    async def _meme_clock(self, kind: str, config: MemeConfig) -> None:
        send_jobs = [(job, job.effective_times) for job in config.send_jobs if job.enabled] if kind == "send" else []
        times = config.fetch_times if kind == "fetch" else sorted({value for _, job_times in send_jobs for value in job_times})
        while times and not self._meme_stop.is_set():
            now = datetime.now(BEIJING_TZ)
            # 定时循环只计算未来时点，配置保存时的空队列补采由业务层单独检查。
            due = min(self._next_scheduled_run(value, now) for value in times)
            if not await self._meme_wait_until(due):
                return
            # 进程挂起或系统时间跳变后，不补发旧时段。
            if (datetime.now(BEIJING_TZ) - due).total_seconds() > 60:
                self.ctx.logger.warning("F1 Meme 跳过错过的 %s 时段：%s", kind, due.isoformat())
                continue
            if kind == "fetch":
                for source in config.source_names:
                    self._launch_meme_worker(lambda source=source, due=due: self._meme_fetch(source, due, config), kind)
            else:
                for job, job_times in send_jobs:
                    if due.strftime("%H:%M") in job_times:
                        self._launch_meme_worker(lambda job=job, due=due: self._meme_send(job, due, config), kind)

    def _launch_meme_worker(self, operation: Callable[[], Awaitable[None]], kind: str) -> None:
        async def guarded() -> None:
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.exception("F1 Meme %s 后台任务失败", kind)

        task = asyncio.create_task(guarded())
        self._meme_workers[task] = kind
        task.add_done_callback(lambda done: self._meme_workers.pop(done, None))

    async def _meme_wait_until(self, due: datetime) -> bool:
        while not self._meme_stop.is_set():
            remaining = (due - datetime.now(BEIJING_TZ)).total_seconds()
            if remaining <= 0:
                return True
            try:
                await asyncio.wait_for(self._meme_stop.wait(), timeout=min(remaining, 60.0))
            except TimeoutError:
                continue
        return False
