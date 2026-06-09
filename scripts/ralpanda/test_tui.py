"""Tests for TUI display helpers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Allow running standalone or via unittest discover.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralpanda import tui


class TestWrap(unittest.TestCase):
    def test_wrap_breaks_long_path_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(
                Path(tmp) / ".ralpanda" / "logs" / "ralpanda-long-plan-001.jsonl"
            )

            wrapped = tui._wrap(path, 18)

            self.assertTrue(len(wrapped) > 1)
            self.assertTrue(all(len(line) <= 18 for line in wrapped))
            self.assertEqual("".join(wrapped), path)

    def test_append_wrapped_label_value_preserves_full_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(
                Path(tmp)
                / "My Project"
                / ".ralpanda"
                / "logs"
                / "ralpanda-long-plan-001.jsonl"
            )
            lines = []
            label = " Agent log: "

            tui._append_wrapped_label_value(lines, label, path, 0, 32)

            self.assertTrue(len(lines) > 1)
            self.assertTrue(all(len(text) <= 32 for text, _ in lines))
            recovered = lines[0][0][len(label):]
            recovered += "".join(text[len(label):] for text, _ in lines[1:])
            self.assertEqual(recovered, path)


class TestLogTailing(unittest.TestCase):
    def test_tail_log_file_uses_log_entry_ts(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "agent.jsonl"
            entries = [
                {
                    "ts": "2026-03-27T00:05:35-0700",
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "first line\nsecond line"},
                        ],
                    },
                },
                {
                    "ts": "2026-03-27T00:05:36-0700",
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}},
                        ],
                    },
                },
            ]
            log_path.write_text("\n".join(json.dumps(entry) for entry in entries))

            lines = []
            tui._tail_log_file(log_path, lines, 0)

        self.assertEqual(lines[0], ("00:05:35", "first line"))
        self.assertEqual(lines[1], ("", "second line"))
        self.assertEqual(lines[2][0], "00:05:36")
        self.assertIn("[tool: Bash]", lines[2][1])

    def test_tail_log_file_falls_back_to_timestamp_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "agent.jsonl"
            entry = {
                "timestamp": "2026-03-27T07:05:36.243Z",
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "fallback timestamp"},
                    ],
                },
            }
            log_path.write_text(json.dumps(entry))

            lines = []
            tui._tail_log_file(log_path, lines, 0)

        self.assertEqual(lines, [("07:05:36", "fallback timestamp")])

    def test_tail_log_file_infers_assistant_time_from_following_tool_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "agent.jsonl"
            entries = [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "about to read"},
                        ],
                    },
                    "request_id": "req_123",
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}},
                        ],
                    },
                    "request_id": "req_123",
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "tool_use_id": "toolu_123",
                                "type": "tool_result",
                                "content": "file contents",
                            },
                        ],
                    },
                    "timestamp": "2026-06-07T04:13:43.894Z",
                },
            ]
            log_path.write_text("\n".join(json.dumps(entry) for entry in entries))

            lines = []
            tui._tail_log_file(log_path, lines, 0)

        self.assertEqual(lines[0], ("04:13:43", "about to read"))
        self.assertEqual(lines[1][0], "04:13:43")
        self.assertIn("[tool: Read]", lines[1][1])

    def test_tail_log_file_marks_missing_or_invalid_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "agent.jsonl"
            entries = [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "missing timestamp"},
                        ],
                    },
                },
                {
                    "ts": "not-a-timestamp",
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "invalid timestamp"},
                        ],
                    },
                },
            ]
            log_path.write_text("\n".join(json.dumps(entry) for entry in entries))

            lines = []
            tui._tail_log_file(log_path, lines, 0)

        self.assertEqual(
            lines,
            [
                (tui.UNKNOWN_LOG_TIME, "missing timestamp"),
                (tui.UNKNOWN_LOG_TIME, "invalid timestamp"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
