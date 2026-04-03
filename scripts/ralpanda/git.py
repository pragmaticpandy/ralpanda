"""Git operations for ralpanda: commit, status, base_sha management."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import dag


def is_clean() -> bool:
    """Check if the git working tree is clean (no staged, unstaged, or untracked changes)."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == ""


def dirty_summary() -> str:
    """Return a short summary of what's dirty in the working tree."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True,
    )
    lines = [l for l in result.stdout.strip().split("\n") if l]
    if not lines:
        return ""
    # Categorize
    staged = sum(1 for l in lines if l[0] != " " and l[0] != "?")
    unstaged = sum(1 for l in lines if len(l) > 1 and l[1] != " " and l[0] != "?")
    untracked = sum(1 for l in lines if l.startswith("?"))
    parts = []
    if staged:
        parts.append(f"{staged} staged")
    if unstaged:
        parts.append(f"{unstaged} modified")
    if untracked:
        parts.append(f"{untracked} untracked")
    # Show first few filenames
    names = [l[2:].lstrip() for l in lines[:3]]
    summary = ", ".join(parts)
    summary += f": {', '.join(names)}"
    if len(lines) > 3:
        summary += f" (+{len(lines) - 3} more)"
    return summary


def commit_task(tasks_file: Path, task_id: str) -> str | None:
    """Stage all changes and commit with task metadata.

    Returns the short SHA on success, None if nothing to commit.
    .ralpanda/ should be in .gitignore so only real code gets staged.
    """
    # Check for changes
    if is_clean():
        return None

    # Read task info for commit message
    tasks_data = dag.load_tasks(tasks_file)
    task = dag.get_task(tasks_data["tasks"], task_id)
    if not task:
        return None

    title = task.get("title", "completed task")
    outcome = task.get("outcome", {}) or {}
    summary = outcome.get("summary", "completed task")
    files_list = outcome.get("files_changed", [])
    decisions = outcome.get("decisions", [])

    # Stage all changes
    subprocess.run(["git", "add", "-A"], capture_output=True, check=True)

    # Check if anything is actually staged
    result = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
    if result.returncode == 0:
        return None

    # Build commit message
    msg = f"{task_id}: {title}\n\n{summary}"
    if files_list:
        msg += f"\n\nFiles: {', '.join(files_list)}"
    if decisions:
        decision_lines = []
        for d in decisions:
            what = d.get("what", "")
            why = d.get("why", "")
            decision_lines.append(f"- {what}\n  (reason: {why})")
        msg += "\n\nDecisions:\n" + "\n".join(decision_lines)

    subprocess.run(["git", "commit", "-m", msg], capture_output=True, check=True)

    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def capture_base_sha(ralpanda_dir: Path) -> str:
    """Write current HEAD to .ralpanda/base_sha. Returns the SHA."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    sha = result.stdout.strip()
    (ralpanda_dir / "base_sha").write_text(sha)
    return sha


def delete_base_sha(ralpanda_dir: Path) -> None:
    """Remove .ralpanda/base_sha."""
    path = ralpanda_dir / "base_sha"
    if path.exists():
        path.unlink()


def get_base_sha(ralpanda_dir: Path) -> str | None:
    """Read .ralpanda/base_sha if it exists."""
    path = ralpanda_dir / "base_sha"
    if path.exists():
        return path.read_text().strip()
    return None
