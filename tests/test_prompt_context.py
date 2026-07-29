from __future__ import annotations

import unittest

from f1_info_plugin.prompt_context import (
    is_primary_planner_request,
    merge_planner_system_context,
)


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


if __name__ == "__main__":
    _ = unittest.main()
