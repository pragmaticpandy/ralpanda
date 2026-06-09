"""Tests for user-level ralpanda hooks."""

import json
import os
import tempfile
import unittest
from pathlib import Path

# Allow running standalone or via unittest discovery.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralpanda import hooks


class HookEnvTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_home = self.root / "config"
        self.project = self.root / "project"
        self.ralpanda_dir = self.project / ".ralpanda"
        self.history_file = self.ralpanda_dir / "history.jsonl"
        self.project.mkdir()
        (self.ralpanda_dir / "logs").mkdir(parents=True)
        self.history_file.touch()

        self._old_env = {
            name: os.environ.get(name)
            for name in (
                "XDG_CONFIG_HOME",
                "CAPTURE_FILE",
                "ENV_CAPTURE_FILE",
            )
        }
        os.environ["XDG_CONFIG_HOME"] = str(self.config_home)

    def tearDown(self):
        for name, value in self._old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def hook_event_dir(self, event: str) -> Path:
        directory = self.config_home / "ralpanda" / "hooks" / event
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def write_script(self, event: str, name: str, body: str, mode: int = 0o755) -> Path:
        script = self.hook_event_dir(event) / name
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(mode)
        return script


class TestHookDiscovery(HookEnvTestCase):
    def test_discovers_executable_regular_files_in_order(self):
        first = self.write_script("task.finished", "10-first.sh", "exit 0\n")
        second = self.write_script("task.finished", "20-second.sh", "exit 0\n")
        self.write_script("task.finished", "30-not-executable.sh", "exit 0\n", 0o644)
        self.write_script("task.finished", ".hidden.sh", "exit 0\n")
        (self.hook_event_dir("task.finished") / "40-dir.sh").mkdir()

        self.assertEqual(
            hooks.discover_hooks("task.finished"),
            [first, second],
        )

    def test_missing_event_directory_has_no_hooks(self):
        self.assertEqual(hooks.discover_hooks("loop.completed"), [])


class TestHookExecution(HookEnvTestCase):
    def test_runs_hook_with_payload_and_environment(self):
        capture_file = self.root / "payload.json"
        env_capture_file = self.root / "env.txt"
        os.environ["CAPTURE_FILE"] = str(capture_file)
        os.environ["ENV_CAPTURE_FILE"] = str(env_capture_file)

        self.write_script(
            "loop.paused",
            "notify.sh",
            (
                "cat > \"$CAPTURE_FILE\"\n"
                "printf '%s|%s|%s|%s\\n' "
                "\"$RALPANDA_EVENT\" "
                "\"$RALPANDA_TASK_ID\" "
                "\"$RALPANDA_TASK_TYPE\" "
                "\"$RALPANDA_PAUSE_REASON\" > \"$ENV_CAPTURE_FILE\"\n"
            ),
        )

        results = hooks.run_event(
            "loop.paused",
            {
                "loop": {
                    "state": "paused",
                    "state_info": "ralpanda/test/001: waiting for user",
                    "current_task_id": "ralpanda/test/001",
                    "next_task_id": None,
                    "counts": {"running": 1},
                    "total_tasks": 1,
                },
                "task": {
                    "id": "ralpanda/test/001",
                    "type": "pause",
                    "status": "running",
                    "title": "Pause for inspection",
                    "pause_reason": "waiting for user",
                },
            },
            self.ralpanda_dir,
            history_file=self.history_file,
            project_root=self.project,
        )

        self.assertEqual(results[0]["status"], "ok")
        payload = json.loads(capture_file.read_text())
        self.assertEqual(payload["event"], "loop.paused")
        self.assertEqual(payload["project_root"], str(self.project.resolve()))
        self.assertEqual(payload["loop"]["state"], "paused")
        self.assertNotIn("pause_reason", payload)
        self.assertEqual(payload["task"]["id"], "ralpanda/test/001")
        self.assertEqual(
            env_capture_file.read_text().strip(),
            "loop.paused|ralpanda/test/001|pause|waiting for user",
        )
        hook_logs = list((self.ralpanda_dir / "logs" / "hooks" / "loop.paused").glob("*.log"))
        self.assertEqual(len(hook_logs), 1)
        self.assertIn("status=ok", hook_logs[0].read_text())

    def test_nonzero_hook_is_logged_to_history(self):
        self.write_script("task.finished", "fail.sh", "echo failing\nexit 7\n")

        results = hooks.run_event(
            "task.finished",
            {
                "loop": {
                    "state": "running",
                    "state_info": "ralpanda/test/002",
                    "current_task_id": "ralpanda/test/002",
                    "next_task_id": None,
                    "counts": {"failed": 1},
                    "total_tasks": 1,
                },
                "task": {
                    "id": "ralpanda/test/002",
                    "type": "work",
                    "status": "failed",
                    "title": "Do the thing",
                },
            },
            self.ralpanda_dir,
            history_file=self.history_file,
            project_root=self.project,
        )

        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(results[0]["returncode"], 7)
        history = [
            json.loads(line)
            for line in self.history_file.read_text().splitlines()
            if line.strip()
        ]
        self.assertEqual(history[-1]["event"], "hook_failed")
        self.assertIn("event=task.finished", history[-1]["detail"])


if __name__ == "__main__":
    unittest.main()
