from __future__ import annotations
# pyright: reportAttributeAccessIssue=false

import asyncio
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .constants import BEIJING_TZ, NEWS_FALLBACK_NOTICE, UTC
from .models import F1ExternalApiError, NewsItem, NewsPageData, NewsSummaryData


class NewsMixin:

    @staticmethod
    def _parse_feed_datetime(raw: str) -> datetime | None:
        if not raw:
            return None
        try:
            return parsedate_to_datetime(raw).astimezone(UTC)
        except (TypeError, ValueError, IndexError):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                return None

    def _news_summary_urls(self, items: list[NewsSummaryData]) -> set[str]:
        return {
            normalized
            for item in items
            if (normalized := self._normalize_news_url(item.url))
        }

    async def _news_page_data(self, limit: int = 10, force_refresh: bool = False) -> NewsPageData | str:
        if not self.config.plugin.enabled:
            return "F1 资讯插件未启用。"
        limit = max(1, min(int(limit or self.config.news.daily_limit), 20))
        cache_key = f"news:{datetime.now(BEIJING_TZ).date().isoformat()}:{limit}"
        cache_row = self._get_cache_row(cache_key)
        stale_urls: set[str] = set()
        if cache_row:
            cache_expired = self._cache_expired(cache_row)
            if not force_refresh and not cache_expired:
                cached_page = self._cache_news_page(cache_row)
                if cached_page is not None:
                    return cached_page
                cached_groups = self._cache_news_groups(cache_row)
                if cached_groups:
                    return await self._news_page_data_from_groups(cache_key, cached_groups, limit)
                cached_text = str(cache_row.get("value") or "")
                if cached_text:
                    return cached_text
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
        return await self._news_page_data_from_groups(cache_key, groups[:limit], limit)

    async def _news_text(self, limit: int = 10, force_refresh: bool = False, include_urls: bool = True) -> str:
        page = await self._news_page_data(limit=limit, force_refresh=force_refresh)
        if isinstance(page, str):
            return page if include_urls else self._remove_news_urls(page)
        return self._render_news_text(page, include_urls=include_urls)

    async def _news_page_data_from_groups(
        self,
        cache_key: str,
        groups: list[dict[str, Any]],
        limit: int,
    ) -> NewsPageData:
        selected = groups[:limit]
        items = await self._generate_news_summary_items(selected, limit)
        using_raw_fallback = not items
        notice = ""
        if using_raw_fallback:
            notice = NEWS_FALLBACK_NOTICE
            items = self._fallback_news_summary_items(selected, limit)
        page = NewsPageData(title="今日 F1 重要新闻", items=items, notice=notice, using_raw_fallback=using_raw_fallback)
        cache_urls = self._news_group_urls(selected)
        if not using_raw_fallback:
            cache_urls.update(self._news_summary_urls(items))
        self._set_cache(
            cache_key,
            "" if using_raw_fallback else self._render_news_text(page, include_urls=True),
            ttl_seconds=self.config.news.cache_ttl_minutes * 60,
            urls=cache_urls,
            news_groups=selected,
            news_items=items,
            news_notice=notice,
            using_raw_fallback=using_raw_fallback,
        )
        await self._save_cache_async()
        return page

    async def _news_text_from_groups(
        self,
        cache_key: str,
        groups: list[dict[str, Any]],
        limit: int,
        include_urls: bool,
    ) -> str:
        page = await self._news_page_data_from_groups(cache_key, groups, limit)
        return self._render_news_text(page, include_urls=include_urls)

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

    async def _generate_news_summary_items(self, groups: list[dict[str, Any]], limit: int) -> list[NewsSummaryData]:
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
                return []
            response = str(result.get("response") or "").strip()
            return self._normalize_llm_news_response_items(response, groups, limit)
        except Exception as exc:
            self.ctx.logger.warning("生成 F1 新闻摘要失败: %s", exc)
            return []

    async def _generate_news_summary(self, groups: list[dict[str, Any]], limit: int) -> str:
        items = await self._generate_news_summary_items(groups, limit)
        return self._render_news_items_text(items)

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

    def _normalize_llm_news_response_items(self, response: str, groups: list[dict[str, Any]], limit: int) -> list[NewsSummaryData]:
        if not response:
            return []
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
        items: list[NewsSummaryData] = []
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
                    items.append(NewsSummaryData(summary=summary, url=url))
                elif group.get("items"):
                    items.append(self._fallback_news_summary_item(group))
        return items

    def _normalize_llm_news_response(self, response: str, groups: list[dict[str, Any]], limit: int) -> str:
        items = self._normalize_llm_news_response_items(response, groups, limit)
        return self._render_news_items_text(items)

    @staticmethod
    def _render_news_items_text(items: list[NewsSummaryData]) -> str:
        return "\n".join(
            f"{idx}. {item.summary} {item.url}".rstrip()
            for idx, item in enumerate(items, 1)
        )

    @staticmethod
    def _contains_chinese(text: str) -> bool:
        return any("\u4e00" <= char <= "\u9fff" for char in text)

    @staticmethod
    def _is_raw_news_fallback(text: str) -> bool:
        return NEWS_FALLBACK_NOTICE in text

    def _fallback_news_summary_items(self, groups: list[dict[str, Any]], limit: int) -> list[NewsSummaryData]:
        return [
            self._fallback_news_summary_item(group)
            for group in groups[:limit]
        ]

    def _fallback_news_summary_item(self, group: dict[str, Any]) -> NewsSummaryData:
        best = self._best_item(group)
        title = self._clean_text(best.title) or "无标题"
        description = self._clean_text(best.description)
        summary_lines = [f"{best.source}: {title}"]
        if description and description != title:
            summary_lines.append(f"   导语：{description[:300]}")
        return NewsSummaryData(summary="\n".join(summary_lines), url=best.url)

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

    @staticmethod
    def _child_text(element: ET.Element, names: list[str]) -> str:
        for name in names:
            node = element.find(name)
            if node is not None and node.text:
                return NewsMixin._clean_text(node.text)
        return ""

    @staticmethod
    def _child_text_ns(element: ET.Element, names: list[str], namespace: dict[str, str]) -> str:
        for name in names:
            node = element.find(name, namespace)
            if node is not None and node.text:
                return NewsMixin._clean_text(node.text)
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
