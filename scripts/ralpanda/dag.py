"""Task DAG operations, flock helper, and history logging.

All tasks.json mutations go through locked_tasks() to prevent corruption
from concurrent writers (the loop process and external /ralpanda sessions).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


PLAN_COMPLETENESS_CHECK_NAME = "plan-completeness"

VALID_OUTCOME_STATUSES_BY_KIND = {
    "work": {"done", "failed", "split"},
    "review_check": {"pass", "fail", "infra_fail"},
    "review_compatibility_check": {"pass", "fail", "infra_fail"},
    "coordinator": {"tasks_created", "infra_fail"},
}


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
# Agent outcome/running paths
# ---------------------------------------------------------------------------

def safe_path_component(value: str | None) -> str:
    """Return a filesystem-safe path component for task/check namespaces."""
    text = str(value or "unnamed").strip()
    text = text.replace("/", "-")
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text)
    text = text.strip(".-")
    return text or "unnamed"


def task_safe_id(task_id: str) -> str:
    """Return the stable filesystem-safe directory name for a task ID."""
    return task_id.replace("/", "-")


def work_agent_namespace() -> str:
    """Return the namespace for a work-task agent."""
    return "work"


def review_agent_namespace(
    task_type: str,
    stage_name: str | None,
    check_name: str | None,
) -> str:
    """Return the namespaced ID for a review/check agent."""
    phase = (
        "review_compatibility"
        if task_type == "review_compatibility"
        else "review"
    )
    return "/".join((
        phase,
        safe_path_component(stage_name or "checks"),
        safe_path_component(check_name or "check"),
    ))


def coordinator_agent_namespace(coordinator_attempt: int) -> str:
    """Return the namespaced ID for a coordinator attempt."""
    return f"coordinator/attempt-{coordinator_attempt}"


def agent_kind_for_check_task(task_type: str) -> str:
    """Return the transport kind for a review-style task."""
    if task_type == "review_compatibility":
        return "review_compatibility_check"
    return "review_check"


def _agent_namespace_path(
    root: Path,
    task_id: str,
    attempt: int,
    namespace: str,
) -> Path:
    """Return a nested path for a task attempt and agent namespace."""
    parts = [safe_path_component(p) for p in namespace.split("/") if p]
    if not parts:
        parts = ["agent"]
    base = root / task_safe_id(task_id) / f"attempt-{attempt}"
    return base.joinpath(*parts[:-1], f"{parts[-1]}.json")


def outcome_path(
    ralpanda_dir: Path,
    task_id: str,
    attempt: int | None = None,
    namespace: str | None = None,
) -> Path:
    """Return the outcome path for an agent.

    New agents use attempt-scoped outcome files:
    .ralpanda/outcomes/<task-safe-id>/attempt-<n>/<namespace>.json

    The legacy two-argument behavior is retained for callers/tests that only
    need the old task-level location.
    """
    safe_id = task_safe_id(task_id)
    outcomes_dir = ralpanda_dir / "outcomes"
    outcomes_dir.mkdir(parents=True, exist_ok=True)
    if attempt is None:
        return outcomes_dir / f"{safe_id}.json"
    return _agent_namespace_path(
        outcomes_dir,
        task_id,
        attempt,
        namespace or work_agent_namespace(),
    )


def running_metadata_path(
    ralpanda_dir: Path,
    task_id: str,
    attempt: int,
    namespace: str,
) -> Path:
    """Return the running metadata path for an agent spawn."""
    root = ralpanda_dir / "running"
    root.mkdir(parents=True, exist_ok=True)
    return _agent_namespace_path(root, task_id, attempt, namespace)


def atomic_write_json(path: Path, payload: dict) -> None:
    """Atomically write a JSON object to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def read_json_file(path: Path) -> tuple[dict | None, str | None]:
    """Read a JSON object, returning (data, error)."""
    if not path.exists():
        return None, "missing outcome file"
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    except OSError as exc:
        return None, f"could not read outcome file: {exc}"
    if not isinstance(data, dict):
        return None, "outcome must be a JSON object"
    return data, None


