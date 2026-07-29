from __future__ import annotations

from typing import Any


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
