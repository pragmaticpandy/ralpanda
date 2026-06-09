"""Tests for prompt outcome paths."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Allow running standalone or via unittest discover
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralpanda import dag, prompt


class TempCwd:
    def __enter__(self) -> Path:
        self.old_cwd = Path.cwd()
        self.root = Path(tempfile.mkdtemp()).resolve()
        os.chdir(self.root)
        return self.root

    def __exit__(self, exc_type, exc, tb) -> None:
        os.chdir(self.old_cwd)


class TestPromptOutcomePaths(unittest.TestCase):
    def test_work_prompt_uses_absolute_outcome_path(self):
        with TempCwd() as root:
            ralpanda = Path(".ralpanda")
            task_id = "ralpanda/test-plan/001"
            task = {
                "id": task_id,
                "title": "Do work",
                "type": "work",
                "status": "running",
                "depends_on": [],
                "description": "Do the work.",
                "acceptance_criteria": [],
                "outcome": None,
                "attempt": 1,
            }

            text = prompt.build_work_prompt(task, [task], ralpanda)
            expected = (
                root
                / ".ralpanda/outcomes/ralpanda-test-plan-001/attempt-1/work.json"
            )

            self.assertIn(f"`{expected}`", text)
            self.assertIn("This is an absolute path.", text)
            self.assertIn("even if you change directories", text)

    def test_review_prompt_normalizes_relative_outcome_path(self):
        with TempCwd() as root:
            text = prompt.build_review_check_prompt(
                "code-quality",
                "Check code quality.",
                "parallel",
                "ralpanda/test-plan/002",
                1,
                "abc123",
                ".ralpanda/outcomes/review.json",
                "review_check",
                "review/everything-else/code-quality",
            )

            self.assertIn(f"`{root / '.ralpanda/outcomes/review.json'}`", text)
            self.assertIn("This is an absolute path.", text)

    def test_review_compatibility_prompt_normalizes_relative_outcome_path(self):
        with TempCwd() as root:
            text = prompt.build_review_compatibility_prompt(
                "compat-code-quality",
                "Check compatibility.",
                "parallel",
                "ralpanda/test-plan/001",
                1,
                ".ralpanda/plans/test-plan.md",
                "code-quality",
                "Check code quality.",
                ".ralpanda/outcomes/compat.json",
                "review_compatibility_check",
                "review_compatibility/checks/compat-code-quality",
            )

            self.assertIn(f"`{root / '.ralpanda/outcomes/compat.json'}`", text)
            self.assertIn("This is an absolute path.", text)

    def test_coordinator_prompt_normalizes_relative_outcome_path(self):
        with TempCwd() as root:
            text = prompt.build_coordinator_prompt(
                "ralpanda/test-plan/002",
                [{"name": "code-quality", "stage": "review"}],
                ["Duplicate helper functions."],
                ".ralpanda/plans/test-plan.md",
                "ralpanda/test-plan/",
                2,
                ["ralpanda/test-plan/001"],
                1,
                ".ralpanda/outcomes/coordinator.json",
                dag.coordinator_agent_namespace(1),
            )

            self.assertIn(f"`{root / '.ralpanda/outcomes/coordinator.json'}`", text)
            self.assertIn("This is an absolute path.", text)


if __name__ == "__main__":
    unittest.main()
