"""Task DAG operations, flock helper, and history logging.

All tasks.json mutations go through locked_tasks() to prevent corruption
from concurrent writers (the loop process and external /ralpanda sessions).
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------

@contextmanager
def locked_tasks(tasks_file: Path):
    """Acquire an exclusive flock on tasks.json, yield the parsed data,
    and atomically write it back on exit.

    Usage:
        with locked_tasks(path) as data:
            data["tasks"][0]["status"] = "done"
        # written back atomically on context exit
    """
    fd = os.open(str(tasks_file), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Read current contents (dup fd so fdopen doesn't close our lock fd)
        with os.fdopen(os.dup(fd), "r") as f:
            f.seek(0)
            data = json.load(f)
        yield data
        # Atomic write back
        tmp = f"{tasks_file}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, str(tasks_file))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def locked_tasks_readonly(tasks_file: Path):
    """Acquire a shared flock on tasks.json and yield the parsed data.
    Does NOT write back on exit.
    """
    fd = os.open(str(tasks_file), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        with os.fdopen(os.dup(fd), "r") as f:
            f.seek(0)
            data = json.load(f)
        yield data
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_tasks(tasks_file: Path) -> dict:
    """Read tasks.json with a shared lock. Returns the full JSON object."""
    with locked_tasks_readonly(tasks_file) as data:
        return data


# ---------------------------------------------------------------------------
# Plan slug
# ---------------------------------------------------------------------------

def plan_slug_from_source(plan_source: str | None) -> str:
    """Extract plan slug from a plan_source path.

    e.g. ".ralpanda/plans/add-user-auth.md" -> "add-user-auth"
    Falls back to "_gate" for tasks without a plan_source.
    """
    if plan_source:
        return Path(plan_source).stem
    return "_gate"


# ---------------------------------------------------------------------------
# Log file paths
# ---------------------------------------------------------------------------

def task_log_path(ralpanda_dir: Path, task_id: str, suffix: str = "") -> Path:
    """Return the log file path for a task, sanitizing the ID.

    "ralpanda/add-auth/001"         -> .ralpanda/logs/ralpanda-add-auth-001.jsonl
    "ralpanda/add-auth/001" "syntax" -> .ralpanda/logs/ralpanda-add-auth-001-syntax.jsonl
    """
    safe_id = task_id.replace("/", "-")
    logs_dir = ralpanda_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    if suffix:
        return logs_dir / f"{safe_id}-{suffix}.jsonl"
    return logs_dir / f"{safe_id}.jsonl"


def outcome_path(ralpanda_dir: Path, task_id: str) -> Path:
    """Return the outcome file path for a task."""
    safe_id = task_id.replace("/", "-")
    outcomes_dir = ralpanda_dir / "outcomes"
    outcomes_dir.mkdir(parents=True, exist_ok=True)
    return outcomes_dir / f"{safe_id}.json"


# ---------------------------------------------------------------------------
# Task queries (read-only, operate on already-loaded task list)
# ---------------------------------------------------------------------------

def get_next_task(tasks: list[dict]) -> dict | None:
    """Return the first pending task whose deps are all satisfied (done/split)."""
    satisfied = {t["id"] for t in tasks if t["status"] in ("done", "split")}
    for t in tasks:
        if t["status"] != "pending":
            continue
        if all(dep in satisfied for dep in t.get("depends_on", [])):
            return t
    return None


def blocked_reason(tasks: list[dict]) -> str:
    """Return a human-readable reason why no task can run.

    Assumes get_next_task() already returned None and not all_done().
    """
    status_by_id = {t["id"]: t["status"] for t in tasks}
    # Find pending tasks and their unsatisfied deps
    for t in tasks:
        if t["status"] != "pending":
            continue
        unsatisfied = []
        for dep_id in t.get("depends_on", []):
            dep_status = status_by_id.get(dep_id, "missing")
            if dep_status not in ("done", "split"):
                unsatisfied.append((dep_id, dep_status))
        if unsatisfied:
            # Report the first blocked task and its blockers
            blockers = ", ".join(
                f"{did.split('/')[-1]}({dst})" for did, dst in unsatisfied
            )
            return f"{t['id'].split('/')[-1]} blocked by: {blockers}"
    return "no runnable tasks"


def all_done(tasks: list[dict]) -> bool:
    """True if every task is in a terminal state (done/split)."""
    return all(
        t["status"] in ("done", "split")
        for t in tasks
    )


def task_counts(tasks: list[dict]) -> dict[str, int]:
    """Return counts per status."""
    counts: dict[str, int] = {}
    for t in tasks:
        s = t["status"]
        counts[s] = counts.get(s, 0) + 1
    return counts


def get_task(tasks: list[dict], task_id: str) -> dict | None:
    """Find a task by ID."""
    for t in tasks:
        if t["id"] == task_id:
            return t
    return None


def validate_dag(tasks: list[dict]) -> bool:
    """Return True if the dependency graph is acyclic."""
    graph = {t["id"]: t.get("depends_on", []) for t in tasks}
    visited: set[str] = set()
    stack: set[str] = set()

    def has_cycle(node: str) -> bool:
        if node in stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        stack.add(node)
        for dep in graph.get(node, []):
            if has_cycle(dep):
                return True
        stack.discard(node)
        return False

    return not any(has_cycle(t) for t in graph)


def validate_unique_ids(tasks: list[dict]) -> list[str]:
    """Return list of duplicate IDs (empty if all unique)."""
    seen: dict[str, int] = {}
    for t in tasks:
        tid = t["id"]
        seen[tid] = seen.get(tid, 0) + 1
    return [tid for tid, count in seen.items() if count > 1]


def validate_tasks(tasks: list[dict]) -> str:
    """Return 'valid' or an error description."""
    dupes = validate_unique_ids(tasks)
    if dupes:
        return f"duplicate_ids: {', '.join(dupes)}"
    if not validate_dag(tasks):
        return "cycle_detected"
    return "valid"


def _global_max_num(tasks: list[dict]) -> int:
    """Return the highest task number across all slugs."""
    max_num = 0
    for t in tasks:
        parts = t["id"].split("/")
        if len(parts) == 3 and parts[0] == "ralpanda":
            try:
                num = int(parts[2])
                max_num = max(max_num, num)
            except ValueError:
                pass
    return max_num


def next_task_id(tasks: list[dict], plan_slug: str) -> str:
    """Generate the next globally-unique task ID under a plan slug."""
    max_num = _global_max_num(tasks)
    return f"ralpanda/{plan_slug}/{max_num + 1:03d}"


def next_task_ids(tasks: list[dict], plan_slug: str, count: int) -> list[str]:
    """Generate count globally-unique sequential task IDs under a plan slug."""
    max_num = _global_max_num(tasks)
    return [f"ralpanda/{plan_slug}/{max_num + i + 1:03d}" for i in range(count)]


# ---------------------------------------------------------------------------
# Task mutations (locked read-modify-write)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def update_task_status(tasks_file: Path, task_id: str, status: str) -> None:
    """Update a task's status and set appropriate timestamps."""
    now = _now_iso()
    with locked_tasks(tasks_file) as data:
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["status"] = status
                if status == "running":
                    t["started_at"] = now
                elif status in ("done", "failed", "split"):
                    t["completed_at"] = now
                break


