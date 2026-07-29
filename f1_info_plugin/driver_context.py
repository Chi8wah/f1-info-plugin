from __future__ import annotations
# pyright: reportAttributeAccessIssue=false

from dataclasses import dataclass
from typing import Any

import re
import time
import unicodedata


_MAIBOT_EMBEDDED_MESSAGE_PATTERN = re.compile(
    r"<message\b(?P<attributes>[^>]*)>\s*(?P<content>.*?)(?=<message\b|<system-reminder\b|\Z)",
    flags=re.DOTALL | re.IGNORECASE,
)
_MAIBOT_SELF_MESSAGE_ATTRIBUTE_PATTERN = re.compile(
    r"\bis_self_message\s*=\s*(?:\"true\"|'true'|true)(?=\s|$)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DriverContextProfileData:
    """从用户配置归一化得到的单个车手上下文。"""

    driver_id: str
    name: str
    number: int | None
    aliases: tuple[str, ...]
    team: str
    info: str


@dataclass(frozen=True, slots=True)
class DriverContextSessionState:
    """Planner 与 Replyer 之间短时传递的本轮命中结果。"""

    profiles: tuple[DriverContextProfileData, ...]
    stored_at: float


class DriverContextMixin:
    """匹配用户维护的车手资料，并向 Planner/Replyer 生成上下文。"""

    _DRIVER_CONTEXT_SESSION_TTL_SECONDS = 600.0

    def _driver_context_enabled(self) -> bool:
        return bool(self.config.plugin.enabled and self.config.driver_context.enabled)

    def _configured_driver_context_profiles(self) -> list[DriverContextProfileData]:
        if not self._driver_context_enabled():
            return []

        profiles: list[DriverContextProfileData] = []
        seen_driver_ids: set[str] = set()
        for configured in self.config.driver_context.profiles:
            driver_id = str(configured.driver_id).strip()
            name = str(configured.name).strip()
            if not configured.enabled or not driver_id or not name:
                continue
            if driver_id in seen_driver_ids:
                continue
            seen_driver_ids.add(driver_id)

            aliases: list[str] = []
            seen_aliases: set[str] = set()
            for raw_alias in configured.aliases:
                alias = str(raw_alias).strip()
                normalized_alias = self._normalize_driver_context_text(alias).casefold()
                if not normalized_alias or normalized_alias in seen_aliases:
                    continue
                seen_aliases.add(normalized_alias)
                aliases.append(alias)

            profiles.append(
                DriverContextProfileData(
                    driver_id=driver_id,
                    name=name,
                    number=(
                        int(configured.number)
                        if configured.number is not None
                        else None
                    ),
                    aliases=tuple(aliases),
                    team=str(configured.team).strip(),
                    info=str(configured.info).strip(),
                )
            )
        return profiles

    def _match_driver_context_profiles(
        self,
        messages: list[dict[str, Any]],
    ) -> list[DriverContextProfileData]:
        profiles = self._configured_driver_context_profiles()
        if not profiles:
            return []

        recent_texts = self._recent_driver_context_user_texts(messages)
        if not recent_texts:
            return []

        alias_specs = self._driver_alias_specs(profiles)
        profile_order = {
            profile.driver_id: index for index, profile in enumerate(profiles)
        }
        matched: list[DriverContextProfileData] = []
        matched_ids: set[str] = set()
        limit = int(self.config.driver_context.max_matched_drivers)

        # 先处理最新用户消息；同一条消息中按首次出现位置和较长别名排序。
        for text in recent_texts:
            positions: dict[str, tuple[int, int, DriverContextProfileData]] = {}
            for alias, profile in alias_specs:
                position = self._find_driver_alias(text, alias)
                if position < 0:
                    continue
                candidate = (position, -len(alias), profile)
                current = positions.get(profile.driver_id)
                if current is None or candidate[:2] < current[:2]:
                    positions[profile.driver_id] = candidate

            ordered = sorted(
                positions.values(),
                key=lambda item: (
                    item[0],
                    item[1],
                    profile_order[item[2].driver_id],
                ),
            )
            for _, _, profile in ordered:
                if profile.driver_id in matched_ids:
                    continue
                matched.append(profile)
                matched_ids.add(profile.driver_id)
                if len(matched) >= limit:
                    return matched
        return matched

    def _recent_driver_context_user_texts(
        self,
        messages: list[dict[str, Any]],
    ) -> list[str]:
        limit = int(self.config.driver_context.recent_user_message_limit)
        user_contents = [
            self._driver_context_content_text(message.get("content"))
            for message in messages
            if isinstance(message, dict) and message.get("role") == "user"
        ]
        # MaiBot 会把时间、上下文恢复、工具列表和人物画像等内部信息也标为
        # user；只要本轮出现 <message>，便以其作为真实聊天正文的边界。
        has_embedded_maibot_messages = any(
            _MAIBOT_EMBEDDED_MESSAGE_PATTERN.search(content)
            for content in user_contents
        )
        texts: list[str] = []
        for content in reversed(user_contents):
            if (
                has_embedded_maibot_messages
                and not _MAIBOT_EMBEDDED_MESSAGE_PATTERN.search(content)
            ):
                continue
            for text in self._embedded_maibot_user_texts(content):
                if not text:
                    continue
                texts.append(text)
                if len(texts) >= limit:
                    return texts
        return texts

    @staticmethod
    def _embedded_maibot_user_texts(content: str) -> list[str]:
        """提取 MaiBot 聊天记录中的外部用户消息，跳过机器人自己的历史发言。"""

        embedded_messages = list(_MAIBOT_EMBEDDED_MESSAGE_PATTERN.finditer(content))
        if not embedded_messages:
            text = content.strip()
            return [text] if text else []

        texts: list[str] = []
        for match in reversed(embedded_messages):
            if _MAIBOT_SELF_MESSAGE_ATTRIBUTE_PATTERN.search(
                match.group("attributes")
            ):
                continue
            text = match.group("content").strip()
            if text:
                texts.append(text)
        return texts

    @classmethod
    def _driver_context_content_text(cls, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                text
                for item in content
                if (text := cls._driver_context_content_text(item))
            )
        if isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                return text
            nested_content = content.get("content")
            if nested_content is not content:
                return cls._driver_context_content_text(nested_content)
        return ""

    def _driver_alias_specs(
        self,
        profiles: list[DriverContextProfileData],
    ) -> list[tuple[str, DriverContextProfileData]]:
        aliases_by_profile: list[tuple[str, DriverContextProfileData]] = []
        for profile in profiles:
            for alias in (profile.name, *profile.aliases):
                normalized = self._normalize_driver_context_text(alias)
                if not normalized:
                    continue
                aliases_by_profile.append((normalized, profile))
            if profile.number is not None:
                aliases_by_profile.append((str(profile.number), profile))
        return aliases_by_profile

    @staticmethod
    def _normalize_driver_context_text(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", str(value)).split())

    @classmethod
    def _find_driver_alias(cls, text: str, alias: str) -> int:
        normalized_text = cls._normalize_driver_context_text(text)
        normalized_alias = cls._normalize_driver_context_text(alias)
        if not normalized_text or not normalized_alias:
            return -1

        if normalized_alias.isascii():
            pattern = re.compile(
                rf"(?<![0-9A-Za-z]){re.escape(normalized_alias)}(?![0-9A-Za-z])",
                flags=re.IGNORECASE,
            )
            match = pattern.search(normalized_text)
            return match.start() if match else -1

        return normalized_text.casefold().find(normalized_alias.casefold())

    @staticmethod
    def _render_driver_context(
        profiles: list[DriverContextProfileData] | tuple[DriverContextProfileData, ...],
        *,
        planner: bool,
    ) -> str:
        if not profiles:
            return ""
        lines = [
            "【F1 车手资料补充】",
            "以下资料仅用于补充车手信息及相关社区梗，不代表实时赛果或官方消息。",
        ]
        for profile in profiles:
            lines.append(f"车手：{profile.name}")
            if profile.number is not None:
                lines.append(f"车手号码：{profile.number}")
            if profile.aliases:
                lines.append(f"外号/别名：{'、'.join(profile.aliases)}")
            if profile.team:
                lines.append(f"所在车队：{profile.team}")
            if profile.info:
                lines.append(f"补充信息：{profile.info}")
        if planner:
            lines.append(
                "涉及积分、排名、最新赛果、处罚、合同或转会等实时问题时，"
                "请调用相应 F1 Tool 核实。"
            )
        else:
            lines.append("请自然运用这些称呼和语境，不必主动复述整份资料或说明资料来源。")
        return "\n".join(lines)

    def _planner_driver_context_text(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[DriverContextProfileData]]:
        profiles = self._match_driver_context_profiles(messages)
        return self._render_driver_context(profiles, planner=True), profiles

    def _replyer_driver_context_text(self, session_id: str) -> str:
        profiles = self._remembered_driver_context_profiles(session_id)
        return self._render_driver_context(profiles, planner=False)

    def _remember_driver_context_profiles(
        self,
        session_id: str,
        profiles: list[DriverContextProfileData],
        monotonic_now: float | None = None,
    ) -> None:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return
        now = time.monotonic() if monotonic_now is None else monotonic_now
        self._prune_driver_context_session_states(now)
        if not profiles:
            self._driver_context_session_states.pop(normalized_session_id, None)
            return
        self._driver_context_session_states[normalized_session_id] = (
            DriverContextSessionState(tuple(profiles), now)
        )

    def _remembered_driver_context_profiles(
        self,
        session_id: str,
        monotonic_now: float | None = None,
    ) -> tuple[DriverContextProfileData, ...]:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id or not self._driver_context_enabled():
            return ()
        now = time.monotonic() if monotonic_now is None else monotonic_now
        self._prune_driver_context_session_states(now)
        state = self._driver_context_session_states.get(normalized_session_id)
        return state.profiles if state else ()

    def _prune_driver_context_session_states(self, monotonic_now: float) -> None:
        expired_session_ids = [
            session_id
            for session_id, state in self._driver_context_session_states.items()
            if monotonic_now - state.stored_at > self._DRIVER_CONTEXT_SESSION_TTL_SECONDS
        ]
        for session_id in expired_session_ids:
            self._driver_context_session_states.pop(session_id, None)

    def _clear_driver_context_session_states(self) -> None:
        self._driver_context_session_states.clear()
