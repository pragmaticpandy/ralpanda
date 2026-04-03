"""Tests for dag.py — run with: python -m pytest ralpanda/test_dag.py or python ralpanda/test_dag.py"""

import json
import os
import tempfile
import unittest
from pathlib import Path

# Allow running standalone or via pytest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralpanda import dag


def _make_tasks_file(tasks: list[dict]) -> Path:
    """Create a temp tasks.json file with the given tasks."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump({"version": 1, "tasks": tasks}, f, indent=2)
    f.close()
    return Path(f.name)


class TestGetNextTask(unittest.TestCase):
    def test_simple_chain(self):
        tasks = [
            {"id": "a", "status": "done", "depends_on": []},
            {"id": "b", "status": "pending", "depends_on": ["a"]},
            {"id": "c", "status": "pending", "depends_on": ["b"]},
        ]
        self.assertEqual(dag.get_next_task(tasks)["id"], "b")

    def test_no_runnable(self):
        tasks = [
            {"id": "a", "status": "pending", "depends_on": ["b"]},
            {"id": "b", "status": "pending", "depends_on": ["a"]},
        ]
        self.assertIsNone(dag.get_next_task(tasks))

    def test_split_satisfies(self):
        tasks = [
            {"id": "a", "status": "split", "depends_on": []},
            {"id": "b", "status": "pending", "depends_on": ["a"]},
        ]
        self.assertEqual(dag.get_next_task(tasks)["id"], "b")

    def test_all_done(self):
        tasks = [{"id": "a", "status": "done", "depends_on": []}]
        self.assertIsNone(dag.get_next_task(tasks))


class TestValidation(unittest.TestCase):
    def test_valid(self):
        tasks = [
            {"id": "a", "status": "pending", "depends_on": []},
            {"id": "b", "status": "pending", "depends_on": ["a"]},
        ]
        self.assertEqual(dag.validate_tasks(tasks), "valid")

    def test_cycle(self):
        tasks = [
            {"id": "a", "status": "pending", "depends_on": ["b"]},
            {"id": "b", "status": "pending", "depends_on": ["a"]},
        ]
        self.assertEqual(dag.validate_tasks(tasks), "cycle_detected")

    def test_duplicate_ids(self):
        tasks = [
            {"id": "a", "status": "pending", "depends_on": []},
            {"id": "a", "status": "pending", "depends_on": []},
        ]
        self.assertIn("duplicate_ids", dag.validate_tasks(tasks))

    def test_self_cycle(self):
        tasks = [{"id": "a", "status": "pending", "depends_on": ["a"]}]
        self.assertEqual(dag.validate_tasks(tasks), "cycle_detected")


class TestTaskCounts(unittest.TestCase):
    def test_counts(self):
        tasks = [
            {"id": "a", "status": "done"},
            {"id": "b", "status": "done"},
            {"id": "c", "status": "pending"},
            {"id": "d", "status": "failed"},
        ]
        counts = dag.task_counts(tasks)
        self.assertEqual(counts, {"done": 2, "pending": 1, "failed": 1})


class TestAllDone(unittest.TestCase):
    def test_all_done(self):
        tasks = [
            {"id": "a", "status": "done"},
            {"id": "b", "status": "split"},
        ]
        self.assertTrue(dag.all_done(tasks))

    def test_not_done(self):
        tasks = [
            {"id": "a", "status": "done"},
            {"id": "b", "status": "pending"},
        ]
        self.assertFalse(dag.all_done(tasks))


class TestNextTaskId(unittest.TestCase):
    def test_first(self):
        tasks = []
        self.assertEqual(dag.next_task_id(tasks, "test"), "ralpanda/test/001")

    def test_increment(self):
        tasks = [
            {"id": "ralpanda/test/001"},
            {"id": "ralpanda/test/005"},
        ]
        self.assertEqual(dag.next_task_id(tasks, "test"), "ralpanda/test/006")

    def test_different_slugs_globally_unique(self):
        tasks = [
            {"id": "ralpanda/foo/010"},
            {"id": "ralpanda/bar/003"},
        ]
        # Global max is 010, so both slugs get 011+
        self.assertEqual(dag.next_task_id(tasks, "foo"), "ralpanda/foo/011")
        self.assertEqual(dag.next_task_id(tasks, "bar"), "ralpanda/bar/011")


class TestLockedTasks(unittest.TestCase):
    def test_read_modify_write(self):
        path = _make_tasks_file([{"id": "a", "status": "pending", "depends_on": []}])
        try:
            with dag.locked_tasks(path) as data:
                data["tasks"][0]["status"] = "done"
            # Verify it was written
            with open(path) as f:
                result = json.load(f)
            self.assertEqual(result["tasks"][0]["status"], "done")
        finally:
            os.unlink(path)

    def test_readonly(self):
        path = _make_tasks_file([{"id": "a", "status": "pending", "depends_on": []}])
        try:
            with dag.locked_tasks_readonly(path) as data:
                data["tasks"][0]["status"] = "done"  # modify in memory
            # Verify it was NOT written back
            with open(path) as f:
                result = json.load(f)
            self.assertEqual(result["tasks"][0]["status"], "pending")
        finally:
            os.unlink(path)


class TestUpdateTaskStatus(unittest.TestCase):
    def test_update_to_running(self):
        path = _make_tasks_file([{"id": "a", "status": "pending", "depends_on": []}])
        try:
            dag.update_task_status(path, "a", "running")
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data["tasks"][0]["status"], "running")
            self.assertIsNotNone(data["tasks"][0].get("started_at"))
        finally:
            os.unlink(path)

    def test_update_to_done(self):
        path = _make_tasks_file([{"id": "a", "status": "running", "depends_on": []}])
        try:
            dag.update_task_status(path, "a", "done")
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data["tasks"][0]["status"], "done")
            self.assertIsNotNone(data["tasks"][0].get("completed_at"))
        finally:
            os.unlink(path)


class TestInsertPauseBefore(unittest.TestCase):
    def test_basic_insert(self):
        path = _make_tasks_file([
            {"id": "a", "status": "done", "depends_on": [], "type": "work", "plan_source": ".ralpanda/plans/test.md"},
            {"id": "b", "status": "pending", "depends_on": ["a"], "type": "work", "plan_source": ".ralpanda/plans/test.md"},
        ])
        try:
            pause_id = dag.insert_pause_before(path, "b")
            with open(path) as f:
                data = json.load(f)
            tasks = data["tasks"]
            # Should have 3 tasks: a, pause, b
            self.assertEqual(len(tasks), 3)
            self.assertEqual(tasks[1]["type"], "pause")
            # b should depend on pause
            self.assertIn(pause_id, tasks[2]["depends_on"])
            # pause should depend on a (inherited from b's deps)
            self.assertIn("a", tasks[1]["depends_on"])
        finally:
            os.unlink(path)

    def test_no_duplicate_pause(self):
        """Pressing p twice on the same task should not insert a second pause."""
        path = _make_tasks_file([
            {"id": "a", "status": "done", "depends_on": [], "type": "work", "plan_source": ".ralpanda/plans/test.md"},
            {"id": "b", "status": "pending", "depends_on": ["a"], "type": "work", "plan_source": ".ralpanda/plans/test.md"},
        ])
        try:
            first = dag.insert_pause_before(path, "b")
            self.assertIsNotNone(first)
            second = dag.insert_pause_before(path, "b")
            self.assertIsNone(second)  # Should no-op
            with open(path) as f:
                data = json.load(f)
            # Still only 3 tasks
            self.assertEqual(len(data["tasks"]), 3)
        finally:
            os.unlink(path)

    def test_no_duplicate_deps(self):
        """Target task's deps should not have duplicates after pause insertion."""
        path = _make_tasks_file([
            {"id": "a", "status": "done", "depends_on": [], "type": "work", "plan_source": ".ralpanda/plans/test.md"},
            {"id": "b", "status": "pending", "depends_on": ["a"], "type": "work", "plan_source": ".ralpanda/plans/test.md"},
        ])
        try:
            dag.insert_pause_before(path, "b")
            with open(path) as f:
                data = json.load(f)
            b_deps = data["tasks"][2]["depends_on"]
            self.assertEqual(len(b_deps), len(set(b_deps)), f"Duplicate deps: {b_deps}")
        finally:
            os.unlink(path)