def validate_outcome_envelope(
    outcome: dict,
    *,
    task_id: str,
    attempt: int,
    agent_kind: str,
    agent_namespace: str,
) -> str | None:
    """Validate the shared on-disk outcome transport envelope."""
    required = {
        "schema_version",
        "task_id",
        "attempt",
        "agent",
        "status",
        "summary",
        "payload",
    }
    missing = sorted(required - set(outcome))
    if missing:
        return f"outcome missing fields: {', '.join(missing)}"
    if outcome.get("schema_version") != 1:
        return "outcome schema_version must be 1"
    if outcome.get("task_id") != task_id:
        return "outcome task_id does not match expected task"
    if outcome.get("attempt") != attempt:
        return "outcome attempt does not match expected attempt"

    agent = outcome.get("agent")
    if not isinstance(agent, dict):
        return "outcome agent must be an object"
    if agent.get("kind") != agent_kind:
        return "outcome agent.kind does not match expected kind"
    if agent.get("namespace") != agent_namespace:
        return "outcome agent.namespace does not match expected namespace"

    valid_statuses = VALID_OUTCOME_STATUSES_BY_KIND.get(agent_kind)
    if not valid_statuses:
        return f"unknown agent kind: {agent_kind}"
    if outcome.get("status") not in valid_statuses:
        return (
            f"invalid outcome status for {agent_kind}: "
            f"{outcome.get('status')!r}"
        )

    if not isinstance(outcome.get("summary"), str):
        return "outcome summary must be a string"
    if not isinstance(outcome.get("payload"), dict):
        return "outcome payload must be an object"

    if (
        agent_kind in ("review_check", "review_compatibility_check")
        and outcome.get("status") in ("fail", "infra_fail")
        and not outcome.get("summary", "").strip()
    ):
        return "failed review/check outcomes must include a non-empty summary"

    return None


def read_valid_outcome(
    ralpanda_dir: Path,
    task_id: str,
    attempt: int,
    agent_namespace: str,
    agent_kind: str,
) -> tuple[dict | None, str | None, Path]:
    """Read and validate an expected outcome file."""
    path = outcome_path(ralpanda_dir, task_id, attempt, agent_namespace)
    outcome, error = read_json_file(path)
    if error:
        return None, error, path
    assert outcome is not None
    error = validate_outcome_envelope(
        outcome,
        task_id=task_id,
        attempt=attempt,
        agent_kind=agent_kind,
        agent_namespace=agent_namespace,
    )
    if error:
        return None, error, path
    return outcome, None, path


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
        return logs_dir / f"{safe_id}-{safe_path_component(suffix)}.jsonl"
    return logs_dir / f"{safe_id}.jsonl"


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


def review_check_stages(task: dict | None) -> tuple[list[dict], list[dict]]:
    """Return flattened review checks and ordered stage metadata.

    Review tasks historically used a flat ``checks`` array. Newer tasks may use
    ``check_stages`` so cheaper checks can gate more expensive later stages.
    Plan-completeness checks are treated as cheap coverage gates: when a
    multi-stage task has one in a later stage, normalize it into the first stage.
    This helper keeps the runtime and TUI compatible with both shapes.
    """
    if not task:
        return [], []

    configured_stages = (
        task.get("check_stages")
        or task.get("review_check_stages")
        or task.get("stages")
    )
    if isinstance(configured_stages, list) and configured_stages:
        normalized_stages: list[dict] = []
        for stage_num, stage in enumerate(configured_stages, start=1):
            if not isinstance(stage, dict):
                continue
            stage_checks = stage.get("checks", [])
            if not isinstance(stage_checks, list):
                continue
            normalized_stages.append({
                "name": stage.get("name") or f"stage-{stage_num}",
                "checks": [
                    dict(check)
                    for check in stage_checks
                    if isinstance(check, dict)
                ],
            })

        if len(normalized_stages) > 1:
            moved_checks: list[dict] = []
            for stage in normalized_stages[1:]:
                kept_checks = []
                for check in stage["checks"]:
                    if check.get("name") == PLAN_COMPLETENESS_CHECK_NAME:
                        moved_checks.append(check)
                    else:
                        kept_checks.append(check)
                stage["checks"] = kept_checks
            normalized_stages[0]["checks"].extend(moved_checks)

        checks: list[dict] = []
        stages: list[dict] = []
        for stage in normalized_stages:
            name = stage["name"]
            indices: list[int] = []
            for check in stage["checks"]:
                copied = dict(check)
                copied.setdefault("stage", name)
                indices.append(len(checks))
                checks.append(copied)
            stages.append({"name": name, "check_indices": indices})
        return checks, stages

    checks = [dict(c) for c in task.get("checks", []) if isinstance(c, dict)]
    if not checks:
        return [], []
    return checks, [{"name": "checks", "check_indices": list(range(len(checks)))}]


