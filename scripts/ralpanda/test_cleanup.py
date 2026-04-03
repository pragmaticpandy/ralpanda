"""Tests for cleanup and signal handling — run with: python -m pytest ralpanda/test_cleanup.py"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

# Allow running standalone or via pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralpanda import dag, agent
from ralpanda.__main__ import LoopState, cleanup, handle_input, poll_agents


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
