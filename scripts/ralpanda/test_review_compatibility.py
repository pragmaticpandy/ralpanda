"""Tests for pre-plan review compatibility gates."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

# Allow running standalone or via unittest discovery.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralpanda import agent


def _make_ralpanda_dir() -> Path:
    d = Path(tempfile.mkdtemp())
    ralpanda = d / ".ralpanda"
    for sub in ("logs", "outcomes", "sentinels"):
        (ralpanda / sub).mkdir(parents=True)
    (ralpanda / "history.jsonl").touch()
    return ralpanda


def _write_tasks(path: Path, tasks: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "tasks": tasks}, indent=2) + "\n")


class TestReviewCompatibilityBlocker(unittest.TestCase):
    def test_failure_inserts_pause_and_cloned_gate(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        history_file = ralpanda / "history.jsonl"
        plan_source = ".ralpanda/plans/test-plan.md"
        compat_id = "ralpanda/test-plan/001"
        work_id = "ralpanda/test-plan/002"
        review_id = "ralpanda/test-plan/003"
        check = {
            "name": "compat-pattern-y",
            "prompt": "Check whether pattern Y remains applicable.",
            "mode": "parallel",
            "for_review_check": "pattern-y",
            "review_prompt": "Verify that situation X uses pattern Y.",
        }

        _write_tasks(tasks_file, [
            {
                "id": compat_id,
                "title": "Check review compatibility with plan",
                "type": "review_compatibility",
                "status": "running",
                "depends_on": [],
                "plan_source": plan_source,
                "description": "Check review assumptions before work starts.",
                "acceptance_criteria": ["Review compatibility checks pass"],
                "checks": [check],
                "target_review_task_id": review_id,
                "outcome": None,
                "attempt": 1,
            },
            {
                "id": work_id,
                "title": "Implement the plan",
                "type": "work",
                "status": "pending",
                "depends_on": [compat_id],
                "plan_source": plan_source,
                "description": "Do the work.",
                "acceptance_criteria": [],
                "outcome": None,
                "attempt": 0,
            },
            {
                "id": review_id,
                "title": "Review",
                "type": "review",
                "status": "pending",
                "depends_on": [work_id],
                "plan_source": plan_source,
                "description": "Review the work.",
                "acceptance_criteria": [],
                "checks": [],
                "outcome": None,
                "attempt": 0,
            },
        ])

        state = agent.ReviewState(
            task_id=compat_id,
            checks=[check],
            task_type="review_compatibility",
            plan_source=plan_source,
        )
        state.check_results = [{
            "name": "compat-pattern-y",
            "status": "fail",
            "detail": "Pattern Y is incompatible with the plan.",
        }]
        state.failed_checks = [check]
        state.failed_analyses = ["Pattern Y is incompatible with the plan."]

        agent._finalize_review_compatibility_blocker(
            state, ralpanda, tasks_file, history_file,
        )

        data = json.loads(tasks_file.read_text())
        tasks = data["tasks"]
        old_gate = next(t for t in tasks if t["id"] == compat_id)
        pause = next(t for t in tasks if t["type"] == "pause")
        clone = next(
            t for t in tasks
            if t["type"] == "review_compatibility" and t["id"] != compat_id
        )
        work = next(t for t in tasks if t["id"] == work_id)

        self.assertIn("Review compatibility blocked", old_gate["outcome"]["summary"])
        self.assertIn("fix the plan or review checks", pause["pause_reason"].lower())
        self.assertIn(pause["id"], clone["depends_on"])
        self.assertEqual(clone["checks"], [check])
        self.assertEqual(work["depends_on"], [clone["id"]])
        self.assertEqual(clone["target_review_task_id"], review_id)


if __name__ == "__main__":
    unittest.main()