def update_task_outcome(tasks_file: Path, task_id: str, outcome: dict) -> None:
    """Set a task's outcome field."""
    with locked_tasks(tasks_file) as data:
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["outcome"] = outcome
                break


def update_task_usage(tasks_file: Path, task_id: str, usage: dict) -> None:
    """Set a task's usage field (token counts, cost)."""
    with locked_tasks(tasks_file) as data:
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["usage"] = usage
                break


def increment_attempt(tasks_file: Path, task_id: str) -> None:
    """Bump a task's attempt counter by 1."""
    with locked_tasks(tasks_file) as data:
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["attempt"] = t.get("attempt", 0) + 1
                break


def insert_tasks_after(tasks_file: Path, after_id: str, new_tasks: list[dict]) -> None:
    """Insert new tasks into the array just after the given task."""
    with locked_tasks(tasks_file) as data:
        tasks = data["tasks"]
        for i, t in enumerate(tasks):
            if t["id"] == after_id:
                data["tasks"] = tasks[:i + 1] + new_tasks + tasks[i + 1:]
                break


def insert_tasks_before(tasks_file: Path, before_id: str, new_tasks: list[dict]) -> None:
    """Insert new tasks just before the given task and update its deps."""
    new_ids = [t["id"] for t in new_tasks]
    with locked_tasks(tasks_file) as data:
        tasks = data["tasks"]
        for i, t in enumerate(tasks):
            if t["id"] == before_id:
                data["tasks"] = tasks[:i] + new_tasks + tasks[i:]
                # Update the target task's depends_on
                target = data["tasks"][i + len(new_tasks)]
                deps = list(set(target.get("depends_on", []) + new_ids))
                target["depends_on"] = deps
                break


