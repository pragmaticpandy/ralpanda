"""Tests for attempt-scoped outcome-file processing."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Allow running standalone or via unittest discover
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralpanda import agent, dag
from ralpanda.__main__ import LoopState, validate_startup


def _make_ralpanda_dir() -> Path:
    root = Path(tempfile.mkdtemp())
    ralpanda = root / ".ralpanda"
    for sub in ("logs", "outcomes", "running", "sentinels"):
        (ralpanda / sub).mkdir(parents=True)
    (ralpanda / "plans").mkdir()
    (ralpanda / "history.jsonl").touch()
    (ralpanda / "plans" / "test.md").write_text("# Test plan\n")
    return ralpanda


def _write_tasks(tasks_file: Path, tasks: list[dict]) -> None:
    tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}, indent=2) + "\n")


def _work_task(*, attempt: int = 1) -> dict:
    return {
        "id": "ralpanda/test/001",
        "title": "Do work",
        "type": "work",
        "status": "running",
        "depends_on": [],
        "plan_source": ".ralpanda/plans/test.md",
        "description": "Do the work.",
        "acceptance_criteria": [],
        "outcome": None,
        "attempt": attempt,
        "created_at": "2026-01-01T00:00:00Z",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": None,
    }


def _write_work_outcome(
    ralpanda: Path,
    task_id: str,
    attempt: int,
    status: str,
) -> None:
    namespace = dag.work_agent_namespace()
    path = dag.outcome_path(ralpanda, task_id, attempt, namespace)
    dag.atomic_write_json(path, {
        "schema_version": 1,
        "task_id": task_id,
        "attempt": attempt,
        "agent": {
            "kind": "work",
            "namespace": namespace,
        },
        "status": status,
        "summary": "Work finished.",
        "payload": {
            "files_changed": ["src/example.py"],
            "decisions": [],
        },
    })


class TestWorkOutcomes(unittest.TestCase):
    def test_clean_exit_without_outcome_retries_instead_of_succeeding(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        history_file = ralpanda / "history.jsonl"
        _write_tasks(tasks_file, [_work_task(attempt=1)])

        with patch.object(agent.git, "commit_task", return_value=None):
            finished = agent.process_work_result(
                ralpanda,
                tasks_file,
                "ralpanda/test/001",
                0,
                3,
                history_file,
            )

        data = json.loads(tasks_file.read_text())
        self.assertIsNone(finished)
        self.assertEqual(data["tasks"][0]["status"], "pending")
        self.assertIn("outcome_error=missing outcome file", history_file.read_text())

    def test_valid_outcome_is_authoritative_after_nonzero_exit(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        history_file = ralpanda / "history.jsonl"
        task_id = "ralpanda/test/001"
        task = _work_task(attempt=1)
        task["plan_source"] = None
        _write_tasks(tasks_file, [task])
        _write_work_outcome(ralpanda, task_id, 1, "done")

        with patch.object(agent.git, "commit_task", return_value=None):
            finished = agent.process_work_result(
                ralpanda,
                tasks_file,
                task_id,
                7,
                3,
                history_file,
            )

        data = json.loads(tasks_file.read_text())
        self.assertEqual(data["tasks"][0]["status"], "done")
        self.assertEqual(data["tasks"][0]["outcome"]["status"], "done")
        self.assertEqual(finished, {"commit_sha": None})
        self.assertIn("agent_nonzero_after_valid_outcome", history_file.read_text())

    def test_startup_recovers_running_work_from_valid_outcome(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        history_file = ralpanda / "history.jsonl"
        task_id = "ralpanda/test/001"
        task = _work_task(attempt=1)
        task["plan_source"] = None
        _write_tasks(tasks_file, [task])
        _write_work_outcome(ralpanda, task_id, 1, "done")
        loop_state = LoopState(
            ralpanda_dir=ralpanda,
            tasks_file=tasks_file,
            history_file=history_file,
            config={"model": "sonnet", "max_attempts_per_task": 3},
        )

        with patch.object(agent.git, "commit_task", return_value=None):
            error = validate_startup(loop_state)

        data = json.loads(tasks_file.read_text())
        self.assertIsNone(error)
        self.assertEqual(data["tasks"][0]["status"], "done")
        self.assertIn("startup_recovered_running_task", history_file.read_text())


if __name__ == "__main__":
    unittest.main()
