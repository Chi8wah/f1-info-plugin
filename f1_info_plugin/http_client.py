from __future__ import annotations
# pyright: reportAttributeAccessIssue=false

import asyncio
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .constants import EXTERNAL_CATEGORY_PHRASES, EXTERNAL_SOURCE_LABELS
from .models import F1ExternalApiError, OpenF1UnavailableError


class HttpClientMixin:

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

    def _log_warning(self, message: str, *args: Any) -> None:
        logger_obj = getattr(getattr(self, "ctx", None), "logger", None)
        if logger_obj is not None:
            logger_obj.warning(message, *args)