def rewire_deps(tasks_file: Path, old_id: str, new_ids: list[str]) -> None:
    """Replace old_id with new_ids in all tasks' depends_on arrays."""
    with locked_tasks(tasks_file) as data:
        for t in data["tasks"]:
            deps = t.get("depends_on", [])
            if old_id in deps:
                new_deps = []
                for d in deps:
                    if d == old_id:
                        new_deps.extend(new_ids)
                    else:
                        new_deps.append(d)
                t["depends_on"] = list(dict.fromkeys(new_deps))  # unique, preserve order


def insert_pause_before(tasks_file: Path, before_id: str, plan_source: str | None = None) -> str | None:
    """Insert a pause task as a dependency of the given task. Returns the pause task ID,
    or None if there's already a pending pause blocking this task."""
    with locked_tasks(tasks_file) as data:
        tasks = data["tasks"]
        # Find the target task
        target = None
        for t in tasks:
            if t["id"] == before_id:
                target = t
                break
        if not target:
            raise ValueError(f"Task {before_id} not found")

        # Check if there's already a pending pause in the target's deps
        target_deps = set(target.get("depends_on", []))
        for t in tasks:
            if t["type"] == "pause" and t["status"] == "pending" and t["id"] in target_deps:
                return None  # Already has a pending pause

        slug = plan_slug_from_source(plan_source or target.get("plan_source"))
        pause_id = next_task_id(tasks, slug)
        pause_task = {
            "id": pause_id,
            "title": "Pause (inserted from TUI)",
            "type": "pause",
            "status": "pending",
            "depends_on": list(target.get("depends_on", [])),
            "plan_source": target.get("plan_source"),
            "description": "Pause inserted from TUI before task execution.",
            "acceptance_criteria": [],
            "outcome": None,
            "attempt": 0,
            "created_at": _now_iso(),
            "started_at": None,
            "completed_at": None,
        }

        # Insert before target
        for i, t in enumerate(tasks):
            if t["id"] == before_id:
                data["tasks"] = tasks[:i] + [pause_task] + tasks[i:]
                break

        # Add pause as dependency of target (deduplicate)
        for t in data["tasks"]:
            if t["id"] == before_id:
                deps = list(dict.fromkeys(t.get("depends_on", []) + [pause_id]))
                t["depends_on"] = deps
                break

        return pause_id


def insert_dirty_pause(tasks_file: Path, before_id: str, dirty_info: str) -> str | None:
    """Insert a pause task before *before_id* because git is dirty.

    Returns the pause task ID, or None if a dirty-pause already blocks this task.
    """
    with locked_tasks(tasks_file) as data:
        tasks = data["tasks"]
        target = None
        for t in tasks:
            if t["id"] == before_id:
                target = t
                break
        if not target:
            return None

        # Don't stack dirty pauses — check if one already blocks this task
        target_deps = set(target.get("depends_on", []))
        for t in tasks:
            if (
                t["type"] == "pause"
                and t["status"] == "pending"
                and t["id"] in target_deps
                and t.get("pause_reason", "").startswith("git dirty")
            ):
                return None

        slug = plan_slug_from_source(target.get("plan_source"))
        pause_id = next_task_id(tasks, slug)
        pause_task = {
            "id": pause_id,
            "title": "Pause (git dirty)",
            "type": "pause",
            "pause_reason": f"git dirty: {dirty_info}",
            "status": "pending",
            "depends_on": list(target.get("depends_on", [])),
            "plan_source": target.get("plan_source"),
            "description": f"Auto-inserted because git was dirty before starting {before_id}.",
            "acceptance_criteria": [],
            "outcome": None,
            "attempt": 0,
            "created_at": _now_iso(),
            "started_at": None,
            "completed_at": None,
        }

        # Insert before target
        for i, t in enumerate(tasks):
            if t["id"] == before_id:
                data["tasks"] = tasks[:i] + [pause_task] + tasks[i:]
                break

        # Add pause as dependency of target
        for t in data["tasks"]:
            if t["id"] == before_id:
                deps = list(dict.fromkeys(t.get("depends_on", []) + [pause_id]))
                t["depends_on"] = deps
                break

        return pause_id


