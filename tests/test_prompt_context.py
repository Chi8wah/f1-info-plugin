from __future__ import annotations

import unittest

from f1_info_plugin.prompt_context import (
    is_primary_planner_request,
    merge_planner_system_context,
    merge_planner_system_context_items,
    resolve_planner_context_payload,
)


def _context_item(
    item_type: str,
    text: str,
    *,
    item_id: str,
) -> dict[str, object]:
    return {
        "item_type": item_type,
        "meta": {
            "item_id": item_id,
            "logical_turn_id": None,
            "timestamp": "2026-08-18T12:00:00+08:00",
        },
        "parts": [{"type": "text", "text": text}],
    }


class PrimaryPlannerRequestTest(unittest.TestCase):
    def test_openai_reply_tool_identifies_the_primary_planner(self) -> None:
        self.assertTrue(
            is_primary_planner_request(
                [
                    {
                        "type": "function",
                        "function": {"name": "reply"},
                    }
                ]
            )
        )

    def test_auxiliary_or_missing_tool_sets_are_not_primary_planner(self) -> None:
        self.assertFalse(is_primary_planner_request([]))
        self.assertFalse(
            is_primary_planner_request(
                [{"type": "function", "function": {"name": "web_search"}}]
            )
        )
        self.assertFalse(is_primary_planner_request(None))

    def test_legacy_direct_tool_name_is_supported(self) -> None:
        self.assertTrue(is_primary_planner_request([{"name": "reply"}]))


class PlannerSystemContextTest(unittest.TestCase):
    def test_context_is_merged_into_the_existing_system_message(self) -> None:
        original_messages = [
            {"role": "system", "content": "base", "metadata": "keep"},
            {"role": "user", "content": "hello"},
        ]

        merged = merge_planner_system_context(original_messages, "driver context")

        self.assertEqual(
            merged,
            [
                {
                    "role": "system",
                    "content": "base\n\ndriver context",
                    "metadata": "keep",
                },
                {"role": "user", "content": "hello"},
            ],
        )
        self.assertEqual(original_messages[0]["content"], "base")

    def test_context_is_inserted_at_zero_when_system_message_is_absent(self) -> None:
        merged = merge_planner_system_context(
            [{"role": "user", "content": "hello"}],
            "schedule context",
        )

        self.assertEqual(
            merged,
            [
                {"role": "system", "content": "schedule context"},
                {"role": "user", "content": "hello"},
            ],
        )

    def test_multiple_contexts_keep_one_system_message_and_are_idempotent(
        self,
    ) -> None:
        messages = [
            {"role": "system", "content": "base"},
            {"role": "system", "content": "existing context"},
            {"role": "user", "content": "hello"},
        ]

        merged = merge_planner_system_context(messages, "driver context")
        merged = merge_planner_system_context(merged, "schedule context")
        merged = merge_planner_system_context(merged, "driver context")

        self.assertEqual(
            [message["role"] for message in merged],
            ["system", "user"],
        )
        self.assertEqual(
            merged[0]["content"],
            "base\n\nexisting context\n\ndriver context\n\nschedule context",
        )
        self.assertEqual(merged[0]["content"].count("driver context"), 1)

    def test_text_part_lists_are_normalized_to_one_string(self) -> None:
        merged = merge_planner_system_context(
            [
                {
                    "role": "system",
                    "content": ["base", {"type": "text", "text": "rules"}],
                },
                {"role": "user", "content": "hello"},
            ],
            "schedule context",
        )

        self.assertEqual(
            merged[0]["content"],
            "base\nrules\n\nschedule context",
        )

    def test_non_text_system_content_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            merge_planner_system_context(
                [
                    {
                        "role": "system",
                        "content": [{"type": "image", "image_url": "data:test"}],
                    }
                ],
                "schedule context",
            )


class PlannerContextItemsTest(unittest.TestCase):
    def test_context_is_merged_into_one_system_item_and_meta_is_preserved(
        self,
    ) -> None:
        original_items = [
            _context_item("SystemMessageItem", "base", item_id="system-1"),
            _context_item(
                "SystemMessageItem",
                "existing context",
                item_id="system-2",
            ),
            _context_item("UserMessageItem", "hello", item_id="user-1"),
        ]

        merged = merge_planner_system_context_items(
            original_items,
            "driver context",
        )
        merged = merge_planner_system_context_items(merged, "schedule context")
        merged = merge_planner_system_context_items(merged, "driver context")

        self.assertEqual(
            [item["item_type"] for item in merged],
            ["SystemMessageItem", "UserMessageItem"],
        )
        self.assertEqual(merged[0]["meta"], original_items[0]["meta"])
        self.assertEqual(
            merged[0]["parts"],
            [
                {
                    "type": "text",
                    "text": (
                        "base\n\nexisting context\n\ndriver context"
                        "\n\nschedule context"
                    ),
                }
            ],
        )
        self.assertEqual(
            original_items[0]["parts"],
            [{"type": "text", "text": "base"}],
        )

    def test_new_items_take_precedence_and_provide_a_message_view(self) -> None:
        items = [
            _context_item("SystemMessageItem", "base", item_id="system-1"),
            _context_item("UserMessageItem", "聊聊潘子", item_id="user-1"),
        ]

        payload_key, payload, messages = resolve_planner_context_payload(
            messages=[{"role": "user", "content": "legacy"}],
            items=items,
            item_schema_version=1,
        )

        self.assertEqual(payload_key, "items")
        self.assertIs(payload, items)
        self.assertEqual(
            messages,
            [
                {
                    "role": "system",
                    "content": [{"type": "text", "text": "base"}],
                },
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "聊聊潘子"}],
                },
            ],
        )

    def test_legacy_messages_are_used_when_items_are_absent(self) -> None:
        messages = [{"role": "user", "content": "legacy"}]

        payload_key, payload, message_view = resolve_planner_context_payload(
            messages=messages,
            items=None,
            item_schema_version=None,
        )

        self.assertEqual(payload_key, "messages")
        self.assertIs(payload, messages)
        self.assertIs(message_view, messages)

    def test_unknown_item_schema_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "schema 版本"):
            resolve_planner_context_payload(
                messages=None,
                items=[
                    _context_item(
                        "SystemMessageItem",
                        "base",
                        item_id="system-1",
                    )
                ],
                item_schema_version=2,
            )

    def test_items_without_a_leading_system_item_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "SystemMessageItem"):
            merge_planner_system_context_items(
                [
                    _context_item(
                        "UserMessageItem",
                        "hello",
                        item_id="user-1",
                    )
                ],
                "driver context",
            )


if __name__ == "__main__":
    _ = unittest.main()
