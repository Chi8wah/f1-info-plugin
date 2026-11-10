"""Meme 的数量边界、队列持久化和发送不确定性回归测试。"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError

import asyncio
import base64
import hashlib
import io
import json
import sqlite3
import tempfile
import time
import unittest

from maibot_sdk.config import generate_plugin_config_schema

from f1_info_plugin.config import F1InfoPluginConfig
from f1_info_plugin.constants import BEIJING_TZ
from f1_info_plugin.core import F1InfoPlugin
from f1_info_plugin.meme import MemeMixin
from f1_info_plugin.meme_client import FxEmbedClient, MemeClientError, MemePage, MemePhoto, MemePost, validate_image_url
from f1_info_plugin.meme_config import MemeConfig, MemeSendJobConfig
from f1_info_plugin.meme_store import MemeStorageFull, MemeStore
from f1_info_plugin.scheduler import SchedulerMixin


def jpeg(seed: str) -> bytes:
    # 本模块校验字节格式与长度，不要求 Pillow 解码依赖。
    return b"\xff\xd8\xff" + seed.encode() + b"\xff\xd9"


def post(number: int, photos: int = 1, *, source: str = "f1trollofficial") -> MemePost:
    return MemePost(str(number), "author", source, time.time() - 1000 + number,
                    f"https://x.com/{source}/status/{number}", "meme",
                    tuple(MemePhoto(f"{number}-{index}", index, f"https://pbs.twimg.com/media/{number}-{index}.jpg") for index in range(photos)))


def page(numbers: list[int], cursor: str | None = None) -> MemePage:
    return MemePage(tuple(post(number) for number in numbers), cursor)


class FakeClient:
    def __init__(self, pages: list[MemePage]) -> None:
        self.page = AsyncMock(side_effect=pages)
        self.image = AsyncMock(side_effect=self._image)

    @staticmethod
    async def _image(url: str, max_bytes: int, deadline: float) -> tuple[bytes, str, str]:
        del max_bytes, deadline
        return jpeg(url), "jpg", "image/jpeg"


class Harness(MemeMixin, SchedulerMixin):
    def __init__(self, root: Path, *, limit: int = 30, groups: list[str] | None = None) -> None:
        self._init_meme_state()
        self.config = SimpleNamespace(plugin=SimpleNamespace(enabled=True), meme=MemeConfig(
            enabled=True, fetch_image_limit=limit,
            send_jobs=[MemeSendJobConfig(group_id=value) for value in (groups or ["100"])],
        ))
        self.ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=root), logger=Mock(),
                                   chat=SimpleNamespace(get_stream_by_group_id=AsyncMock(side_effect=lambda group_id, **kw: {"session_id": f"stream-{group_id}"})),
                                   send=SimpleNamespace(hybrid=AsyncMock(return_value={"sent": True, "message_id": "message-1"})))
        self._meme_store = MemeStore(root / "meme")
        self._meme_store.initialize()
        self._meme_client = FakeClient([])

    @property
    def meme(self) -> MemeConfig:
        return self.config.meme

    @property
    def store(self) -> MemeStore:
        assert self._meme_store is not None
        return self._meme_store


class MemeConfigTest(unittest.TestCase):
    def test_defaults_and_normalization(self) -> None:
        self.assertEqual(MemeConfig().fetch_image_limit, 30)
        self.assertEqual(MemeConfig().accounts, ["@f1trollofficial"])
        self.assertFalse(MemeConfig().enabled)
        self.assertEqual(MemeConfig().send_jobs, [])
        config = MemeConfig(accounts=[" @F1TrollOfficial ", "f1trollofficial"], fetch_times=["9:00", "09:00", "01:05"])
        self.assertEqual(config.source_names, ["f1trollofficial"])
        self.assertEqual(config.fetch_times, ["01:05", "09:00"])
        for invalid in ("25:00", "12:60", "12:1", "tomorrow"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                MemeConfig(fetch_times=[invalid])
        with self.assertRaises(ValueError):
            MemeConfig(fetch_image_limit=0)
        with self.assertRaises(ValueError):
            MemeConfig(accounts=["https://x.com/f1trollofficial"])
        with self.assertRaises(ValueError):
            MemeConfig(enabled=True, accounts=[])
        with self.assertRaises(ValueError):
            MemeConfig(enabled=True, send_jobs=[MemeSendJobConfig()])
        with self.assertRaises(ValueError):
            MemeConfig(send_jobs=[MemeSendJobConfig(group_id="1"), MemeSendJobConfig(group_id="1")])

    def test_legacy_jobs_keep_their_manual_times(self) -> None:
        old_times = [f"{hour:02d}:00" for hour in range(11, 23)]
        config = F1InfoPluginConfig.model_validate({
            "plugin": {"enabled": True, "config_version": "1.3.0"},
            "meme": {"enabled": True, "send_jobs": [
                {"enabled": True, "platform": "qq", "group_id": "100", "times": old_times},
            ]},
        })
        job = config.meme.send_jobs[0]
        self.assertEqual(config.plugin.config_version, "1.3.0")
        self.assertEqual(config.meme.send_order, "oldest_first")
        self.assertEqual(job.schedule_mode, "manual")
        self.assertEqual(job.effective_times, old_times)
        self.assertEqual(job.model_dump()["times"], old_times)

    def test_interval_range_includes_only_aligned_endpoints(self) -> None:
        job = MemeSendJobConfig(schedule_mode="interval", start_time=" 11:00 ", end_time="22:00")
        self.assertEqual(job.start_time, "11:00")
        self.assertEqual(job.effective_times, [f"{hour:02d}:00" for hour in range(11, 23)])
        job = MemeSendJobConfig(schedule_mode="interval", interval_hours=2)
        self.assertEqual(job.effective_times, ["11:00", "13:00", "15:00", "17:00", "19:00", "21:00"])
        job = MemeSendJobConfig(schedule_mode="interval", start_time="11:30", end_time="22:00")
        self.assertEqual(job.effective_times, [f"{hour:02d}:30" for hour in range(11, 22)])

    def test_interval_range_supports_midnight_and_daily_reset(self) -> None:
        job = MemeSendJobConfig(schedule_mode="interval", start_time="22:00", end_time="2:00")
        self.assertEqual(job.end_time, "02:00")
        self.assertEqual(job.effective_times, ["00:00", "01:00", "02:00", "22:00", "23:00"])
        job = MemeSendJobConfig(schedule_mode="interval", start_time="22:30", end_time="02:15", interval_hours=2)
        self.assertEqual(job.effective_times, ["00:30", "22:30"])
        job = MemeSendJobConfig(schedule_mode="interval", start_time="00:00", end_time="23:59")
        self.assertEqual(job.effective_times, [f"{hour:02d}:00" for hour in range(24)])
        job = MemeSendJobConfig(schedule_mode="interval", interval_hours=24)
        self.assertEqual(job.effective_times, ["11:00"])

    def test_switching_modes_preserves_both_sets_of_values(self) -> None:
        job = MemeSendJobConfig(times=["18:30", "9:00"], start_time="22:00", end_time="02:00", interval_hours=2)
        self.assertEqual(job.effective_times, ["09:00", "18:30"])
        data = job.model_dump()
        data["schedule_mode"] = "interval"
        interval = MemeSendJobConfig.model_validate(data)
        self.assertEqual(interval.effective_times, ["00:00", "02:00", "22:00"])
        data = interval.model_dump()
        data["schedule_mode"] = "manual"
        manual = MemeSendJobConfig.model_validate(data)
        self.assertEqual(manual.effective_times, ["09:00", "18:30"])
        self.assertEqual((manual.start_time, manual.end_time, manual.interval_hours), ("22:00", "02:00", 2))

    def test_enabled_interval_job_does_not_require_manual_times(self) -> None:
        job = MemeSendJobConfig(group_id="100", schedule_mode="interval", times=[])
        MemeConfig(enabled=True, send_jobs=[job])
        with self.assertRaisesRegex(ValueError, "发送时间"):
            MemeConfig(enabled=True, send_jobs=[MemeSendJobConfig(group_id="100", times=[])])
        # 手动模式不受另一种模式的区间关系约束。
        MemeSendJobConfig(start_time="11:00", end_time="11:00")

    def test_interval_range_validation(self) -> None:
        for invalid in (
            {"schedule_mode": "other"}, {"start_time": "25:00"}, {"end_time": "22:60"},
            {"start_time": "11:00", "end_time": "11:00"},
            {"interval_hours": 0}, {"interval_hours": 25}, {"interval_hours": 1.5}, {"interval_hours": True},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                MemeSendJobConfig.model_validate({"schedule_mode": "interval", **invalid})

    def test_webui_interval_hours_accepts_integer_strings_in_both_modes(self) -> None:
        expected_times = {
            1: [f"{hour:02d}:00" for hour in range(11, 23)],
            2: ["11:00", "13:00", "15:00", "17:00", "19:00", "21:00"],
            24: ["11:00"],
        }
        for mode in ("manual", "interval"):
            for value, expected in (("1", 1), (" 2 ", 2), ("24", 24)):
                with self.subTest(mode=mode, value=value):
                    config = F1InfoPluginConfig.model_validate_json(json.dumps({
                        "meme": {"enabled": True, "send_jobs": [{
                            "group_id": "100", "schedule_mode": mode,
                            "times": ["12:00"], "interval_hours": value,
                        }]},
                    }))
                    job = config.meme.send_jobs[0]
                    self.assertEqual(job.interval_hours, expected)
                    self.assertIs(type(job.model_dump()["interval_hours"]), int)
                    self.assertEqual(job.effective_times, ["12:00"] if mode == "manual" else expected_times[expected])

    def test_interval_hours_rejects_invalid_webui_values_in_both_modes(self) -> None:
        for mode in ("manual", "interval"):
            for value in ("", " ", "1.5", "1.0", "1e0", "abc", "0", "25", "-1", 1.0, 1.5, True, False, None):
                with self.subTest(mode=mode, value=value), self.assertRaises(ValueError):
                    F1InfoPluginConfig.model_validate_json(json.dumps({
                        "meme": {"enabled": True, "send_jobs": [{
                            "group_id": "100", "schedule_mode": mode, "interval_hours": value,
                        }]},
                    }))

    def test_schema_exposes_send_order_and_schedule_fields(self) -> None:
        schema = generate_plugin_config_schema(F1InfoPluginConfig)
        meme_fields = schema["sections"]["meme"]["fields"]
        order = meme_fields["send_order"]
        self.assertEqual(order["ui_type"], "select")
        self.assertEqual(order["choices"], ["oldest_first", "newest_first"])
        self.assertEqual(order["default"], "oldest_first")
        self.assertIn("旧图可能还没发就过期", order["hint"])
        self.assertIn("可能连续发送几天前的旧图", order["hint"])
        fields = meme_fields["send_jobs"]["item_fields"]
        self.assertEqual(fields["schedule_mode"]["choices"], ["manual", "interval"])
        self.assertEqual(fields["schedule_mode"]["default"], "manual")
        self.assertEqual(fields["interval_hours"]["default"], 1)
        self.assertTrue({"times", "start_time", "end_time", "interval_hours"} <= fields.keys())


class MemeLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_own_config_save_requests_empty_queue_catchup(self) -> None:
        plugin = F1InfoPlugin()
        plugin._save_cache_async = AsyncMock()
        plugin._restart_scheduler = AsyncMock()
        plugin._reconfigure_schedule_context_task = AsyncMock()
        plugin._reconfigure_meme = AsyncMock()
        await plugin.on_config_update("self", {}, "1.3.0")
        plugin._reconfigure_meme.assert_awaited_once_with(catch_up_empty=True)
        plugin._reconfigure_meme.reset_mock()
        await plugin.on_config_update("global", {}, "1.3.0")
        plugin._reconfigure_meme.assert_not_awaited()


class MemeRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.directory.name))
        self.due = datetime.now(BEIJING_TZ)

    async def asyncTearDown(self) -> None:
        await self.h._stop_meme_tasks()
        self.directory.cleanup()

    async def collect(self, pages: list[MemePage], *, limit: int | None = None, offset: int = 0) -> FakeClient:
        if limit is not None:
            self.h.meme.fetch_image_limit = limit
        client = FakeClient(pages)
        self.h._meme_client = client
        await self.h._meme_fetch("f1trollofficial", self.due + timedelta(seconds=offset), self.h.meme)
        return client

    async def save_meme_at(self, when: datetime) -> None:
        with patch("f1_info_plugin.meme.datetime") as clock:
            clock.now.return_value = when
            await self.h._reconfigure_meme(catch_up_empty=True)

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.h.store.database)) as db:
            db.row_factory = sqlite3.Row
            return db.execute(sql, params).fetchall()

    async def test_fewer_results_keep_paging_until_thirty_actual_images(self) -> None:
        client = await self.collect([page(list(range(1, 10)), "p2"), page(list(range(10, 20)), "p3"), page(list(range(20, 35)), "p4")])
        self.assertEqual(client.page.await_count, 3)
        self.assertEqual(client.image.await_count, 30)
        self.assertEqual(len(self.rows("SELECT * FROM assets")), 30)
        self.assertEqual(self.rows("SELECT reason,added FROM sources")[0][:], ("limit", 30))

    async def test_multiphoto_post_stops_exactly_at_remaining_limit(self) -> None:
        client = await self.collect([MemePage((post(1, 4),), "next")], limit=2)
        self.assertEqual(client.image.await_count, 2)
        self.assertEqual(len(self.rows("SELECT * FROM assets")), 2)
        self.assertEqual(len(self.rows("SELECT * FROM items WHERE state='pending'")), 2)
        # 未下载的同帖图片在下轮与新增图片共享额度，不会启动越过额度的后台下载。
        client = await self.collect([], limit=1, offset=1)
        self.assertEqual(client.page.await_count, 0)
        self.assertEqual(client.image.await_count, 1)

    async def test_history_in_middle_keeps_other_new_posts_on_same_page(self) -> None:
        await self.collect([page([10])], limit=1)
        client = await self.collect([page([11, 10, 12], "unneeded")], limit=30, offset=1)
        self.assertEqual(client.page.await_count, 1)
        self.assertEqual(client.image.await_count, 2)
        self.assertEqual(self.rows("SELECT reason,added FROM sources")[0][:], ("history", 2))

    async def test_same_response_duplicate_is_not_historical_boundary(self) -> None:
        client = await self.collect([page([1, 1, 2], "p2"), page([3])], limit=3)
        self.assertEqual(client.page.await_count, 2)
        self.assertEqual(client.image.await_count, 3)

    async def test_hash_duplicate_does_not_end_paging_or_consume_quota(self) -> None:
        first = await self.collect([page([1])], limit=1)
        original = (await first.image("https://pbs.twimg.com/media/1-0.jpg", 1000, 0))[0]
        client = FakeClient([page([2], "p2"), page([3])])
        async def image(url: str, max_bytes: int, deadline: float) -> tuple[bytes, str, str]:
            return (original if "/2-" in url else jpeg(url)), "jpg", "image/jpeg"
        client.image.side_effect = image
        self.h._meme_client = client
        self.h.meme.fetch_image_limit = 1
        await self.h._meme_fetch("f1trollofficial", self.due + timedelta(seconds=1), self.h.meme)
        self.assertEqual(client.page.await_count, 2)
        self.assertEqual(self.rows("SELECT added FROM sources")[0][0], 1)
        self.assertEqual(len(self.rows("SELECT * FROM assets")), 2)

    async def test_download_failures_do_not_count_and_retry_next_fetch(self) -> None:
        self.h.meme.fetch_image_limit = 2
        client = FakeClient([page([1, 2], "p2"), page([3])])
        client.image.side_effect = [MemeClientError("broken"), (jpeg("2"), "jpg", "image/jpeg"), (jpeg("3"), "jpg", "image/jpeg")]
        self.h._meme_client = client
        await self.h._meme_fetch("f1trollofficial", self.due, self.h.meme)
        self.assertEqual(client.page.await_count, 2)
        self.assertEqual(self.rows("SELECT added FROM sources")[0][0], 2)
        client = await self.collect([], limit=1, offset=1)
        self.assertEqual(client.image.await_count, 1)
        self.assertEqual(client.page.await_count, 0)

    async def test_repeated_page_and_cursor_terminate_without_loop(self) -> None:
        client = await self.collect([page([1], "same"), page([2], "same")])
        self.assertEqual(client.page.await_count, 2)
        self.assertEqual(self.rows("SELECT reason FROM sources")[0][0], "pagination")
        client = await self.collect([page([3], "x"), page([3], "y")], offset=1)
        self.assertEqual(client.page.await_count, 2)
        self.assertIn("重复页", self.rows("SELECT error FROM sources")[0][0])

    async def test_same_fetch_slot_is_persistent_and_does_not_refetch(self) -> None:
        await self.collect([page([1])], limit=1)
        client = await self.collect([page([2])], limit=1)
        client.page.assert_not_awaited()

    async def test_expired_page_ends_and_is_not_downloaded(self) -> None:
        old = post(1)
        old = replace(old, published_at=time.time() - 8 * 86400)
        client = await self.collect([MemePage((old,), "older")])
        client.page.assert_awaited_once()
        client.image.assert_not_awaited()
        self.assertEqual(self.rows("SELECT * FROM posts"), [])
        self.assertEqual(self.rows("SELECT * FROM items"), [])
        self.assertEqual(self.rows("SELECT reason FROM sources")[0][0], "expired")

    async def test_groups_share_files_and_have_independent_send_progress(self) -> None:
        self.h.meme.send_jobs = [MemeSendJobConfig(group_id="100"), MemeSendJobConfig(group_id="200")]
        await self.collect([page([1])], limit=1)
        self.assertEqual(len(self.rows("SELECT * FROM assets")), 1)
        self.assertEqual(len(self.rows("SELECT * FROM deliveries")), 2)
        a, b = self.h.meme.send_jobs
        await self.h._meme_send(a, self.due, self.h.meme)
        self.assertEqual(self.rows("SELECT state FROM deliveries WHERE target=?", (a.target_key,))[0][0], "sent")
        self.assertEqual(self.rows("SELECT state FROM deliveries WHERE target=?", (b.target_key,))[0][0], "pending")
        await self.h._meme_send(a, self.due, self.h.meme)
        self.assertEqual(self.h.ctx.send.hybrid.await_count, 1)
        await self.h._meme_send(b, self.due, self.h.meme)
        self.assertEqual(self.h.ctx.send.hybrid.await_count, 2)
        self.assertEqual(self.rows("SELECT message_id FROM deliveries WHERE target=?", (b.target_key,))[0][0], "message-1")

    async def test_send_order_change_uses_post_time_and_keeps_photo_order(self) -> None:
        now = time.time()
        oldest = replace(post(1), published_at=now - 7200)
        middle = replace(post(2), published_at=now - 3600)
        newest = replace(post(3, 2), published_at=now - 600, text="较新的原帖")
        await self.collect([MemePage((newest,), None)])
        await self.collect([MemePage((middle, oldest), None)], offset=1)
        job = self.h.meme.send_jobs[0]
        await self.h._meme_send(job, self.due, self.h.meme)

        self.h.meme.send_order = "newest_first"
        for offset in range(1, 5):
            await self.h._meme_send(job, self.due + timedelta(seconds=offset), self.h.meme)
        sent_segments = [call.args[0] for call in self.h.ctx.send.hybrid.await_args_list]
        sent = [base64.b64decode(segments[1]["content"]) for segments in sent_segments]
        expected = [*oldest.photos, *newest.photos, *middle.photos]
        self.assertEqual(sent, [jpeg(photo.url) for photo in expected])
        self.assertEqual(
            [segments[0]["content"] for segments in sent_segments[1:3]],
            ["较新的原帖 ——by @f1trollofficial\n"] * 2,
        )

    async def test_send_includes_saved_caption_and_author_with_image_in_one_message(self) -> None:
        saved_post = replace(post(1), author_handle="F1TrollOfficial", text="  aabbcc 🏎️\n第二行  ")
        await self.collect([MemePage((saved_post,), None)], limit=1)
        # 重启后从数据库读取采集时的正文和作者。
        self.h._meme_store = MemeStore(self.h.store.root)
        self.h.store.initialize()
        await self.h._meme_send(self.h.meme.send_jobs[0], self.due, self.h.meme)
        self.h.ctx.send.hybrid.assert_awaited_once_with(
            [
                {"type": "text", "content": "aabbcc 🏎️\n第二行 ——by @F1TrollOfficial\n"},
                {"type": "image", "content": base64.b64encode(jpeg(saved_post.photos[0].url)).decode("ascii")},
            ],
            "stream-100",
            return_details=True,
            timeout_ms=30000,
            sync_to_maisaka_history=True,
            maisaka_source_kind="guided_reply",
        )
        self.assertEqual(tuple(self.rows("SELECT author_id,author_handle FROM posts")[0]), ("author", "F1TrollOfficial"))
        self.assertEqual(self.rows("SELECT state FROM deliveries")[0][0], "sent")

    async def test_old_queue_migrates_author_without_losing_pending_image(self) -> None:
        old_post = replace(post(1), text=" \n ")
        await self.collect([MemePage((old_post,), None)], limit=1)
        with closing(sqlite3.connect(self.h.store.database)) as db, db:
            db.execute("ALTER TABLE posts DROP COLUMN author_handle")
            db.execute("DROP TABLE sent_history")
            db.execute("PRAGMA user_version=1")
        self.h.store.initialize()
        self.h.store.initialize()
        self.assertEqual(self.rows("PRAGMA user_version")[0][0], 3)
        self.assertEqual(self.rows("SELECT author_handle FROM posts")[0][0], "f1trollofficial")
        self.assertEqual(len(list(self.h.store.images.iterdir())), 1)
        await self.h._meme_send(self.h.meme.send_jobs[0], self.due, self.h.meme)
        self.assertEqual(self.h.ctx.send.hybrid.await_args.args[0][0], {"type": "text", "content": "——by @f1trollofficial\n"})
        self.assertEqual(self.rows("SELECT state FROM deliveries")[0][0], "sent")

    async def test_same_image_uses_caption_and_author_from_currently_enabled_source(self) -> None:
        await self.collect([page([1])], limit=1)
        other_post = replace(post(2, source="another"), author_handle="Another", text="另一来源的正文")
        self.h.store.record_post("another", other_post)
        raw = jpeg(post(1).photos[0].url)
        self.assertFalse(self.h.store.accept_image(("another", other_post.post_id, other_post.photos[0].media_id),
                                                   raw, "jpg", "image/jpeg", "unused",
                                                   self.h.meme.targets, ["another"], 0, 200, 1024 * 1024))
        self.h.meme.accounts = ["@another"]
        await self.h._meme_send(self.h.meme.send_jobs[0], self.due, self.h.meme)
        self.assertEqual(self.h.ctx.send.hybrid.await_args.args[0][0], {"type": "text", "content": "另一来源的正文 ——by @Another\n"})

    async def test_false_send_is_terminal_and_next_slot_uses_another_image(self) -> None:
        await self.collect([page([1, 2])], limit=2)
        self.h.ctx.send.hybrid.side_effect = [{"sent": False, "message_id": None}, True]
        job = self.h.meme.send_jobs[0]
        await self.h._meme_send(job, self.due, self.h.meme)
        self.h.ctx.send.hybrid.assert_awaited_once()
        states = self.rows("SELECT state,attempts FROM deliveries ORDER BY published_at")
        self.assertEqual([tuple(row) for row in states], [("failed", 1), ("pending", 0)])
        digest = hashlib.sha256(jpeg(post(1).photos[0].url)).digest()
        self.assertEqual(self.rows("SELECT digest FROM sent_history")[0][0], digest)

        for offset in (1, 2):
            await self.h._meme_send(job, self.due + timedelta(seconds=offset), self.h.meme)
        self.assertEqual(self.h.ctx.send.hybrid.await_count, 2)
        states = self.rows("SELECT state,attempts FROM deliveries ORDER BY published_at")
        self.assertEqual([tuple(row) for row in states], [("failed", 1), ("sent", 1)])

    async def test_timeout_is_uncertain_and_never_automatically_retried(self) -> None:
        await self.collect([page([1])], limit=1)
        self.h.ctx.send.hybrid.side_effect = TimeoutError("lost receipt")
        job = self.h.meme.send_jobs[0]
        await self.h._meme_send(job, self.due, self.h.meme)
        await self.h._meme_send(job, self.due + timedelta(seconds=1), self.h.meme)
        self.assertEqual(self.h.ctx.send.hybrid.await_count, 1)
        self.assertEqual(self.rows("SELECT state FROM deliveries")[0][0], "uncertain")

    async def test_unknown_response_is_not_truthy_success(self) -> None:
        await self.collect([page([1])], limit=1)
        self.h.ctx.send.hybrid.return_value = {"error": "bad result"}
        await self.h._meme_send(self.h.meme.send_jobs[0], self.due, self.h.meme)
        self.assertEqual(self.rows("SELECT state FROM deliveries")[0][0], "uncertain")

    async def test_missing_session_and_missed_time_preserve_queue(self) -> None:
        await self.collect([page([1])], limit=1)
        self.h.ctx.chat.get_stream_by_group_id.return_value = None
        self.h.ctx.chat.get_stream_by_group_id.side_effect = None
        job = self.h.meme.send_jobs[0]
        await self.h._meme_send(job, self.due, self.h.meme)
        await self.h._meme_send(job, self.due - timedelta(minutes=2), self.h.meme)
        self.h.ctx.send.hybrid.assert_not_awaited()
        self.assertEqual(self.rows("SELECT state FROM deliveries")[0][0], "pending")

    async def test_removed_source_is_not_sent_and_new_target_uses_inventory(self) -> None:
        await self.collect([page([1])], limit=1)
        new_job = MemeSendJobConfig(group_id="300")
        self.h.meme.send_jobs.append(new_job)
        await self.h._meme_maintain(self.h.meme)
        self.assertEqual(len(self.rows("SELECT * FROM deliveries")), 2)
        self.h.meme.accounts = ["@another"]
        await self.h._meme_send(new_job, self.due, self.h.meme)
        self.h.ctx.send.hybrid.assert_not_awaited()

    async def test_corrupted_file_is_not_sent_and_can_be_recovered_by_fetch(self) -> None:
        await self.collect([page([1])], limit=1)
        path = next(self.h.store.images.iterdir())
        path.write_bytes(b"corrupted")
        await self.h._meme_send(self.h.meme.send_jobs[0], self.due, self.h.meme)
        self.h.ctx.send.hybrid.assert_not_awaited()
        self.assertEqual(self.rows("SELECT state FROM assets")[0][0], "missing")
        self.assertEqual(tuple(self.rows("SELECT state,attempts FROM deliveries")[0]), ("pending", 0))
        self.assertEqual(self.rows("SELECT * FROM sent_history"), [])
        client = await self.collect([], limit=1, offset=1)
        self.assertEqual(client.image.await_count, 1)
        self.assertEqual(self.rows("SELECT state FROM assets")[0][0], "available")
        await self.h._meme_send(self.h.meme.send_jobs[0], self.due + timedelta(seconds=1), self.h.meme)
        self.h.ctx.send.hybrid.assert_awaited_once()

    async def test_shutdown_waits_for_started_send_receipt(self) -> None:
        await self.collect([page([1])], limit=1)
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return True
        self.h.ctx.send.hybrid.side_effect = send
        self.h._launch_meme_worker(lambda: self.h._meme_send(self.h.meme.send_jobs[0], self.due, self.h.meme), "send")
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(len(self.rows("SELECT * FROM sent_history")), 1)
        shutdown = asyncio.create_task(self.h._stop_meme_tasks())
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        release.set()
        await asyncio.wait_for(shutdown, 2)
        self.assertEqual(self.rows("SELECT state FROM deliveries")[0][0], "sent")

    async def test_reconfigure_starts_only_one_set_of_clocks_and_does_not_fetch(self) -> None:
        client = self.h._meme_client
        await self.h._reconfigure_meme()
        old_tasks = list(self.h._meme_loops)
        await self.h._reconfigure_meme()
        self.assertEqual(len(self.h._meme_loops), 2)
        self.assertEqual(self.h._meme_loops, old_tasks)
        self.h.meme.fetch_times = ["10:00"]
        await self.h._reconfigure_meme()
        self.assertTrue(all(task.done() for task in old_tasks))
        client.page.assert_not_awaited()
        self.h.config.plugin.enabled = False
        await self.h._reconfigure_meme()
        self.assertEqual(self.h._meme_loops, [])
        self.assertTrue(self.h.store.database.exists())

    async def test_config_save_after_last_fetch_catches_up_empty_queue(self) -> None:
        self.h.meme.fetch_times = ["10:00"]
        self.h.meme.fetch_image_limit = 1
        now = datetime(2026, 11, 3, 10, 30, tzinfo=BEIJING_TZ)
        last_due = now.replace(minute=0)
        key = f"fetch:f1trollofficial:{last_due.isoformat()}"
        self.h.store.start_run(key, "fetch", "f1trollofficial", last_due.timestamp())
        self.h.store.finish_fetch(key, "f1trollofficial", 0, "error", "upstream 404")
        client = FakeClient([page([1])])
        self.h._meme_client = client
        await self.save_meme_at(now)
        await asyncio.gather(*list(self.h._meme_workers))
        self.assertEqual(client.page.await_count, 1)
        self.assertEqual(self.rows("SELECT state FROM deliveries")[0][0], "pending")
        self.assertEqual(len(self.rows("SELECT * FROM runs WHERE kind='fetch'")), 2)
        self.h.ctx.send.hybrid.assert_not_awaited()

    async def test_config_save_does_not_catch_up_before_last_time_or_without_active_group(self) -> None:
        now = datetime(2026, 11, 3, 10, 30, tzinfo=BEIJING_TZ)
        client = self.h._meme_client
        for times in (["11:00"], ["10:00", "18:00"], ["10:30"]):
            with self.subTest(times=times):
                self.h.meme.fetch_times = times
                await self.save_meme_at(now)
                self.assertEqual(self.h._meme_workers, {})
        self.h.meme.fetch_times = ["10:00"]
        self.h.meme.send_jobs[0].enabled = False
        await self.save_meme_at(now)
        self.h.meme.send_jobs = []
        await self.save_meme_at(now)
        self.h.config.plugin.enabled = False
        await self.save_meme_at(now)
        self.h.config.plugin.enabled = True
        self.h.meme.enabled = False
        await self.save_meme_at(now)
        client.page.assert_not_awaited()

    async def test_config_save_uses_existing_inventory_for_new_group_without_fetch(self) -> None:
        await self.collect([page([1])], limit=1)
        self.h.meme.send_jobs.append(MemeSendJobConfig(group_id="200"))
        self.h.meme.fetch_times = ["10:00"]
        client = FakeClient([])
        self.h._meme_client = client
        await self.save_meme_at(datetime(2026, 11, 3, 10, 30, tzinfo=BEIJING_TZ))
        client.page.assert_not_awaited()
        self.assertEqual(len(self.rows("SELECT * FROM deliveries WHERE state='pending'")), 2)

    async def test_config_save_catches_up_if_only_one_group_is_empty(self) -> None:
        self.h.meme.send_jobs.append(MemeSendJobConfig(group_id="200"))
        await self.collect([page([1])], limit=1)
        await self.h._meme_send(self.h.meme.send_jobs[0], self.due, self.h.meme)
        self.h.ctx.send.hybrid.reset_mock()
        self.h.meme.fetch_times = ["10:00"]
        client = FakeClient([page([2])])
        self.h._meme_client = client
        await self.save_meme_at(datetime(2026, 11, 3, 10, 30, tzinfo=BEIJING_TZ))
        await asyncio.gather(*list(self.h._meme_workers))
        self.assertEqual(client.page.await_count, 1)
        counts = self.rows("SELECT target,COUNT(*) FROM deliveries WHERE state='pending' GROUP BY target ORDER BY target")
        self.assertEqual([tuple(row) for row in counts], [("2:qq:100", 1), ("2:qq:200", 2)])
        self.h.ctx.send.hybrid.assert_not_awaited()

    async def test_queue_check_ignores_expired_removed_and_missing_assets_but_counts_sending(self) -> None:
        await self.collect([page([1])], limit=1)
        targets, sources = self.h.meme.targets, self.h.meme.source_names
        self.assertFalse(self.h.store.has_empty_queue(targets, sources, 0))
        self.assertTrue(self.h.store.has_empty_queue(targets, sources, time.time() + 86400))
        self.assertTrue(self.h.store.has_empty_queue(targets, ["another"], 0))
        self.h.store.start_run("inflight", "send", targets[0], self.due.timestamp())
        delivery = self.h.store.claim(targets[0], "inflight", sources, 0)
        self.assertFalse(self.h.store.has_empty_queue(targets, sources, 0))
        self.assertTrue(self.h.store.begin_delivery("inflight"))
        self.assertFalse(self.h.store.has_empty_queue(targets, sources, 0))
        self.h.store.mark_missing(delivery["digest"])
        self.assertTrue(self.h.store.has_empty_queue(targets, sources, 0))

    async def test_repeated_config_save_preserves_background_fetch_and_returns_without_network_wait(self) -> None:
        self.h.meme.fetch_times = ["10:00"]
        self.h.meme.fetch_image_limit = 1
        client = FakeClient([])
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_page(*args):
            entered.set()
            await release.wait()
            return page([1])
        client.page.side_effect = slow_page
        self.h._meme_client = client
        now = datetime(2026, 11, 3, 10, 30, tzinfo=BEIJING_TZ)
        await asyncio.wait_for(self.save_meme_at(now), 2)
        await asyncio.wait_for(entered.wait(), 2)
        old_tasks = list(self.h._meme_loops)
        workers = list(self.h._meme_workers)
        await asyncio.wait_for(self.save_meme_at(now + timedelta(seconds=1)), 2)
        self.assertEqual(self.h._meme_loops, old_tasks)
        self.assertEqual(list(self.h._meme_workers), workers)
        self.assertEqual(client.page.await_count, 1)
        release.set()
        await asyncio.gather(*workers)
        self.assertEqual(len(self.rows("SELECT * FROM assets")), 1)

    async def test_config_save_can_retry_completed_fetch_failure(self) -> None:
        self.h.meme.fetch_times = ["10:00"]
        self.h.meme.fetch_image_limit = 1
        self.h._meme_client = FakeClient([MemeClientError("upstream 404")])
        now = datetime(2026, 11, 3, 10, 30, tzinfo=BEIJING_TZ)
        await self.save_meme_at(now)
        await asyncio.gather(*list(self.h._meme_workers))
        self.assertEqual(self.rows("SELECT state FROM runs")[0][0], "failed")
        client = FakeClient([page([1])])
        self.h._meme_client = client
        await self.save_meme_at(now + timedelta(seconds=1))
        await asyncio.gather(*list(self.h._meme_workers))
        self.assertEqual(client.page.await_count, 1)
        self.assertEqual(len(self.rows("SELECT * FROM assets")), 1)

    async def test_clock_dispatch_keeps_original_due_time_after_next_tick(self) -> None:
        due = datetime(2026, 11, 3, 8, 0, tzinfo=BEIJING_TZ)
        self.h._meme_fetch = AsyncMock()
        self.h._meme_wait_until = AsyncMock(side_effect=[True, False])
        with patch("f1_info_plugin.meme_scheduler.datetime") as clock:
            clock.now.side_effect = [due - timedelta(seconds=1), due, due]
            await self.h._meme_clock("fetch", self.h.meme)
        workers = list(self.h._meme_workers)
        await asyncio.gather(*workers)
        self.h._meme_fetch.assert_awaited_once_with("f1trollofficial", due, self.h.meme)

    async def test_send_clock_only_dispatches_matching_groups(self) -> None:
        due = datetime(2026, 11, 3, 12, 0, tzinfo=BEIJING_TZ)
        self.h.meme.send_jobs.append(MemeSendJobConfig(group_id="200", times=["18:00"]))
        self.h._meme_send = AsyncMock()
        self.h._meme_wait_until = AsyncMock(side_effect=[True, False])
        with patch("f1_info_plugin.meme_scheduler.datetime") as clock:
            clock.now.side_effect = [due - timedelta(seconds=1), due, due]
            await self.h._meme_clock("send", self.h.meme)
        await asyncio.gather(*list(self.h._meme_workers))
        self.h._meme_send.assert_awaited_once_with(self.h.meme.send_jobs[0], due, self.h.meme)

    async def test_send_clock_mixes_modes_and_ignores_inactive_time_lists(self) -> None:
        interval = MemeSendJobConfig(group_id="100", schedule_mode="interval", times=["12:00"], interval_hours=2)
        manual = MemeSendJobConfig(group_id="200", times=["12:00"])
        disabled = MemeSendJobConfig(group_id="300", enabled=False, schedule_mode="interval")
        self.h.meme.send_jobs = [interval, manual, disabled]
        for hour, expected in ((12, manual), (13, interval)):
            with self.subTest(hour=hour):
                due = datetime(2026, 11, 3, hour, 0, tzinfo=BEIJING_TZ)
                self.h._meme_send = AsyncMock()
                self.h._meme_wait_until = AsyncMock(side_effect=[True, False])
                with patch("f1_info_plugin.meme_scheduler.datetime") as clock:
                    clock.now.side_effect = [due - timedelta(seconds=1), due, due]
                    await self.h._meme_clock("send", self.h.meme)
                await asyncio.gather(*list(self.h._meme_workers))
                self.h._meme_send.assert_awaited_once_with(expected, due, self.h.meme)

    async def test_overnight_send_clock_advances_to_next_day(self) -> None:
        job = MemeSendJobConfig(group_id="100", schedule_mode="interval", start_time="22:00", end_time="02:00", interval_hours=2)
        self.h.meme.send_jobs = [job]
        due = datetime(2026, 11, 4, 0, 0, tzinfo=BEIJING_TZ)
        self.h._meme_send = AsyncMock()
        self.h._meme_wait_until = AsyncMock(side_effect=[True, False])
        with patch("f1_info_plugin.meme_scheduler.datetime") as clock:
            clock.now.side_effect = [due - timedelta(minutes=30), due, due]
            await self.h._meme_clock("send", self.h.meme)
        await asyncio.gather(*list(self.h._meme_workers))
        self.h._meme_send.assert_awaited_once_with(job, due, self.h.meme)

    async def test_interval_reconfigure_only_waits_for_future_slot(self) -> None:
        self.h.meme.send_jobs = [MemeSendJobConfig(group_id="100", schedule_mode="interval")]
        now = datetime(2026, 11, 3, 15, 20, tzinfo=BEIJING_TZ)
        self.h._meme_send = AsyncMock()
        self.h._meme_wait_until = AsyncMock(return_value=False)
        with patch("f1_info_plugin.meme_scheduler.datetime") as clock:
            clock.now.return_value = now
            await self.h._reconfigure_meme()
            await asyncio.gather(*self.h._meme_loops)
        due_times = [call.args[0] for call in self.h._meme_wait_until.await_args_list]
        self.assertIn(now.replace(hour=16, minute=0), due_times)
        self.assertTrue(all(due > now for due in due_times))
        self.h._meme_send.assert_not_awaited()

    async def test_disable_before_rpc_releases_the_image_and_any_send_reservation(self) -> None:
        await self.collect([page([1])], limit=1)
        for offset, method in enumerate(("read_image", "begin_delivery")):
            with self.subTest(method=method):
                original = getattr(self.h.store, method)

                def disable(*args):
                    result = original(*args)
                    self.h.config.plugin.enabled = False
                    return result

                self.h.config.plugin.enabled = True
                with patch.object(self.h.store, method, side_effect=disable):
                    await self.h._meme_send(self.h.meme.send_jobs[0], self.due + timedelta(seconds=offset), self.h.meme)
                self.h.ctx.send.hybrid.assert_not_awaited()
                self.assertEqual(tuple(self.rows("SELECT state,attempts FROM deliveries")[0]), ("pending", 0))
                self.assertEqual(self.rows("SELECT * FROM sent_history"), [])

    async def test_partial_fetch_failure_preserves_real_added_count(self) -> None:
        client = FakeClient([page([1], "p2"), MemeClientError("upstream down")])
        self.h._meme_client = client
        await self.h._meme_fetch("f1trollofficial", self.due, self.h.meme)
        row = self.rows("SELECT added,reason,error FROM sources")[0]
        self.assertEqual(tuple(row), (1, "error", "upstream down"))
        self.assertEqual(len(self.rows("SELECT * FROM deliveries WHERE state='pending'")), 1)

    async def test_status_is_scoped_to_configured_group(self) -> None:
        await self.collect([page([1])], limit=1)
        text = await self.h._meme_status_text("stream-100")
        self.assertIn("待发送：1", text)
        self.assertIn("每账号每轮 X：1", text)
        self.assertIn("手动时间点：12:00；每天 1 次", text)
        self.assertIn("不是已配置", await self.h._meme_status_text("some-other-stream"))

    async def test_interval_status_uses_effective_schedule_and_next_day(self) -> None:
        cases = (
            ("11:00", "22:00", 15, 20, "11-03 16:00", "时间段：11:00～22:00，每 1 小时一次；每天 12 次"),
            ("22:00", "02:00", 23, 30, "11-04 00:00", "时间段：22:00～次日02:00，每 1 小时一次；每天 5 次"),
        )
        for start, end, hour, minute, expected_due, expected_rule in cases:
            with self.subTest(start=start, end=end):
                self.h.meme.send_jobs = [MemeSendJobConfig(group_id="100", schedule_mode="interval", start_time=start, end_time=end, times=["03:00"])]
                with patch("f1_info_plugin.meme.datetime") as clock:
                    clock.now.return_value = datetime(2026, 11, 3, hour, minute, tzinfo=BEIJING_TZ)
                    text = await self.h._meme_status_text("stream-100")
                self.assertIn("本群下次发送：" + expected_due, text)
                self.assertIn(expected_rule, text)

    async def test_storage_cannot_delete_file_needed_by_other_group(self) -> None:
        self.h.meme.send_jobs.append(MemeSendJobConfig(group_id="200"))
        await self.collect([page([1])], limit=1)
        await self.h._meme_send(self.h.meme.send_jobs[0], self.due, self.h.meme)
        size = self.rows("SELECT size FROM assets")[0][0]
        with self.assertRaises(MemeStorageFull):
            self.h.store.ensure_space(1, size)
        self.assertEqual(len(list(self.h.store.images.iterdir())), 1)
        await self.h._meme_send(self.h.meme.send_jobs[1], self.due, self.h.meme)
        self.h.store.ensure_space(1, size)
        self.assertEqual(len(list(self.h.store.images.iterdir())), 0)
        self.assertEqual(len(self.rows("SELECT * FROM deliveries WHERE state='sent'")), 2)

    async def test_duplicate_needs_no_space_and_does_not_evict_existing_image(self) -> None:
        await self.collect([page([1])], limit=1)
        row = self.rows("SELECT digest,size FROM assets")[0]
        self.h.store.ensure_space(row["size"], row["size"], row["digest"])
        self.assertEqual(len(list(self.h.store.images.iterdir())), 1)

    async def test_expiry_never_deletes_inflight_send_file(self) -> None:
        await self.collect([page([1])], limit=1)
        job = self.h.meme.send_jobs[0]
        self.h.store.start_run("inflight", "send", job.target_key, self.due.timestamp())
        self.h.store.claim(job.target_key, "inflight", self.h.meme.source_names, 0)
        self.h.store.cleanup(time.time() + 86400)
        self.assertEqual(len(list(self.h.store.images.iterdir())), 1)
        self.assertTrue(self.h.store.begin_delivery("inflight"))
        self.h.store.cleanup(time.time() + 86400)
        self.assertEqual(len(list(self.h.store.images.iterdir())), 1)
        self.h.store.finish_delivery("inflight", "sent")
        self.h.store.cleanup(time.time() + 86400)
        self.assertEqual(len(list(self.h.store.images.iterdir())), 0)

    async def test_cleanup_preserves_migrated_send_history_across_reposts(self) -> None:
        self.h.meme.send_jobs = [
            MemeSendJobConfig(group_id=group_id) for group_id in ("100", "200", "300")
        ]
        sent, uncertain, waiting = self.h.meme.send_jobs
        first = post(1)
        await self.collect([MemePage((first,), None)], limit=1)
        await self.h._meme_send(sent, self.due, self.h.meme)
        self.h.ctx.send.hybrid.side_effect = TimeoutError("lost receipt")
        await self.h._meme_send(uncertain, self.due, self.h.meme)
        self.h.ctx.send.hybrid.side_effect = None
        digest = hashlib.sha256(jpeg(first.photos[0].url)).digest()
        with closing(sqlite3.connect(self.h.store.database)) as db, db:
            db.execute("DROP TABLE sent_history")
            db.execute("PRAGMA user_version=2")
            db.execute("UPDATE posts SET published_at=?", (time.time() - 8 * 86400,))
        self.h._meme_store = MemeStore(self.h.store.root)
        self.h.store.initialize()
        await self.h._meme_maintain(self.h.meme)
        for table in ("posts", "items", "deliveries", "assets"):
            self.assertEqual(self.rows(f"SELECT * FROM {table}"), [])
        self.assertEqual(list(self.h.store.images.iterdir()), [])
        self.assertEqual([tuple(row) for row in self.rows("SELECT target,digest FROM sent_history ORDER BY target")],
                         [(sent.target_key, digest), (uncertain.target_key, digest)])

        newer = post(2)
        repost = replace(newer, photos=(replace(newer.photos[0], url=first.photos[0].url),))
        await self.collect([MemePage((repost,), None)], offset=1)
        self.assertEqual([tuple(row) for row in self.rows("SELECT target,state FROM deliveries")],
                         [(waiting.target_key, "pending")])
        self.h.ctx.send.hybrid.reset_mock()
        for job in self.h.meme.send_jobs:
            await self.h._meme_send(job, self.due + timedelta(seconds=1), self.h.meme)
        self.h.ctx.send.hybrid.assert_awaited_once()
        self.assertEqual(self.h.ctx.send.hybrid.await_args.args[1], "stream-300")
        self.assertEqual(self.rows("SELECT digest FROM sent_history WHERE target=?", (waiting.target_key,))[0][0],
                         digest)

    async def test_migrated_failures_block_reposts_only_with_evidence_of_a_send_attempt(self) -> None:
        self.h.meme.send_jobs = [
            MemeSendJobConfig(group_id=group_id) for group_id in ("100", "200", "300")
        ]
        from_delivery, from_run, preparation_only = self.h.meme.send_jobs
        first = post(1)
        await self.collect([MemePage((first,), None)])
        digest = self.rows("SELECT digest FROM assets")[0][0]
        self.h.store.start_run("old-failure", "send", from_run.target_key, self.due.timestamp())
        with closing(sqlite3.connect(self.h.store.database)) as db, db:
            db.execute("UPDATE deliveries SET attempts=1,error='SDK 明确返回发送失败' WHERE target=?",
                       (from_delivery.target_key,))
            db.execute("UPDATE deliveries SET attempts=2,error='本地 Meme 图片校验失败' WHERE target=?",
                       (from_run.target_key,))
            db.execute("UPDATE deliveries SET state='failed',attempts=3,error='本地 Meme 图片校验失败' WHERE target=?",
                       (preparation_only.target_key,))
            db.execute("""UPDATE runs SET state='failed',digest=?,detail='SDK 明确返回发送失败',finished_at=?
                WHERE key='old-failure'""", (digest, time.time()))
        self.h.store.initialize()
        self.h.store.initialize()
        rows = self.rows("SELECT target,digest FROM sent_history ORDER BY target")
        self.assertEqual([tuple(row) for row in rows], [
            (from_delivery.target_key, bytes.fromhex(digest)), (from_run.target_key, bytes.fromhex(digest)),
        ])
        states = self.rows("SELECT state,attempts FROM deliveries ORDER BY target")
        self.assertEqual([tuple(row) for row in states], [("failed", 1), ("failed", 2), ("pending", 0)])

        self.h.store.cleanup(time.time())
        self.assertEqual(self.rows("SELECT * FROM deliveries"), [])
        newer = post(2)
        repost = replace(newer, photos=(replace(newer.photos[0], url=first.photos[0].url),))
        await self.collect([MemePage((repost,), None)], offset=1)
        for job in self.h.meme.send_jobs:
            await self.h._meme_send(job, self.due + timedelta(seconds=1), self.h.meme)
        self.h.ctx.send.hybrid.assert_awaited_once()
        self.assertEqual(self.h.ctx.send.hybrid.await_args.args[1], "stream-300")

    async def test_cleanup_keeps_shared_image_and_prunes_only_old_finished_runs(self) -> None:
        first, newer = post(1), post(2)
        newer = replace(newer, photos=(replace(newer.photos[0], url=first.photos[0].url),))
        await self.collect([MemePage((first,), None)], limit=1)
        await self.collect([MemePage((newer,), None)], offset=1)
        old = time.time() - 8 * 86400
        with closing(sqlite3.connect(self.h.store.database)) as db, db:
            db.execute("UPDATE posts SET published_at=? WHERE post_id='1'", (old,))
            db.execute("UPDATE deliveries SET published_at=?,state='skipped',error='图片过期'", (old,))
            db.execute("UPDATE runs SET finished_at=? WHERE scheduled_at=?", (old, self.due.timestamp()))
            db.execute("DROP TABLE sent_history")
            db.execute("PRAGMA user_version=2")
        self.h.store.initialize()
        self.h.store.start_run("still-running", "fetch", "f1trollofficial", old)
        await self.h._meme_maintain(self.h.meme)
        self.assertEqual(self.h.store.known_posts("f1trollofficial"), {"2"})
        self.assertEqual(self.rows("SELECT post_id FROM items")[0][0], "2")
        self.assertEqual(len(list(self.h.store.images.iterdir())), 1)
        self.assertEqual(tuple(self.rows("SELECT state,published_at FROM deliveries")[0]),
                         ("pending", newer.published_at))
        self.assertCountEqual([row[0] for row in self.rows("SELECT state FROM runs")],
                              ["running", "done"])

    async def test_cleanup_cannot_apply_an_old_download_to_a_reused_row_id(self) -> None:
        first, newer = post(1), post(2)
        source = "f1trollofficial"
        self.h.store.record_post(source, first)
        old_id = self.h.store.pending_items(source, 0)[0]["id"]
        self.h.store.cleanup(time.time() + 86400)
        self.h.store.record_post(source, newer)
        self.assertEqual(self.h.store.pending_items(source, 0)[0]["id"], old_id)
        old_key = (source, first.post_id, first.photos[0].media_id)
        self.h.store.download_failed(old_key, "late failure")
        self.assertFalse(self.h.store.accept_image(old_key, jpeg(first.photos[0].url), "jpg", "image/jpeg",
                                                   "unused", self.h.meme.targets, [source], 0, 200, 1024 * 1024))
        self.assertEqual([tuple(row) for row in self.rows("SELECT state,attempts,digest FROM items")],
                         [("pending", 0, None)])

    async def test_queue_cap_skips_oldest_and_retains_latest(self) -> None:
        self.h.meme.max_pending_per_group = 2
        await self.collect([page([1, 2, 3])], limit=3)
        states = self.rows("SELECT state FROM deliveries ORDER BY published_at")
        self.assertEqual([row[0] for row in states], ["skipped", "pending", "pending"])

    async def test_restart_distinguishes_prepared_images_from_inflight_sends(self) -> None:
        self.h.meme.send_jobs.append(MemeSendJobConfig(group_id="200"))
        await self.collect([page([1])], limit=1)
        sending, preparing = self.h.meme.send_jobs
        for key, job in (("inflight", sending), ("preflight", preparing)):
            self.h.store.start_run(key, "send", job.target_key, self.due.timestamp())
            self.h.store.claim(job.target_key, key, self.h.meme.source_names, 0)
        self.assertTrue(self.h.store.begin_delivery("inflight"))
        self.assertFalse(self.h.store.begin_delivery("inflight"))
        fresh = MemeStore(self.h.store.root)
        fresh.initialize()
        self.assertEqual([tuple(row) for row in self.rows("SELECT state,attempts FROM deliveries ORDER BY target")],
                         [("uncertain", 1), ("pending", 0)])
        self.assertEqual([row[0] for row in self.rows("SELECT target FROM sent_history")], [sending.target_key])
        self.assertEqual([row[0] for row in self.rows("SELECT state FROM runs WHERE kind='send' ORDER BY key")],
                         ["uncertain", "interrupted"])
        self.assertEqual(fresh.known_posts("f1trollofficial"), {"1"})


class MemeClientTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def http_error(code: int, payload: dict | bytes, *, retry_after: str = "") -> HTTPError:
        headers = Message()
        headers["Content-Type"] = "application/json"
        if retry_after:
            headers["Retry-After"] = retry_after
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return HTTPError("https://api.fxtwitter.com/2/profile/f1trollofficial/media?count=15", code, "upstream error", headers, io.BytesIO(raw))

    async def test_timeline_404_retries_then_recovers(self) -> None:
        client = FxEmbedClient()
        error = self.http_error(404, {"code": 404, "results": []})
        error_body = error.fp
        response = io.BytesIO(b'{"code":200,"results":[]}')
        response.headers = Message()
        response.headers["Content-Type"] = "application/json"
        with patch.object(client._opener, "open", side_effect=[error, response]) as request, patch("f1_info_plugin.meme_client.time.sleep") as sleep:
            result = await client.page("f1trollofficial", None, time.monotonic() + 10)
        self.assertEqual(result.posts, ())
        self.assertEqual(request.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        headers = {key.lower(): value for key, value in request.call_args.args[0].header_items()}
        self.assertEqual(headers["cache-control"], "no-cache")
        self.assertTrue(error_body.closed)

    async def test_persistent_timeline_404_is_bounded_and_keeps_error_detail(self) -> None:
        client = FxEmbedClient()
        errors = [self.http_error(404, {"code": 404, "message": "User not found"}) for _ in range(3)]
        with patch.object(client._opener, "open", side_effect=errors) as request, patch("f1_info_plugin.meme_client.time.sleep") as sleep:
            with self.assertRaises(MemeClientError) as caught:
                await client.page("f1trollofficial", None, time.monotonic() + 10)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("HTTP 404：User not found", str(caught.exception))
        self.assertIn("已尝试 3 次", str(caught.exception))

    async def test_image_404_is_not_retried(self) -> None:
        client = FxEmbedClient()
        with patch.object(client._opener, "open", side_effect=self.http_error(404, {"message": "media deleted"})) as request, patch("f1_info_plugin.meme_client.time.sleep") as sleep:
            with self.assertRaisesRegex(MemeClientError, "HTTP 404：media deleted"):
                await client.image("https://pbs.twimg.com/media/deleted.jpg", 1000, time.monotonic() + 10)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("Cache-control", request.call_args.args[0].headers)
        sleep.assert_not_called()

    async def test_nonretryable_http_error_has_short_single_line_detail(self) -> None:
        client = FxEmbedClient()
        for code in (400, 403):
            with self.subTest(code=code), patch.object(client._opener, "open", side_effect=self.http_error(code, {"message": "bad\n" + "x" * 500})) as request:
                with self.assertRaises(MemeClientError) as caught:
                    await client.page("f1trollofficial", None, time.monotonic() + 10)
                self.assertEqual(request.call_count, 1)
                self.assertIn(f"HTTP {code}：bad ", str(caught.exception))
                self.assertNotIn("\n", str(caught.exception))
                self.assertLess(len(str(caught.exception)), 240)
        with patch.object(client._opener, "open", side_effect=self.http_error(400, b"<html>upstream error</html>")):
            with self.assertRaisesRegex(MemeClientError, "^上游 HTTP 400$"):
                await client.page("f1trollofficial", None, time.monotonic() + 10)

    async def test_timeline_retry_after_respects_collection_deadline(self) -> None:
        client = FxEmbedClient()
        error = self.http_error(404, {"code": 404, "results": []}, retry_after="120")
        with patch.object(client._opener, "open", side_effect=error) as request, patch("f1_info_plugin.meme_client.time.sleep") as sleep:
            with self.assertRaises(MemeClientError) as caught:
                await client.page("f1trollofficial", None, time.monotonic() + 5)
        self.assertEqual(request.call_count, 1)
        self.assertIn("时间线返回空结果", str(caught.exception))
        self.assertIn("已尝试 1 次", str(caught.exception))
        sleep.assert_not_called()

    async def test_response_validation_filters_replies_reposts_quotes_and_gifs(self) -> None:
        def raw_post(number: str, **kwargs):
            return {"type": "status", "id": number, "author": {"id": "1", "screen_name": "f1trollofficial"},
                    "created_timestamp": time.time(), "media": {"photos": [{"type": "photo", "url": "https://pbs.twimg.com/media/test.jpg"}]}, **kwargs}
        data = {"code": 200, "results": [raw_post("1"), raw_post("2", replying_to={"status": "3"}), raw_post("3", reposted_by={"id": "2"}), raw_post("4", media={"photos": [{"type": "gif"}]})], "cursor": {"bottom": "next"}}
        result = FxEmbedClient.parse_page(data, "f1trollofficial")
        self.assertEqual([len(row.photos) for row in result.posts], [1, 0, 0, 0])
        self.assertEqual(result.posts[0].author_handle, "f1trollofficial")
        self.assertEqual(result.cursor, "next")
        self.assertEqual(result.posts[0].text, "")
        for text, expected in (("aabbcc 🏎️\n第二行", "aabbcc 🏎️\n第二行"), (None, ""), ({"text": "invalid"}, "")):
            with self.subTest(text=text):
                parsed = FxEmbedClient.parse_page({"code": 200, "results": [raw_post("1", text=text)]}, "f1trollofficial")
                self.assertEqual(parsed.posts[0].text, expected)
                self.assertEqual(len(parsed.posts[0].photos), 1)
        for invalid in ({"code": 500, "results": []}, {"code": 200}, {"code": 200, "results": [{}]}):
            with self.assertRaises(MemeClientError):
                FxEmbedClient.parse_page(invalid, "f1trollofficial")
        for handle in (None, "", 123):
            with self.subTest(handle=handle), self.assertRaises(MemeClientError):
                FxEmbedClient.parse_page({"code": 200, "results": [raw_post("1", author={"id": "1", "screen_name": handle})]}, "f1trollofficial")
        for url in ("http://pbs.twimg.com/media/x.jpg", "https://pbs.twimg.com.evil.test/media/x.jpg", "https://pbs.twimg.com/profile_images/x.jpg", "https://user@pbs.twimg.com/media/x.jpg"):
            with self.assertRaises(MemeClientError):
                validate_image_url(url)

    async def test_stream_limits_and_file_validation(self) -> None:
        client = FxEmbedClient()
        class Response(io.BytesIO):
            def __init__(self, data: bytes, mime: str, length: int | None = None):
                super().__init__(data)
                self.headers = Message()
                self.headers["Content-Type"] = mime
                if length is not None:
                    self.headers["Content-Length"] = str(length)
        with patch.object(client._opener, "open", return_value=Response(jpeg("valid"), "image/jpeg")):
            raw, extension, mime = await client.image("https://pbs.twimg.com/media/test.jpg", 100, time.monotonic() + 5)
            self.assertEqual((extension, mime), ("jpg", "image/jpeg"))
            self.assertEqual(hashlib.sha256(raw).digest(), hashlib.sha256(jpeg("valid")).digest())
        with patch.object(client._opener, "open", return_value=Response(b"x" * 101, "image/jpeg")):
            with self.assertRaises(MemeClientError):
                await client.image("https://pbs.twimg.com/media/test.jpg", 100, time.monotonic() + 5)
        with patch.object(client._opener, "open", return_value=Response(jpeg("short"), "image/jpeg", length=90)):
            with self.assertRaises(MemeClientError):
                await client.image("https://pbs.twimg.com/media/test.jpg", 100, time.monotonic() + 5)
        with self.assertRaises(MemeClientError):
            FxEmbedClient.image_format(b"<html>error</html>", "image/jpeg")


if __name__ == "__main__":
    unittest.main()
