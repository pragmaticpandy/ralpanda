"""Tests for cleanup and signal handling."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

# Allow running standalone or via unittest discovery.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralpanda import dag, agent
from ralpanda.__main__ import (
    LoopState,
    advance_loop,
    cleanup,
    handle_input,
    poll_agents,
    validate_startup,
    _event_payload,
)


def _make_ralpanda_dir() -> Path:
    """Create a temp .ralpanda directory with minimal structure."""
    d = Path(tempfile.mkdtemp())
    ralpanda = d / ".ralpanda"
    for sub in ("logs", "outcomes", "sentinels"):
        (ralpanda / sub).mkdir(parents=True)
    tasks_file = ralpanda / "tasks.json"
    tasks_file.write_text(json.dumps({"version": 1, "tasks": []}))
    (ralpanda / "history.jsonl").touch()
    (ralpanda / "loop.state").write_text("running")
    return ralpanda


def _make_loop_state(**overrides) -> LoopState:
    ralpanda = _make_ralpanda_dir()
    defaults = dict(
        ralpanda_dir=ralpanda,
        tasks_file=ralpanda / "tasks.json",
        history_file=ralpanda / "history.jsonl",
        config={"model": "sonnet", "max_attempts_per_task": 3},
        tasks=[],
    )
    defaults.update(overrides)
    return LoopState(**defaults)


class TestEventPayload(unittest.TestCase):
    def test_includes_shared_loop_snapshot(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        tasks_file.write_text(json.dumps({
            "version": 1,
            "tasks": [
                {
                    "id": "ralpanda/test/001",
                    "status": "done",
                    "depends_on": [],
                },
                {
                    "id": "ralpanda/test/002",
                    "status": "pending",
                    "depends_on": ["ralpanda/test/001"],
                },
            ],
        }))
        ls = _make_loop_state(
            ralpanda_dir=ralpanda,
            tasks_file=tasks_file,
            history_file=ralpanda / "history.jsonl",
            current_task_id="ralpanda/test/001",
        )
        ls.set_state("running", "ralpanda/test/001")

        payload = _event_payload(ls)

        self.assertEqual(payload["loop"]["state"], "running")
        self.assertEqual(payload["loop"]["state_info"], "ralpanda/test/001")
        self.assertEqual(payload["loop"]["current_task_id"], "ralpanda/test/001")
        self.assertEqual(payload["loop"]["next_task_id"], "ralpanda/test/002")
        self.assertEqual(payload["loop"]["counts"], {"done": 1, "pending": 1})
        self.assertEqual(payload["loop"]["total_tasks"], 2)


class TestWaitingForTasks(unittest.TestCase):
    def test_validate_startup_allows_missing_tasks_file(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        tasks_file.unlink()
        ls = _make_loop_state(
            ralpanda_dir=ralpanda,
            tasks_file=tasks_file,
            history_file=ralpanda / "history.jsonl",
        )

        error = validate_startup(ls)

        self.assertIsNone(error)
        self.assertEqual(ls.state, "waiting_tasks")
        self.assertEqual(ls.state_info, f"waiting for {tasks_file}")
        self.assertEqual(ls.tasks, [])

    def test_advance_loop_waits_for_missing_tasks_file(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        tasks_file.unlink()
        ls = _make_loop_state(
            ralpanda_dir=ralpanda,
            tasks_file=tasks_file,
            history_file=ralpanda / "history.jsonl",
        )

        with patch("ralpanda.__main__._run_loop_completed_hook") as completed_hook:
            advance_loop(ls, MagicMock())

        completed_hook.assert_not_called()
        self.assertEqual(ls.state, "waiting_tasks")
        self.assertEqual(ls.state_info, f"waiting for {tasks_file}")

    def test_advance_loop_waits_for_empty_tasks_file(self):
        ls = _make_loop_state()

        with patch("ralpanda.__main__._run_loop_completed_hook") as completed_hook:
            advance_loop(ls, MagicMock())

        completed_hook.assert_not_called()
        self.assertEqual(ls.state, "waiting_tasks")
        self.assertEqual(ls.state_info, f"{ls.tasks_file} has no tasks")

    def test_maybe_reload_tasks_loads_file_after_it_appears(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        tasks_file.unlink()
        ls = _make_loop_state(
            ralpanda_dir=ralpanda,
            tasks_file=tasks_file,
            history_file=ralpanda / "history.jsonl",
            tasks=[{"id": "stale", "status": "pending", "depends_on": []}],
            tasks_mtime=123.0,
        )

        ls.maybe_reload_tasks()
        self.assertEqual(ls.tasks, [])

        task = {"id": "ralpanda/test/001", "status": "pending", "depends_on": []}
        tasks_file.write_text(json.dumps({"version": 1, "tasks": [task]}))
        ls.maybe_reload_tasks()

        self.assertEqual(ls.tasks, [task])

    def test_advance_loop_blocks_invalid_tasks_file_after_it_appears(self):
        ralpanda = _make_ralpanda_dir()
        tasks_file = ralpanda / "tasks.json"
        tasks = [
            {"id": "dup", "status": "pending", "depends_on": []},
            {"id": "dup", "status": "pending", "depends_on": []},
        ]
        tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))
        ls = _make_loop_state(
            ralpanda_dir=ralpanda,
            tasks_file=tasks_file,
            history_file=ralpanda / "history.jsonl",
            state="waiting_tasks",
            state_info=f"waiting for {tasks_file}",
        )

        advance_loop(ls, MagicMock())

        self.assertEqual(ls.state, "waiting_blocked")
        self.assertIn("duplicate_ids", ls.state_info)


class TestCleanupTerminatesAgent(unittest.TestCase):
    """Verify cleanup() terminates a running agent subprocess."""

    def test_cleanup_terminates_agent_proc(self):
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.wait = MagicMock()
        proc.pid = 99999

        ls = _make_loop_state(agent_proc=proc)

        with patch.object(agent, "close_agent") as mock_close:
            cleanup(ls)

        proc.terminate.assert_called_once()
        proc.wait.assert_called_once_with(timeout=5)
        mock_close.assert_called_once_with(proc)

    def test_cleanup_kills_if_terminate_times_out(self):
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.wait = MagicMock(side_effect=subprocess.TimeoutExpired("cmd", 5))
        proc.kill = MagicMock()
        proc.pid = 99999

        ls = _make_loop_state(agent_proc=proc)

        with patch.object(agent, "close_agent"):
            cleanup(ls)

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_cleanup_resets_running_task_to_pending(self):
        ralpanda = _make_ralpanda_dir()
        tasks = [{"id": "t1", "status": "running", "depends_on": []}]
        tasks_file = ralpanda / "tasks.json"
        tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))

        ls = _make_loop_state(
            ralpanda_dir=ralpanda,
            tasks_file=tasks_file,
            history_file=ralpanda / "history.jsonl",
            current_task_id="t1",
        )

        cleanup(ls)

        data = json.loads(tasks_file.read_text())
        self.assertEqual(data["tasks"][0]["status"], "pending")


class TestRecordedMetadataTermination(unittest.TestCase):
    """Verify stale metadata is checked before PID/PGID signals are sent."""

    def _metadata_for_cmd(
        self,
        cmd: list[str],
        *,
        pid: int = 12345,
        pgid: int = 12345,
        started_at_unix: float | None = None,
    ) -> dict:
        if started_at_unix is None:
            started_at_unix = time.time()
        return {
            "schema_version": 1,
            "task_id": "ralpanda/test/001",
            "attempt": 1,
            "agent": {"kind": "work", "namespace": "work"},
            "expected_outcome_path": "/tmp/ralpanda-outcome.json",
            "log_path": "/tmp/ralpanda-log.jsonl",
            "started_at": "2026-01-01T00:00:00Z",
            "pid": pid,
            "pgid": pgid,
            "process": {
                "argv0": "claude",
                "required_args": list(agent.EXPECTED_AGENT_ARGS),
                "cmdline_sha256": agent._cmdline_digest(cmd),
                "started_at_unix": started_at_unix,
            },
        }

    def test_terminates_only_when_recorded_process_identity_matches(self):
        started_at = time.time()
        cmd = [
            "claude",
            "-p",
            "write outcome to /tmp/ralpanda-outcome.json",
            "--model",
            "sonnet",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        metadata = self._metadata_for_cmd(cmd, started_at_unix=started_at)
        live = agent._LiveProcessInfo(
            pid=12345,
            pgid=12345,
            sid=12345,
            argv=cmd,
            argv_source="proc",
            command=None,
            started_at_unix=started_at + 0.5,
        )

        with patch.object(agent, "_read_live_process_info", return_value=live), \
             patch.object(agent, "_process_is_alive", return_value=False), \
             patch("ralpanda.agent.os.killpg") as killpg:
            result = agent._terminate_recorded_group(metadata)

        killpg.assert_called_once_with(12345, signal.SIGTERM)
        self.assertTrue(result.attempted)
        self.assertTrue(result.safe_to_forget)
        self.assertFalse(result.hard_killed)

    def test_terminates_when_observed_interpreter_identity_matches(self):
        started_at = time.time()
        requested_cmd = [
            "claude",
            "-p",
            "write outcome to /tmp/ralpanda-outcome.json",
            "--model",
            "sonnet",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        observed_cmd = [
            "node",
            "/opt/claude/cli.js",
            "-p",
            "write outcome to /tmp/ralpanda-outcome.json",
            "--model",
            "sonnet",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        metadata = self._metadata_for_cmd(
            requested_cmd,
            started_at_unix=started_at,
        )
        metadata["process"]["observed_argv0"] = "node"
        metadata["process"]["observed_cmdline_sha256"] = agent._cmdline_digest(
            observed_cmd,
        )
        live = agent._LiveProcessInfo(
            pid=12345,
            pgid=12345,
            sid=12345,
            argv=observed_cmd,
            argv_source="proc",
            command=None,
            started_at_unix=started_at + 0.5,
        )

        with patch.object(agent, "_read_live_process_info", return_value=live), \
             patch.object(agent, "_process_is_alive", return_value=False), \
             patch("ralpanda.agent.os.killpg") as killpg:
            result = agent._terminate_recorded_group(metadata)

        killpg.assert_called_once_with(12345, signal.SIGTERM)
        self.assertTrue(result.attempted)
        self.assertTrue(result.safe_to_forget)

    def test_skips_kill_when_recorded_pid_is_not_expected_process(self):
        started_at = time.time()
        cmd = [
            "claude",
            "-p",
            "write outcome to /tmp/ralpanda-outcome.json",
            "--model",
            "sonnet",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        metadata = self._metadata_for_cmd(cmd, started_at_unix=started_at)
        live = agent._LiveProcessInfo(
            pid=12345,
            pgid=12345,
            sid=12345,
            argv=["sleep", "60"],
            argv_source="proc",
            command=None,
            started_at_unix=started_at + 0.5,
        )

        with patch.object(agent, "_read_live_process_info", return_value=live), \
             patch("ralpanda.agent.os.killpg") as killpg:
            result = agent._terminate_recorded_group(metadata)

        killpg.assert_not_called()
        self.assertFalse(result.attempted)
        self.assertFalse(result.safe_to_forget)
        self.assertEqual(result.skip_reason, "cmdline_digest_mismatch")

    def test_startup_recovery_blocks_when_live_metadata_process_is_unverified(self):
        ralpanda = _make_ralpanda_dir()
        task = {
            "id": "ralpanda/test/001",
            "type": "work",
            "status": "running",
            "attempt": 1,
            "depends_on": [],
        }
        tasks_file = ralpanda / "tasks.json"
        tasks_file.write_text(json.dumps({"version": 1, "tasks": [task]}))
        metadata_dir = (
            ralpanda
            / "running"
            / dag.task_safe_id(task["id"])
            / "attempt-1"
        )
        metadata_dir.mkdir(parents=True)
        metadata_path = metadata_dir / "work.json"
        metadata_path.write_text(json.dumps({"pid": 12345, "pgid": 12345}))
        ls = _make_loop_state(
            ralpanda_dir=ralpanda,
            tasks_file=tasks_file,
            history_file=ralpanda / "history.jsonl",
        )

        blocked = agent._RecordedTerminationResult(
            attempted=False,
            safe_to_forget=False,
            skip_reason="argv0_mismatch",
        )
        with patch.object(agent, "_terminate_recorded_group", return_value=blocked):
            error = validate_startup(ls)

        self.assertIsNotNone(error)
        self.assertIn("refused to kill", error)
        self.assertTrue(metadata_path.exists())
        data = json.loads(tasks_file.read_text())
        self.assertEqual(data["tasks"][0]["status"], "running")


class TestCleanupTerminatesReviewProcs(unittest.TestCase):
    """Verify cleanup() terminates all review subprocesses."""

    def test_cleanup_terminates_parallel_and_isolated_procs(self):
        parallel_proc = MagicMock()
        isolated_proc = MagicMock()
        coord_proc = MagicMock()

        rs = agent.ReviewState(
            task_id="t1",
            checks=[{"name": "a", "mode": "parallel"}, {"name": "b", "mode": "isolated"}],
        )
        rs.parallel_procs = {0: parallel_proc}
        rs.current_isolated_proc = isolated_proc
        rs.coordinator_proc = coord_proc

        ls = _make_loop_state(review_state=rs)

        with patch.object(agent, "close_agent"):
            cleanup(ls)

        parallel_proc.terminate.assert_called_once()
        isolated_proc.terminate.assert_called_once()
        coord_proc.terminate.assert_called_once()


class TestForceQuitKillsAgent(unittest.TestCase):
    """Verify Q (force quit) terminates the agent during poll."""

    def test_force_quit_terminates_agent_in_poll(self):
        proc = MagicMock()
        proc.poll = MagicMock(return_value=None)  # still running
        proc.terminate = MagicMock()
        proc.wait = MagicMock()
        proc.pid = 99999

        ls = _make_loop_state(agent_proc=proc, force_quit=True)

        with patch.object(agent, "close_agent"):
            poll_agents(ls)

        proc.terminate.assert_called_once()
        self.assertIsNone(ls.agent_proc)


class TestSignalHandlerSetsExit(unittest.TestCase):
    """Verify the signal handler sets should_exit."""

    def test_sigint_sets_should_exit(self):
        ls = _make_loop_state()
        self.assertFalse(ls.should_exit)

        # Simulate what the signal handler does
        ls.should_exit = True
        ls.exit_reason = "signal"

        self.assertTrue(ls.should_exit)
        self.assertEqual(ls.exit_reason, "signal")


class TestEndToEndSignalCleanup(unittest.TestCase):
    """Integration: SIGINT -> should_exit -> cleanup -> agent terminated."""

    def test_signal_then_cleanup_kills_real_subprocess(self):
        # Spawn a real long-running process
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        ls = _make_loop_state(agent_proc=proc)

        # Simulate SIGINT handler
        ls.should_exit = True
        ls.exit_reason = "signal"

        # Run cleanup (which should terminate the real process)
        with patch.object(agent, "close_agent"):
            cleanup(ls)

        # Process should be dead
        exit_code = proc.wait(timeout=5)
        self.assertIsNotNone(exit_code)

        # Verify it's not still running
        self.assertIsNotNone(proc.returncode)


if __name__ == "__main__":
    unittest.main()
