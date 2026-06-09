"""Tests for ordered end-of-plan review stages."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Allow running standalone or via unittest discovery.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralpanda import agent


class DoneProc:
    """Minimal subprocess stand-in whose work is already complete."""

    def poll(self):
        return 0


class RunningProc:
    """Minimal subprocess stand-in whose work is still running."""

    def poll(self):
        return None


def _make_ralpanda_dir() -> Path:
    d = Path(tempfile.mkdtemp())
    ralpanda = d / ".ralpanda"
    for sub in ("logs", "outcomes", "sentinels"):
        (ralpanda / sub).mkdir(parents=True)
    (ralpanda / "history.jsonl").touch()
    return ralpanda


def _write_tasks(path: Path, tasks: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "tasks": tasks}, indent=2) + "\n")


def _write_outcome(
    path: Path,
    task_id: str,
    attempt: int,
    kind: str,
    namespace: str,
    status: str,
    *,
    summary: str = "done",
    payload: dict | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "task_id": task_id,
        "attempt": attempt,
        "agent": {
            "kind": kind,
            "namespace": namespace,
        },
        "status": status,
        "summary": summary,
        "payload": payload or {},
    }) + "\n")


def _write_check_outcome(
    ralpanda: Path,
    task_id: str,
    stage: str,
    check_name: str,
    status: str,
    *,
    summary: str = "check complete",
    payload: dict | None = None,
) -> None:
    namespace = agent.dag.review_agent_namespace("review", stage, check_name)
    path = agent.dag.outcome_path(ralpanda, task_id, 1, namespace)
    _write_outcome(
        path,
        task_id,
        1,
        "review_check",
        namespace,
        status,
        summary=summary,
        payload=payload,
    )


def _write_coordinator_outcome(
    ralpanda: Path,
    task_id: str,
    coordinator_attempt: int,
    status: str,
    *,
    summary: str = "coordinator complete",
    payload: dict | None = None,
) -> None:
    namespace = agent.dag.coordinator_agent_namespace(coordinator_attempt)
    path = agent.dag.outcome_path(ralpanda, task_id, 1, namespace)
    _write_outcome(
        path,
        task_id,
        1,
        "coordinator",
        namespace,
        status,
        summary=summary,
        payload=payload,
    )


def _review_task(check_stages: list[dict]) -> dict:
    return {
        "id": "ralpanda/test-plan/001",
        "title": "Review",
        "type": "review",
        "status": "running",
        "depends_on": [],
        "plan_source": ".ralpanda/plans/test-plan.md",
        "description": "Review the work.",
        "acceptance_criteria": [],
        "check_stages": check_stages,
        "outcome": None,
        "attempt": 1,
    }


def _fixup_task(task_id: str, depends_on: list[str] | None = None) -> dict:
    return {
        "id": task_id,
        "title": "Fix review failure",
        "type": "work",
        "status": "pending",
        "depends_on": depends_on or [],
        "plan_source": ".ralpanda/plans/test-plan.md",
        "description": "Fix the issue reported by the review.",
        "acceptance_criteria": ["Review passes"],
        "outcome": None,
        "attempt": 0,
        "created_at": "2026-01-01T00:00:00Z",
        "started_at": None,
        "completed_at": None,
    }


class TestReviewStages(unittest.TestCase):
    def test_failed_stage_skips_later_stages_and_launches_coordinator(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        history_file = ralpanda / "history.jsonl"
        stages = [
            {
                "name": "cheap",
                "checks": [{"name": "cheap-check", "prompt": "cheap", "mode": "parallel"}],
            },
            {
                "name": "expensive",
                "checks": [{"name": "expensive-check", "prompt": "expensive", "mode": "parallel"}],
            },
        ]
        _write_tasks(tasks_file, [_review_task(stages)])

        spawned: list[tuple[str, dict]] = []

        def fake_spawn(prompt_text, model, log_path, **kwargs):
            spawned.append((log_path.name, kwargs))
            if "coordinator" in log_path.name:
                return RunningProc()
            if "cheap-check" in log_path.name:
                _write_check_outcome(
                    ralpanda,
                    "ralpanda/test-plan/001",
                    "cheap",
                    "cheap-check",
                    "fail",
                    summary="Needs a fix.",
                    payload={"remediation": "Fix the cheap check."},
                )
            return DoneProc()

        with patch.object(agent, "spawn_agent", side_effect=fake_spawn), \
                patch.object(agent, "close_agent"):
            state = agent.start_review(ralpanda, tasks_file, "ralpanda/test-plan/001", "sonnet")
            done = agent.poll_review(state, ralpanda, tasks_file, "sonnet", history_file)

        self.assertFalse(done)
        self.assertEqual(state.phase, "coordinator")
        self.assertTrue(any("cheap-check" in name for name, _ in spawned))
        self.assertTrue(any("coordinator" in name for name, _ in spawned))
        self.assertFalse(any("expensive-check" in name for name, _ in spawned))
        coordinator_kwargs = next(
            kwargs for name, kwargs in spawned if "coordinator" in name
        )
        self.assertEqual(coordinator_kwargs["allowed_tools"], "Bash Write")
        self.assertEqual(
            coordinator_kwargs["max_turns"],
            agent.COORDINATOR_DEFAULT_MAX_TURNS,
        )
        self.assertNotIn("tools", coordinator_kwargs)
        self.assertEqual(
            [(r["name"], r["status"]) for r in state.check_results],
            [("cheap-check", "fail"), ("expensive-check", "skipped")],
        )
        self.assertEqual([c["name"] for c in state.failed_checks], ["cheap-check"])

    def test_malformed_coordinator_output_retries_without_cloning_review(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        history_file = ralpanda / "history.jsonl"
        check = {"name": "cheap-check", "prompt": "cheap", "mode": "parallel"}
        stages = [{"name": "cheap", "checks": [check]}]
        review = _review_task(stages)
        downstream = {
            "id": "ralpanda/test-plan/002",
            "title": "Downstream gate",
            "type": "delete_base_sha",
            "status": "pending",
            "depends_on": [review["id"]],
            "plan_source": review["plan_source"],
            "description": "Downstream task.",
            "acceptance_criteria": [],
            "outcome": None,
            "attempt": 0,
        }
        _write_tasks(tasks_file, [review, downstream])

        state = agent.ReviewState(task_id=review["id"], checks=[check])
        state.check_results = [{
            "name": "cheap-check",
            "stage": "cheap",
            "status": "fail",
            "detail": "Needs a fix.",
        }]
        state.failed_checks = [check]
        state.failed_analyses = ["Needs a fix."]
        state.coordinator_attempt = 1
        state.coordinator_max_attempts = 2

        spawned: list[tuple[str, dict]] = []

        def fake_spawn(prompt_text, model, log_path, **kwargs):
            spawned.append((log_path.name, kwargs))
            return RunningProc()

        with patch.object(agent, "spawn_agent", side_effect=fake_spawn):
            done = agent._process_coordinator_result(
                state, ralpanda, tasks_file, history_file, "sonnet",
            )

        self.assertFalse(done)
        self.assertEqual(state.coordinator_attempt, 2)
        self.assertIsInstance(state.coordinator_proc, RunningProc)

        tasks = json.loads(tasks_file.read_text())["tasks"]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[1]["depends_on"], [review["id"]])
        self.assertTrue(any("coordinator" in name for name, _ in spawned))
        self.assertIn("review_fixup_retry", history_file.read_text())

    def test_invalid_coordinator_fixup_graph_retries_without_inserting(self):
        check = {"name": "cheap-check", "prompt": "cheap", "mode": "parallel"}
        stages = [{"name": "cheap", "checks": [check]}]
        cases = [
            (
                "duplicate IDs",
                [
                    _fixup_task("ralpanda/test-plan/003"),
                    _fixup_task("ralpanda/test-plan/003"),
                ],
                "duplicate_ids",
            ),
            (
                "missing dependency",
                [_fixup_task("ralpanda/test-plan/003", ["missing"])],
                "missing_dependencies",
            ),
            (
                "internal cycle",
                [
                    _fixup_task("ralpanda/test-plan/003", ["ralpanda/test-plan/004"]),
                    _fixup_task("ralpanda/test-plan/004", ["ralpanda/test-plan/003"]),
                ],
                "cycle_detected",
            ),
            (
                "post-rewire cycle",
                [_fixup_task("ralpanda/test-plan/003", ["ralpanda/test-plan/002"])],
                "cycle_detected",
            ),
        ]

        for name, fixups, expected_error in cases:
            with self.subTest(name=name):
                ralpanda = _make_ralpanda_dir()
                tasks_file = ralpanda / "tasks.json"
                history_file = ralpanda / "history.jsonl"
                review = _review_task(stages)
                downstream = {
                    "id": "ralpanda/test-plan/002",
                    "title": "Downstream gate",
                    "type": "delete_base_sha",
                    "status": "pending",
                    "depends_on": [review["id"]],
                    "plan_source": review["plan_source"],
                    "description": "Downstream task.",
                    "acceptance_criteria": [],
                    "outcome": None,
                    "attempt": 0,
                }
                _write_tasks(tasks_file, [review, downstream])
                _write_coordinator_outcome(
                    ralpanda,
                    review["id"],
                    1,
                    "tasks_created",
                    payload={"tasks": fixups},
                )

                state = agent.ReviewState(task_id=review["id"], checks=[check])
                state.check_results = [{
                    "name": "cheap-check",
                    "stage": "cheap",
                    "status": "fail",
                    "detail": "Needs a fix.",
                }]
                state.failed_checks = [check]
                state.failed_analyses = ["Needs a fix."]
                state.coordinator_attempt = 1
                state.coordinator_max_attempts = 2

                spawned: list[tuple[str, dict]] = []

                def fake_spawn(prompt_text, model, log_path, **kwargs):
                    spawned.append((log_path.name, kwargs))
                    return RunningProc()

                with patch.object(agent, "spawn_agent", side_effect=fake_spawn):
                    done = agent._process_coordinator_result(
                        state, ralpanda, tasks_file, history_file, "sonnet",
                    )

                self.assertFalse(done)
                self.assertEqual(state.coordinator_attempt, 2)
                self.assertIsInstance(state.coordinator_proc, RunningProc)

                tasks = json.loads(tasks_file.read_text())["tasks"]
                self.assertEqual([t["id"] for t in tasks], [review["id"], downstream["id"]])
                self.assertEqual(tasks[1]["depends_on"], [review["id"]])
                self.assertTrue(any("coordinator" in item[0] for item in spawned))

                history = history_file.read_text()
                self.assertIn("review_fixup_retry", history)
                self.assertIn(expected_error, history)

    def test_exhausted_coordinator_retries_insert_pause_before_cloned_review(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        history_file = ralpanda / "history.jsonl"
        check = {"name": "cheap-check", "prompt": "cheap", "mode": "parallel"}
        stages = [{"name": "cheap", "checks": [check]}]
        review = _review_task(stages)
        downstream = {
            "id": "ralpanda/test-plan/002",
            "title": "Downstream gate",
            "type": "delete_base_sha",
            "status": "pending",
            "depends_on": [review["id"]],
            "plan_source": review["plan_source"],
            "description": "Downstream task.",
            "acceptance_criteria": [],
            "outcome": None,
            "attempt": 0,
        }
        _write_tasks(tasks_file, [review, downstream])

        state = agent.ReviewState(task_id=review["id"], checks=[check])
        state.check_results = [{
            "name": "cheap-check",
            "stage": "cheap",
            "status": "fail",
            "detail": "Needs a fix.",
        }]
        state.failed_checks = [check]
        state.failed_analyses = ["Needs a fix."]
        state.coordinator_attempt = 2
        state.coordinator_max_attempts = 2

        _write_coordinator_outcome(
            ralpanda,
            review["id"],
            2,
            "tasks_created",
            payload={"tasks": []},
        )

        done = agent._process_coordinator_result(
            state, ralpanda, tasks_file, history_file, "sonnet",
        )

        self.assertTrue(done)
        tasks = json.loads(tasks_file.read_text())["tasks"]
        old_review = next(t for t in tasks if t["id"] == review["id"])
        pause = next(t for t in tasks if t["type"] == "pause")
        clone = next(
            t for t in tasks
            if t["type"] == "review" and t["id"] != review["id"]
        )
        rewired_downstream = next(t for t in tasks if t["id"] == downstream["id"])

        self.assertEqual(pause["depends_on"], [])
        self.assertIn(pause["id"], clone["depends_on"])
        self.assertEqual(rewired_downstream["depends_on"], [clone["id"]])
        self.assertEqual(
            old_review["outcome"]["fixup_generation"]["status"], "failed",
        )

        history = history_file.read_text()
        self.assertIn("review_fixup_failed", history)
        self.assertIn("coordinator_failure_pause_inserted", history)

    def test_empty_fixup_insert_without_infra_does_not_clone_review(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        history_file = ralpanda / "history.jsonl"
        check = {"name": "cheap-check", "prompt": "cheap", "mode": "parallel"}
        stages = [{"name": "cheap", "checks": [check]}]
        review = _review_task(stages)
        downstream = {
            "id": "ralpanda/test-plan/002",
            "title": "Downstream gate",
            "type": "delete_base_sha",
            "status": "pending",
            "depends_on": [review["id"]],
            "plan_source": review["plan_source"],
            "description": "Downstream task.",
            "acceptance_criteria": [],
            "outcome": None,
            "attempt": 0,
        }
        _write_tasks(tasks_file, [review, downstream])

        state = agent.ReviewState(task_id=review["id"], checks=[check])
        agent._insert_fixups_and_clone(
            state, ralpanda, tasks_file, history_file, fixup_tasks=[],
        )

        tasks = json.loads(tasks_file.read_text())["tasks"]
        self.assertEqual([t["id"] for t in tasks], [review["id"], downstream["id"]])
        self.assertEqual(tasks[1]["depends_on"], [review["id"]])
        self.assertIn("fixup_insert_skipped", history_file.read_text())

    def test_fixup_tasks_depend_on_existing_pending_pause(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        history_file = ralpanda / "history.jsonl"
        check = {"name": "cheap-check", "prompt": "cheap", "mode": "parallel"}
        stages = [{"name": "cheap", "checks": [check]}]
        review = _review_task(stages)
        pause = {
            "id": "ralpanda/test-plan/002",
            "title": "Pause (global, inserted from TUI)",
            "type": "pause",
            "status": "pending",
            "depends_on": [],
            "plan_source": None,
            "description": "Global pause inserted from TUI.",
            "acceptance_criteria": [],
            "outcome": None,
            "attempt": 0,
        }
        downstream = {
            "id": "ralpanda/test-plan/003",
            "title": "Downstream gate",
            "type": "delete_base_sha",
            "status": "pending",
            "depends_on": [review["id"], pause["id"]],
            "plan_source": review["plan_source"],
            "description": "Downstream task.",
            "acceptance_criteria": [],
            "outcome": None,
            "attempt": 0,
        }
        fixup_id = "ralpanda/test-plan/004"
        _write_tasks(tasks_file, [review, pause, downstream])

        state = agent.ReviewState(task_id=review["id"], checks=[check])
        agent._insert_fixups_and_clone(
            state, ralpanda, tasks_file, history_file,
            fixup_tasks=[_fixup_task(fixup_id)],
        )

        tasks = json.loads(tasks_file.read_text())["tasks"]
        fixup = next(t for t in tasks if t["id"] == fixup_id)
        clone = next(
            t for t in tasks
            if t["type"] == "review" and t["id"] != review["id"]
        )
        rewired_downstream = next(t for t in tasks if t["id"] == downstream["id"])

        self.assertEqual(fixup["depends_on"], [pause["id"]])
        self.assertIn(pause["id"], clone["depends_on"])
        self.assertIn(fixup_id, clone["depends_on"])
        self.assertEqual(rewired_downstream["depends_on"], [clone["id"], pause["id"]])

    def test_later_stage_launches_only_after_earlier_stage_passes(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        history_file = ralpanda / "history.jsonl"
        stages = [
            {
                "name": "cheap",
                "checks": [{"name": "cheap-check", "prompt": "cheap", "mode": "parallel"}],
            },
            {
                "name": "expensive",
                "checks": [{"name": "expensive-check", "prompt": "expensive", "mode": "parallel"}],
            },
        ]
        _write_tasks(tasks_file, [_review_task(stages)])

        spawned: list[str] = []

        def fake_spawn(prompt_text, model, log_path, **kwargs):
            spawned.append(log_path.name)
            if "cheap-check" in log_path.name:
                _write_check_outcome(
                    ralpanda,
                    "ralpanda/test-plan/001",
                    "cheap",
                    "cheap-check",
                    "pass",
                )
            elif "expensive-check" in log_path.name:
                _write_check_outcome(
                    ralpanda,
                    "ralpanda/test-plan/001",
                    "expensive",
                    "expensive-check",
                    "pass",
                )
            return DoneProc()

        with patch.object(agent, "spawn_agent", side_effect=fake_spawn), \
                patch.object(agent, "close_agent"):
            state = agent.start_review(ralpanda, tasks_file, "ralpanda/test-plan/001", "sonnet")
            done = agent.poll_review(state, ralpanda, tasks_file, "sonnet", history_file)

        self.assertFalse(done)
        self.assertEqual(state.current_stage_idx, 1)
        self.assertEqual(state.phase, "parallel")
        self.assertTrue(any("cheap-check" in name for name in spawned))
        self.assertTrue(any("expensive-check" in name for name in spawned))
        self.assertFalse(any("coordinator" in name for name in spawned))
        self.assertEqual(
            [(r["name"], r["status"]) for r in state.check_results],
            [("cheap-check", "pass")],
        )

    def test_cloned_review_preserves_check_stages(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        history_file = ralpanda / "history.jsonl"
        stages = [
            {
                "name": "cheap",
                "checks": [{"name": "cheap-check", "prompt": "cheap", "mode": "parallel"}],
            },
            {
                "name": "expensive",
                "checks": [{"name": "expensive-check", "prompt": "expensive", "mode": "isolated"}],
            },
        ]
        review = _review_task(stages)
        delete_gate = {
            "id": "ralpanda/test-plan/002",
            "title": "Delete base SHA",
            "type": "delete_base_sha",
            "status": "pending",
            "depends_on": [review["id"]],
            "plan_source": review["plan_source"],
            "description": "Remove the diff baseline.",
            "acceptance_criteria": [],
            "outcome": None,
            "attempt": 0,
        }
        _write_tasks(tasks_file, [review, delete_gate])

        checks = [
            {"name": "cheap-check", "prompt": "cheap", "mode": "parallel", "stage": "cheap"},
            {"name": "expensive-check", "prompt": "expensive", "mode": "isolated", "stage": "expensive"},
        ]
        state = agent.ReviewState(
            task_id=review["id"],
            checks=checks,
            stages=[
                {"name": "cheap", "check_indices": [0]},
                {"name": "expensive", "check_indices": [1]},
            ],
        )

        agent._insert_fixups_and_clone(
            state, ralpanda, tasks_file, history_file,
            fixup_tasks=[_fixup_task("ralpanda/test-plan/003")],
        )

        tasks = json.loads(tasks_file.read_text())["tasks"]
        clone = next(t for t in tasks if t["type"] == "review" and t["id"] != review["id"])
        rewired_gate = next(t for t in tasks if t["id"] == delete_gate["id"])
        self.assertEqual(clone["check_stages"], stages)
        self.assertEqual(rewired_gate["depends_on"], [clone["id"]])


if __name__ == "__main__":
    unittest.main()