def insert_global_pause(tasks_file: Path) -> str | None:
    """Insert a pause task as a dependency of ALL pending non-pause tasks.
    Returns the pause task ID, or None if there's already a pending global pause."""
    with locked_tasks(tasks_file) as data:
        tasks = data["tasks"]

        # Check if there's already a pending pause with no deps that isn't done
        for t in tasks:
            if t["type"] == "pause" and t["status"] == "pending" and not t.get("depends_on"):
                return None  # Already have a pending global pause

        pause_id = next_task_id(tasks, "_gate")
        pause_task = {
            "id": pause_id,
            "title": "Pause (global, inserted from TUI)",
            "type": "pause",
            "status": "pending",
            "depends_on": [],
            "plan_source": None,
            "description": "Global pause inserted from TUI.",
            "acceptance_criteria": [],
            "outcome": None,
            "attempt": 0,
            "created_at": _now_iso(),
            "started_at": None,
            "completed_at": None,
        }

        # Find insertion point (after last running/done task, before first pending)
        insert_idx = 0
        for i, t in enumerate(tasks):
            if t["status"] in ("done", "split", "running", "failed"):
                insert_idx = i + 1

        data["tasks"] = tasks[:insert_idx] + [pause_task] + tasks[insert_idx:]

        # Add pause as dependency of all pending NON-PAUSE tasks only
        # (don't chain pauses — that creates unresolvable deps)
        for t in data["tasks"]:
            if t["status"] == "pending" and t["id"] != pause_id and t["type"] != "pause":
                deps = list(dict.fromkeys(t.get("depends_on", []) + [pause_id]))
                t["depends_on"] = deps

        return pause_id


def clear_done_plans(tasks_file: Path) -> int:
    """Remove tasks belonging to plans where every task in that plan is done/split.

    A plan is identified by its plan_source.  If ANY task in a plan is not in a
    terminal state (done/split), no tasks from that plan are removed.  Tasks
    with plan_source=None (_gate tasks) are treated as their own implicit plan
    group.

    Also cleans up dependency references to removed task IDs.

    Returns the number of tasks removed.
    """
    with locked_tasks(tasks_file) as data:
        tasks = data["tasks"]

        # Group task indices by plan_source
        plans: dict[str | None, list[int]] = {}
        for i, t in enumerate(tasks):
            ps = t.get("plan_source")
            plans.setdefault(ps, []).append(i)

        # Determine which plan groups are fully done
        remove_ids: set[str] = set()
        for ps, indices in plans.items():
            if all(tasks[i]["status"] in ("done", "split") for i in indices):
                for i in indices:
                    remove_ids.add(tasks[i]["id"])

        if not remove_ids:
            return 0

        # Remove tasks and clean up dependency references
        data["tasks"] = [t for t in tasks if t["id"] not in remove_ids]
        for t in data["tasks"]:
            deps = t.get("depends_on", [])
            if any(d in remove_ids for d in deps):
                t["depends_on"] = [d for d in deps if d not in remove_ids]

        return len(remove_ids)


# ---------------------------------------------------------------------------
# Event logging
# ---------------------------------------------------------------------------

def log_event(
    history_file: Path,
    event: str,
    task_id: str = "",
    detail: str = "",
) -> None:
    """Append a JSON event to history.jsonl."""
    entry: dict = {
        "ts": _now_iso(),
        "event": event,
    }
    if task_id:
        entry["task_id"] = task_id
    if detail:
        entry["detail"] = detail
    with open(history_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Token usage extraction from stream-json logs
# ---------------------------------------------------------------------------

def extract_usage(log_path: Path) -> dict | None:
    """Parse a stream-json log file to extract final token usage and peak context."""
    if not log_path.exists():
        return None

    peak_context = 0
    result_usage = None

    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Track peak per-turn context from assistant messages
            if obj.get("type") == "assistant":
                u = obj.get("message", {}).get("usage", {})
                ctx = (
                    u.get("input_tokens", 0)
                    + u.get("cache_read_input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0)
                )
                if ctx > peak_context:
                    peak_context = ctx

            # Capture final result usage
            if obj.get("type") == "result" and "usage" in obj:
                u = obj["usage"]
                result_usage = {
                    "input_tokens": u.get("input_tokens", 0),
                    "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
                    "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
                    "output_tokens": u.get("output_tokens", 0),
                    "cost_usd": obj.get("total_cost_usd"),
                    "peak_context": peak_context,
                }

    return result_usage
