"""Tests for git.py — run with: python -m unittest ralpanda.test_git or python ralpanda/test_git.py"""

import unittest
from pathlib import Path
from unittest.mock import patch

# Allow running standalone or via unittest discovery.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralpanda import git


class _Result:
    def __init__(self, stdout: str):
        self.stdout = stdout


class TestCurrentBranch(unittest.TestCase):
    @patch("subprocess.run")
    def test_current_branch_returns_name(self, run):
        run.return_value = _Result("feature/work\n")

        self.assertEqual(git.current_branch(), "feature/work")
        self.assertTrue(git.is_on_branch())

    @patch("subprocess.run")
    def test_current_branch_returns_none_when_detached(self, run):
        run.return_value = _Result("\n")

        self.assertIsNone(git.current_branch())
        self.assertFalse(git.is_on_branch())


if __name__ == "__main__":
    unittest.main()