def review_checks(task: dict | None) -> list[dict]:
    """Return flattened review checks for display/counting."""
    checks, _ = review_check_stages(task)
    return checks


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


def validate_dependency_refs(tasks: list[dict]) -> list[str]:
    """Return dependency references that do not point at a task ID."""
    task_ids = {t["id"] for t in tasks}
    missing: list[str] = []
    for t in tasks:
        task_id = t["id"]
        for dep_id in t.get("depends_on", []):
            if dep_id not in task_ids:
                missing.append(f"{task_id} -> {dep_id}")
    return missing


def validate_tasks(tasks: list[dict]) -> str:
    """Return 'valid' or an error description."""
    dupes = validate_unique_ids(tasks)
    if dupes:
        return f"duplicate_ids: {', '.join(dupes)}"
    missing_deps = validate_dependency_refs(tasks)
    if missing_deps:
        return f"missing_dependencies: {', '.join(missing_deps)}"
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


def insert_git_preflight_pause(
    tasks_file: Path,
    before_id: str,
    title: str,
    pause_reason: str,
    description: str,
    duplicate_reason_prefix: str,
) -> str | None:
    """Insert a pause task before *before_id* for a git preflight failure.

    Returns the pause task ID, or None if a matching pause already blocks this task.
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

        # Don't stack identical preflight pauses while one already blocks this task.
        target_deps = set(target.get("depends_on", []))
        for t in tasks:
            if (
                t["type"] == "pause"
                and t["status"] == "pending"
                and t["id"] in target_deps
                and t.get("pause_reason", "").startswith(duplicate_reason_prefix)
            ):
                return None

        slug = plan_slug_from_source(target.get("plan_source"))
        pause_id = next_task_id(tasks, slug)
        pause_task = {
            "id": pause_id,
            "title": title,
            "type": "pause",
            "pause_reason": pause_reason,
            "status": "pending",
            "depends_on": list(target.get("depends_on", [])),
            "plan_source": target.get("plan_source"),
            "description": description,
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


def insert_dirty_pause(tasks_file: Path, before_id: str, dirty_info: str) -> str | None:
    """Insert a pause task before *before_id* because git is dirty.

    Returns the pause task ID, or None if a dirty-pause already blocks this task.
    """
    return insert_git_preflight_pause(
        tasks_file,
        before_id,
        "Pause (git dirty)",
        f"git dirty: {dirty_info}",
        f"Auto-inserted because git was dirty before starting {before_id}.",
        "git dirty",
    )


def insert_no_branch_pause(tasks_file: Path, before_id: str) -> str | None:
    """Insert a pause task before *before_id* because git is not on a branch.

    Returns the pause task ID, or None if a no-branch pause already blocks this task.
    """
    return insert_git_preflight_pause(
        tasks_file,
        before_id,
        "Pause (no git branch)",
        "git branch required: checkout or create a branch before resuming",
        f"Auto-inserted because git was not on a branch before starting {before_id}.",
        "git branch required",
    )


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