class TestInsertGlobalPause(unittest.TestCase):
    def test_basic_insert(self):
        path = _make_tasks_file([
            {"id": "a", "status": "done", "depends_on": [], "type": "work"},
            {"id": "b", "status": "pending", "depends_on": ["a"], "type": "work"},
            {"id": "c", "status": "pending", "depends_on": ["b"], "type": "review"},
        ])
        try:
            pause_id = dag.insert_global_pause(path)
            with open(path) as f:
                data = json.load(f)
            # b and c should depend on pause, but only non-pause pending tasks
            b = next(t for t in data["tasks"] if t["id"] == "b")
            c = next(t for t in data["tasks"] if t["id"] == "c")
            self.assertIn(pause_id, b["depends_on"])
            self.assertIn(pause_id, c["depends_on"])
        finally:
            os.unlink(path)

    def test_no_duplicate_global_pause(self):
        """Pressing p (global) twice should not insert a second global pause."""
        path = _make_tasks_file([
            {"id": "a", "status": "done", "depends_on": [], "type": "work"},
            {"id": "b", "status": "pending", "depends_on": ["a"], "type": "work"},
        ])
        try:
            first = dag.insert_global_pause(path)
            self.assertIsNotNone(first)
            second = dag.insert_global_pause(path)
            self.assertIsNone(second)  # Should no-op
        finally:
            os.unlink(path)

    def test_does_not_chain_pauses(self):
        """Global pause should NOT add itself as a dep to other pending pause tasks."""
        path = _make_tasks_file([
            {"id": "a", "status": "done", "depends_on": [], "type": "work"},
            {"id": "b", "status": "pending", "depends_on": ["a"], "type": "pause"},
            {"id": "c", "status": "pending", "depends_on": ["b"], "type": "work"},
        ])
        try:
            pause_id = dag.insert_global_pause(path)
            with open(path) as f:
                data = json.load(f)
            # The existing pause task 'b' should NOT depend on the new global pause
            b = next(t for t in data["tasks"] if t["id"] == "b")
            self.assertNotIn(pause_id, b["depends_on"])
            # But work task 'c' should
            c = next(t for t in data["tasks"] if t["id"] == "c")
            self.assertIn(pause_id, c["depends_on"])
        finally:
            os.unlink(path)


class TestPlanSlug(unittest.TestCase):
    def test_from_path(self):
        self.assertEqual(dag.plan_slug_from_source(".ralpanda/plans/add-auth.md"), "add-auth")

    def test_none(self):
        self.assertEqual(dag.plan_slug_from_source(None), "_gate")

    def test_empty(self):
        self.assertEqual(dag.plan_slug_from_source(""), "_gate")


class TestRewireDeps(unittest.TestCase):
    def test_rewire(self):
        path = _make_tasks_file([
            {"id": "a", "status": "split", "depends_on": []},
            {"id": "a1", "status": "pending", "depends_on": []},
            {"id": "a2", "status": "pending", "depends_on": []},
            {"id": "b", "status": "pending", "depends_on": ["a"]},
        ])
        try:
            dag.rewire_deps(path, "a", ["a1", "a2"])
            with open(path) as f:
                data = json.load(f)
            b = next(t for t in data["tasks"] if t["id"] == "b")
            self.assertIn("a1", b["depends_on"])
            self.assertIn("a2", b["depends_on"])
            self.assertNotIn("a", b["depends_on"])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
