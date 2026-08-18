from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal


SUPPORTED_CONTEXT_ITEM_SCHEMA_VERSION = 1

PlannerPayloadKey = Literal["messages", "items"]

_CONTEXT_ITEM_ROLE_BY_TYPE = {
    "SystemMessageItem": "system",
    "UserMessageItem": "user",
    "AssistantMessageItem": "assistant",
}


def is_primary_planner_request(tool_definitions: Any) -> bool:
    """判断 Hook 是否来自具备内置 reply 工具的主 Planner 请求。"""

    # MaiBot 当前会让表达选择、行为分析、表情选择等子任务复用
    # `maisaka.planner.before_request`。Host 尚未把 request_kind 传给 Hook，
    # 因此不能只根据 Hook 名称判断；内置 reply 工具是主 Planner 的稳定边界。
    if not isinstance(tool_definitions, list):
        return False
    for definition in tool_definitions:
        if not isinstance(definition, dict):
            continue
        direct_name = definition.get("name")
        if isinstance(direct_name, str) and direct_name == "reply":
            return True
        function = definition.get("function")
        if (
            isinstance(function, dict)
            and isinstance(function.get("name"), str)
            and function["name"] == "reply"
        ):
            return True
    return False


def _system_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        raise ValueError("system 消息 content 必须是字符串或纯文本列表")

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            text = item.strip()
        elif (
            isinstance(item, dict)
            and str(item.get("type", "text")).strip().lower() == "text"
            and isinstance(item.get("text"), str)
        ):
            text = str(item["text"]).strip()
        else:
            raise ValueError("system 消息 content 列表只能包含纯文本片段")
        if text:
            parts.append(text)
    return "\n".join(parts)


def _contains_context_block(content: str, context_text: str) -> bool:
    return (
        content == context_text
        or content.startswith(f"{context_text}\n\n")
        or content.endswith(f"\n\n{context_text}")
        or f"\n\n{context_text}\n\n" in content
    )


def _context_item_parts_text(parts: Any) -> str:
    if not isinstance(parts, list):
        raise ValueError("Context Item 的 parts 必须是列表")

    texts: list[str] = []
    for part in parts:
        if (
            not isinstance(part, dict)
            or str(part.get("type", "")).strip().lower() != "text"
            or not isinstance(part.get("text"), str)
        ):
            raise ValueError("SystemMessageItem 只能包含文本 parts")
        text = str(part["text"]).strip()
        if text:
            texts.append(text)
    return "\n".join(texts)


def _context_items_message_view(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将新版 Context Items 投影为仅供插件匹配使用的旧式消息视图。"""

    messages: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Context Item 必须是字典")
        role = _CONTEXT_ITEM_ROLE_BY_TYPE.get(item.get("item_type"))
        if role is None:
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            raise ValueError(f"{item.get('item_type')} 的 parts 必须是列表")
        messages.append(
            {
                "role": role,
                "content": deepcopy(parts),
            }
        )
    return messages


def resolve_planner_context_payload(
    *,
    messages: list[dict[str, Any]] | None,
    items: list[dict[str, Any]] | None,
    item_schema_version: Any,
) -> tuple[
    PlannerPayloadKey,
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """选择当前 Host 使用的 Planner 载荷，并提供统一的消息匹配视图。"""

    # 新 Host 可能为了兼容旧插件同时暴露 messages；只要 items 存在，就以
    # Host 实际消费的新版载荷为准，避免同时改写两个事实来源。
    if items is not None:
        if not isinstance(items, list):
            raise ValueError("Planner Hook 的 items 必须是列表")
        if (
            not isinstance(item_schema_version, int)
            or isinstance(item_schema_version, bool)
            or item_schema_version != SUPPORTED_CONTEXT_ITEM_SCHEMA_VERSION
        ):
            raise ValueError(
                "不支持的 Planner Context Item schema 版本: "
                f"{item_schema_version!r}"
            )
        return "items", items, _context_items_message_view(items)

    if not isinstance(messages, list):
        raise ValueError("Planner Hook 未提供可识别的 messages 或 items 载荷")
    return "messages", messages, messages


def merge_planner_system_context(
    messages: list[dict[str, Any]],
    context_text: str,
) -> list[dict[str, Any]]:
    """将 Planner 上下文合并到唯一的开头 system 消息中。"""

    # 部分 OpenAI 兼容端点只接受索引 0 的唯一 system 消息。这里统一折叠
    # 所有前导 system，而不是新增一条，避免再次触发 400 BadRequestError。
    updated_messages = [dict(message) for message in messages]
    normalized_context = str(context_text or "").strip()
    if not normalized_context:
        return updated_messages

    leading_system_count = 0
    leading_system_contents: list[str] = []
    while leading_system_count < len(updated_messages):
        message = updated_messages[leading_system_count]
        if message.get("role") != "system":
            break
        content = _system_content_text(message.get("content"))
        if content:
            leading_system_contents.append(content)
        leading_system_count += 1

    if leading_system_count == 0:
        updated_messages.insert(
            0,
            {"role": "system", "content": normalized_context},
        )
        return updated_messages

    merged_content = "\n\n".join(leading_system_contents)
    if not _contains_context_block(merged_content, normalized_context):
        merged_content = "\n\n".join(
            block for block in (merged_content, normalized_context) if block
        )

    merged_system_message = dict(updated_messages[0])
    merged_system_message["role"] = "system"
    merged_system_message["content"] = merged_content
    updated_messages[:leading_system_count] = [merged_system_message]
    return updated_messages


def merge_planner_system_context_items(
    items: list[dict[str, Any]],
    context_text: str,
) -> list[dict[str, Any]]:
    """将 Planner 上下文合并到新版载荷开头唯一的 SystemMessageItem。"""

    updated_items = deepcopy(items)
    normalized_context = str(context_text or "").strip()
    if not normalized_context:
        return updated_items

    leading_system_count = 0
    leading_system_contents: list[str] = []
    while leading_system_count < len(updated_items):
        item = updated_items[leading_system_count]
        if not isinstance(item, dict):
            raise ValueError("Context Item 必须是字典")
        if item.get("item_type") != "SystemMessageItem":
            break
        content = _context_item_parts_text(item.get("parts"))
        if content:
            leading_system_contents.append(content)
        leading_system_count += 1

    # 当前 Host 始终在索引 0 创建系统 Item。缺失时直接暴露协议错误，避免插件
    # 自行伪造 item_id、logical_turn_id 和 timestamp。
    if leading_system_count == 0:
        raise ValueError("Planner Context Items 缺少开头的 SystemMessageItem")

    merged_content = "\n\n".join(leading_system_contents)
    if not _contains_context_block(merged_content, normalized_context):
        merged_content = "\n\n".join(
            block for block in (merged_content, normalized_context) if block
        )

    merged_system_item = dict(updated_items[0])
    merged_system_item["parts"] = [{"type": "text", "text": merged_content}]
    updated_items[:leading_system_count] = [merged_system_item]
    return updated_items


def merge_planner_system_context_payload(
    payload_key: PlannerPayloadKey,
    payload: list[dict[str, Any]],
    context_text: str,
) -> list[dict[str, Any]]:
    """按 Host 载荷版本合并 Planner system 上下文。"""

    if payload_key == "items":
        return merge_planner_system_context_items(payload, context_text)
    return merge_planner_system_context(payload, context_text)
