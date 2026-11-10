"""有大小、重试和总截止时间限制的 FxEmbed JSON / 图片客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler

import asyncio
import json
import math
import ssl
import time


# 上游按帖子分页且返回数量可能不足；业务层按实际入库图片数累计 X。
PAGE_SIZE = 15
FETCH_BUDGET_SECONDS = 180.0
REQUEST_TIMEOUT_SECONDS = 20.0
USER_AGENT = "MaiBotF1InfoPlugin/1.5 (FxEmbed meme collector)"


class MemeClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemePhoto:
    media_id: str
    index: int
    url: str


@dataclass(frozen=True)
class MemePost:
    post_id: str
    author_id: str
    author_handle: str
    published_at: float
    url: str
    text: str
    photos: tuple[MemePhoto, ...]


@dataclass(frozen=True)
class MemePage:
    posts: tuple[MemePost, ...]
    cursor: str | None


def validate_image_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "pbs.twimg.com" or parts.port not in {None, 443} or parts.username or parts.password:
        raise MemeClientError("图片地址不是受支持的 X 图片 HTTPS 地址")
    if not parts.path.startswith("/media/"):
        raise MemeClientError("图片地址不是帖子媒体")
    return url


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request | None:
        # 图片重定向也执行相同限制；JSON 接口只访问固定主机。
        if urlsplit(req.full_url).hostname == "pbs.twimg.com":
            validate_image_url(newurl)
        elif urlsplit(newurl).scheme != "https" or urlsplit(newurl).hostname != "api.fxtwitter.com":
            raise MemeClientError("FxEmbed 接口重定向到非预期主机")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class FxEmbedClient:
    def __init__(self) -> None:
        self._opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), _SafeRedirect())

    async def _read(self, url: str, limit: int, deadline: float, *, image: bool = False) -> tuple[bytes, str]:
        # 工作线程只读网络，不修改队列；取消或截止后不会在后台继续入库。
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Meme 本轮采集总耗时已到")
        async with asyncio.timeout(remaining):
            return await asyncio.to_thread(self._read_sync, url, limit, deadline, image)

    def _read_sync(self, url: str, limit: int, deadline: float, image: bool) -> tuple[bytes, str]:
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Meme 本轮采集总耗时已到")
            try:
                headers = {"User-Agent": USER_AGENT, "Accept": "image/*" if image else "application/json"}
                if not image:
                    headers["Cache-Control"] = "no-cache"
                req = Request(url, headers=headers)
                with self._opener.open(req, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining)) as response:
                    content_type = response.headers.get_content_type()
                    length_header = response.headers.get("Content-Length")
                    length = int(length_header) if length_header is not None else None
                    if length is not None and (length < 0 or length > limit):
                        raise MemeClientError("响应体超过大小限制")
                    chunks: list[bytes] = []
                    received = 0
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Meme 本轮采集总耗时已到")
                        chunk = response.read(min(65536, limit + 1 - received))
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > limit:
                            raise MemeClientError("响应体超过大小限制")
                        chunks.append(chunk)
                    if length is not None and received != length:
                        raise MemeClientError("响应下载不完整")
                    return b"".join(chunks), content_type
            except HTTPError as exc:
                message = self._http_error_message(exc)
                # FxEmbed 时间线实测会间歇返回 404；仍受三次尝试和整轮截止时间限制。
                # 图片 404 表示原文件不可用，不采用这个时间线重试规则。
                if exc.code not in {429, 500, 502, 503, 504} and not (exc.code == 404 and not image):
                    raise MemeClientError(message) from exc
                retry_after = exc.headers.get("Retry-After", "")
                wait = self._retry_delay(retry_after, attempt)
                error: Exception = MemeClientError(message)
            except (URLError, OSError, TimeoutError) as exc:
                wait = 0.5 * (attempt + 1)
                error = exc
            if attempt == 2 or time.monotonic() + wait >= deadline:
                raise MemeClientError(f"上游请求失败（已尝试 {attempt + 1} 次）：{type(error).__name__}: {error}") from error
            time.sleep(wait)
        raise AssertionError("不可达的请求分支")

    @staticmethod
    def _http_error_message(error: HTTPError) -> str:
        # 只保留有界的 JSON 错误摘要，避免把 HTML 错误页或整段响应写入日志。
        try:
            with error:
                raw = error.read(4096)
            payload = json.loads(raw)
        except (OSError, ValueError):
            return f"上游 HTTP {error.code}"
        if not isinstance(payload, dict):
            return f"上游 HTTP {error.code}"
        detail = payload.get("message") or payload.get("error")
        if not detail and error.code == 404 and payload.get("results") == []:
            detail = "时间线返回空结果"
        if isinstance(detail, str):
            detail = " ".join("".join(char if char.isprintable() else " " for char in detail).split())[:200]
        else:
            detail = ""
        return f"上游 HTTP {error.code}" + (f"：{detail}" if detail else "")

    @staticmethod
    def _retry_delay(value: str, attempt: int) -> float:
        try:
            seconds = float(value)
            if math.isfinite(seconds):
                return max(0.5, seconds)
        except ValueError:
            try:
                return max(0.5, parsedate_to_datetime(value).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
        return 0.5 * (attempt + 1)

    async def page(self, source: str, cursor: str | None, deadline: float) -> MemePage:
        params = {"count": str(PAGE_SIZE)}
        if cursor:
            params["cursor"] = cursor
        raw, mime = await self._read(f"https://api.fxtwitter.com/2/profile/{source}/media?{urlencode(params)}", 4 * 1024 * 1024, deadline)
        if mime not in {"application/json", "text/json"}:
            raise MemeClientError("FxEmbed 返回的不是 JSON")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise MemeClientError("FxEmbed 返回无效 JSON") from exc
        return self.parse_page(payload, source)

    @staticmethod
    def parse_page(payload: Any, source: str) -> MemePage:
        if not isinstance(payload, dict) or payload.get("code") != 200:
            code = payload.get("code") if isinstance(payload, dict) else None
            raise MemeClientError(f"FxEmbed 响应失败，code={code}")
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise MemeClientError("FxEmbed 缺少 results 列表")
        posts: list[MemePost] = []
        for row in rows:
            if not isinstance(row, dict):
                raise MemeClientError("FxEmbed 帖子结构无效")
            if row.get("type") == "tombstone":
                continue
            post_id = row.get("id")
            if not isinstance(post_id, str) or not post_id.isdecimal():
                raise MemeClientError("FxEmbed 帖子缺少有效 ID")
            author = row.get("author")
            if not isinstance(author, dict):
                raise MemeClientError("FxEmbed 帖子缺少作者")
            author_handle = author.get("screen_name")
            if not isinstance(author_handle, str) or not author_handle:
                raise MemeClientError("FxEmbed 帖子缺少作者用户名")
            timestamp = row.get("created_timestamp")
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
                raise MemeClientError("FxEmbed 帖子发布时间无效")
            photos: list[MemePhoto] = []
            eligible = author_handle.lower() == source and not row.get("reposted_by") and not row.get("replying_to")
            if eligible:
                media = row.get("media") or {}
                if not isinstance(media, dict) or not isinstance(media.get("photos", []), list):
                    raise MemeClientError("FxEmbed 图片结构无效")
                for index, photo in enumerate(media.get("photos", [])):
                    if not isinstance(photo, dict) or photo.get("type") != "photo":
                        continue
                    url = photo.get("url")
                    if not isinstance(url, str):
                        raise MemeClientError("FxEmbed 图片缺少 URL")
                    validate_image_url(url)
                    photos.append(MemePhoto(str(photo.get("id") or index), index, url))
            text = row.get("text")
            posts.append(
                MemePost(
                    post_id=post_id,
                    author_id=str(author.get("id", "")),
                    author_handle=author_handle,
                    published_at=float(timestamp),
                    url=f"https://x.com/{author_handle}/status/{post_id}",
                    text=text if isinstance(text, str) else "",
                    photos=tuple(photos),
                )
            )
        cursor_data = payload.get("cursor") or {}
        if not isinstance(cursor_data, dict):
            raise MemeClientError("FxEmbed 分页结构无效")
        cursor = cursor_data.get("bottom")
        if cursor is not None and not isinstance(cursor, str):
            raise MemeClientError("FxEmbed 分页游标无效")
        return MemePage(tuple(posts), cursor or None)

    async def image(self, url: str, max_bytes: int, deadline: float) -> tuple[bytes, str, str]:
        raw, mime = await self._read(validate_image_url(url), max_bytes, deadline, image=True)
        extension = self.image_format(raw, mime)
        return raw, extension, mime

    @staticmethod
    def image_format(raw: bytes, mime: str) -> str:
        if mime == "image/jpeg" and len(raw) > 4 and raw.startswith(b"\xff\xd8\xff") and raw.endswith(b"\xff\xd9"):
            return "jpg"
        if mime == "image/png" and raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 33 and raw[-8:] == b"IEND\xaeB`\x82":
            return "png"
        if mime == "image/webp" and len(raw) >= 20 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" and int.from_bytes(raw[4:8], "little") + 8 == len(raw):
            return "webp"
        raise MemeClientError("图片格式不符或文件不完整，仅接收 JPEG、PNG、WebP 静态图片")
