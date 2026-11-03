from __future__ import annotations
# pyright: reportAttributeAccessIssue=false

import asyncio
import errno
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .constants import LEGACY_CACHE_PATH, UTC
from .models import NewsItem, NewsPageData, NewsSummaryData


class CacheMixin:

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

    def _cache_news_page(self, row: dict[str, Any]) -> NewsPageData | None:
        raw_items = row.get("news_items")
        if not isinstance(raw_items, list):
            return None
        items: list[NewsSummaryData] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            summary = str(raw_item.get("summary") or "").strip()
            url = str(raw_item.get("url") or "").strip()
            if summary:
                items.append(NewsSummaryData(summary=summary, url=url))
        if not items:
            return None
        return NewsPageData(
            title=str(row.get("news_title") or "今日 F1 重要新闻"),
            items=items,
            beijing_date=str(row.get("news_beijing_date") or ""),
            notice=str(row.get("news_notice") or ""),
            using_raw_fallback=bool(row.get("using_raw_fallback")),
        )

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
        news_items: list[NewsSummaryData] | None = None,
        news_beijing_date: str = "",
        news_notice: str = "",
        using_raw_fallback: bool = False,
    ) -> None:
        row = {
            "value": value,
            "expires_at": time.time() + ttl_seconds,
            "urls": sorted(urls or set()),
        }
        if news_groups is not None:
            row["news_groups"] = self._serialize_news_groups(news_groups)
        if news_items is not None:
            row["news_title"] = "今日 F1 重要新闻"
            if news_beijing_date:
                row["news_beijing_date"] = news_beijing_date
            row["news_notice"] = news_notice
            row["using_raw_fallback"] = using_raw_fallback
            row["news_items"] = [
                {"summary": item.summary, "url": item.url}
                for item in news_items
            ]
        self._cache[key] = row

    def _cache_path(self) -> Path:
        return Path(self.ctx.paths.data_dir) / "cache.json"

    def _migrate_legacy_cache(self) -> None:
        """复制成功后再清理旧缓存，兼容源码与持久化目录位于不同文件系统。"""

        cache_path = self._cache_path()
        if not LEGACY_CACHE_PATH.is_file() or LEGACY_CACHE_PATH.resolve() == cache_path.resolve():
            return

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=cache_path.parent, prefix="cache.legacy.", suffix=".json", delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(LEGACY_CACHE_PATH.read_bytes())
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            # 已有的新缓存优先，旧缓存另存为备份，避免覆盖升级后产生的数据。
            destination = temporary_path if cache_path.exists() else cache_path
            if destination != temporary_path:
                temporary_path.replace(destination)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

        LEGACY_CACHE_PATH.unlink()
        try:
            LEGACY_CACHE_PATH.parent.rmdir()
        except OSError as exc:
            # 只清理空目录，保留用户自行放入的其他文件。
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise
        self.ctx.logger.info("F1 旧版缓存已迁移: %s -> %s", LEGACY_CACHE_PATH, destination)

    def _load_cache(self) -> dict[str, Any]:
        self._migrate_legacy_cache()
        cache_path = self._cache_path()
        if not cache_path.exists():
            return {}
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    async def _save_cache_async(self) -> None:
        async with self._cache_lock:
            await asyncio.to_thread(self._save_cache)

    def _save_cache(self) -> None:
        cache_path = self._cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")
