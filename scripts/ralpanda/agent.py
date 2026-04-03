"""Agent lifecycle: spawn, poll, collect outcomes, review orchestration, splits."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import dag, git, prompt


# ---------------------------------------------------------------------------
# Spawn agents
# ---------------------------------------------------------------------------

def spawn_agent(
    prompt_text: str,
    model: str,
    log_path: Path,
    *,
    allowed_tools: str | None = None,
    disallowed_tools: str | None = None,
    max_turns: int | None = None,
) -> subprocess.Popen:
    """Launch a claude CLI agent as a subprocess.

    stdout/stderr go to log_path. Returns the Popen object.
    """
    cmd = [
        "claude", "-p", prompt_text,
        "--model", model,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    if allowed_tools is not None:
        cmd.extend(["--allowedTools", allowed_tools])
    if disallowed_tools is not None:
        cmd.extend(["--disallowedTools", disallowed_tools])
    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    # Keep reference to log file so it stays open
    proc._log_file = log_file  # type: ignore[attr-defined]
    return proc


def close_agent(proc: subprocess.Popen) -> None:
    """Close the log file associated with an agent process."""
    log_file = getattr(proc, "_log_file", None)
    if log_file:
        log_file.close()


# ---------------------------------------------------------------------------
# Outcome collection
# ---------------------------------------------------------------------------

def collect_outcome(ralpanda_dir: Path, task_id: str) -> dict | None:
    """Read the outcome file written by the agent. Returns None if missing."""
    path = dag.outcome_path(ralpanda_dir, task_id)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def cleanup_outcome(ralpanda_dir: Path, task_id: str) -> None:
    """Remove the outcome file after it's been processed."""
    path = dag.outcome_path(ralpanda_dir, task_id)
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# Work task post-processing
# ---------------------------------------------------------------------------

def process_work_result(
    ralpanda_dir: Path,
    tasks_file: Path,
    task_id: str,
    exit_code: int,
    max_attempts: int,
    history_file: Path,
) -> None:
    """Handle everything after a work agent exits.

    Reads outcome file, updates tasks.json, handles splits, commits.
    """
    outcome = collect_outcome(ralpanda_dir, task_id)

    # Get current attempt count
    tasks_data = dag.load_tasks(tasks_file)
    task = dag.get_task(tasks_data["tasks"], task_id)
    current_attempt = task.get("attempt", 1) if task else 1

    if exit_code == 0 and outcome:
        status = outcome.get("status", "done")

        # Write outcome to tasks.json
        dag.update_task_outcome(tasks_file, task_id, outcome)

        if status == "split":
            # Process split
            split_into = outcome.get("split_into", [])
            if split_into:
                _process_split(ralpanda_dir, tasks_file, task_id, split_into, history_file)
            else:
                dag.update_task_status(tasks_file, task_id, "done")
                dag.log_event(history_file, "task_completed", task_id)
        elif status == "failed":
            # Agent reported failure in outcome
            if current_attempt >= max_attempts:
                dag.update_task_status(tasks_file, task_id, "failed")
                dag.log_event(history_file, "task_failed", task_id, "agent_reported_failure")
            else:
                dag.update_task_status(tasks_file, task_id, "pending")
                dag.log_event(history_file, "task_retry", task_id, f"attempt={current_attempt}")
        else:
            # Done
            dag.update_task_status(tasks_file, task_id, "done")
            dag.log_event(history_file, "task_completed", task_id)
    elif exit_code == 0 and not outcome:
        # Agent exited cleanly but didn't write an outcome file
        dag.update_task_status(tasks_file, task_id, "done")
        dag.log_event(history_file, "task_completed", task_id, "no_outcome_file")
    else:
        # Non-zero exit
        if outcome:
            dag.update_task_outcome(tasks_file, task_id, outcome)

        if current_attempt >= max_attempts:
            dag.update_task_status(tasks_file, task_id, "failed")
            dag.log_event(
                history_file, "task_failed", task_id,
                f"exit_code={exit_code},max_attempts_reached",
            )
        else:
            dag.update_task_status(tasks_file, task_id, "pending")
            dag.log_event(
                history_file, "task_retry", task_id,
                f"exit_code={exit_code},attempt={current_attempt}",
            )

    # Commit any changes the agent made
    sha = git.commit_task(tasks_file, task_id)
    if sha:
        dag.log_event(history_file, "committed", task_id, f"sha={sha}")

    # Extract and persist token usage
    log_path = dag.task_log_path(ralpanda_dir, task_id)
    usage = dag.extract_usage(log_path)
    if usage:
        dag.update_task_usage(tasks_file, task_id, usage)

    # Clean up outcome file
    cleanup_outcome(ralpanda_dir, task_id)


def _process_split(
    ralpanda_dir: Path,
    tasks_file: Path,
    task_id: str,
    split_into: list[dict],
    history_file: Path,
) -> None:
    """Create subtasks from a split outcome and rewire dependencies."""
    tasks_data = dag.load_tasks(tasks_file)
    tasks = tasks_data["tasks"]
    parent = dag.get_task(tasks, task_id)
    if not parent:
        return

    parent_deps = parent.get("depends_on", [])
    plan_source = parent.get("plan_source")
    slug = dag.plan_slug_from_source(plan_source)

    # Read global acceptance criteria from config
    config_path = ralpanda_dir / "config.json"
    global_criteria: list[str] = []
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
            global_criteria = config.get("task_acceptance_criteria", [])
        except (json.JSONDecodeError, OSError):
            pass

    # Generate IDs for subtasks
    new_ids = dag.next_task_ids(tasks, slug, len(split_into))

    # Build title -> ID map for resolving depends_on_subtasks
    title_to_id = {}
    for i, sub in enumerate(split_into):
        title_to_id[sub["title"]] = new_ids[i]

    # Build full task objects
    now = dag._now_iso()
    new_tasks = []
    for i, sub in enumerate(split_into):
        subtask_deps = [
            title_to_id[t]
            for t in sub.get("depends_on_subtasks", [])
            if t in title_to_id
        ]
        criteria = list(sub.get("acceptance_criteria", []))
        for gc in global_criteria:
            if gc not in criteria:
                criteria.append(gc)

        new_tasks.append({
            "id": new_ids[i],
            "title": sub["title"],
            "type": "work",
            "status": "pending",
            "depends_on": list(dict.fromkeys(parent_deps + subtask_deps)),
            "plan_source": plan_source,
            "description": sub.get("description", ""),
            "acceptance_criteria": criteria,
            "outcome": None,
            "attempt": 0,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
        })

    # Insert subtasks after parent
    dag.insert_tasks_after(tasks_file, task_id, new_tasks)

    # Rewire: anything depending on parent now depends on all subtasks
    dag.rewire_deps(tasks_file, task_id, [t["id"] for t in new_tasks])

    # Mark parent as split
    dag.update_task_status(tasks_file, task_id, "split")

    # Validate
    reloaded = dag.load_tasks(tasks_file)
    check = dag.validate_tasks(reloaded["tasks"])
    if check != "valid":
        dag.log_event(history_file, "split_integrity_failed", task_id, check)

    dag.log_event(
        history_file, "task_split", task_id,
        f"subtasks={','.join(t['id'] for t in new_tasks)}",
    )


# ---------------------------------------------------------------------------
# Review orchestration
# ---------------------------------------------------------------------------

@dataclass
class ReviewState:
    """State machine for review task execution."""
    task_id: str
    checks: list[dict]
    parallel_indices: list[int] = field(default_factory=list)
    isolated_indices: list[int] = field(default_factory=list)
    phase: str = "init"  # init -> parallel -> isolated -> collecting -> coordinator -> done
    parallel_procs: dict[int, subprocess.Popen] = field(default_factory=dict)
    current_isolated_idx: int = -1
    current_isolated_proc: subprocess.Popen | None = None
    check_results: list[dict] = field(default_factory=list)
    failed_checks: list[dict] = field(default_factory=list)
    failed_analyses: list[str] = field(default_factory=list)
    infra_failed_checks: list[str] = field(default_factory=list)
    coordinator_proc: subprocess.Popen | None = None

    def __post_init__(self) -> None:
        for i, check in enumerate(self.checks):
            mode = check.get("mode", "isolated")
            if mode == "parallel":
                self.parallel_indices.append(i)
            else:
                self.isolated_indices.append(i)

        if self.parallel_indices:
            self.phase = "parallel"
        elif self.isolated_indices:
            self.phase = "isolated"
        else:
            self.phase = "collecting"


def start_review(
    ralpanda_dir: Path,
    tasks_file: Path,
    task_id: str,
    model: str,
) -> ReviewState:
    """Initialize review and launch parallel checks."""
    tasks_data = dag.load_tasks(tasks_file)
    task = dag.get_task(tasks_data["tasks"], task_id)
    checks = task.get("checks", []) if task else []

    if not checks:
        return ReviewState(task_id=task_id, checks=[], phase="done")

    base_sha = git.get_base_sha(ralpanda_dir)
    state = ReviewState(task_id=task_id, checks=checks)

    # Launch parallel checks
    if state.phase == "parallel":
        for i in state.parallel_indices:
            check = checks[i]
            log_path = dag.task_log_path(ralpanda_dir, task_id, check["name"])
            p = prompt.build_review_check_prompt(
                check["name"], check["prompt"], "parallel",
                task_id, base_sha,
            )
            proc = spawn_agent(
                p, model, log_path,
                allowed_tools="Read Glob Grep Bash",
                disallowed_tools="Edit Write NotebookEdit",
            )
            state.parallel_procs[i] = proc
    elif state.phase == "isolated":
        _launch_next_isolated(state, ralpanda_dir, task_id, checks, model)

    return state


def poll_review(
    state: ReviewState,
    ralpanda_dir: Path,
    tasks_file: Path,
    model: str,
    history_file: Path,
) -> bool:
    """Poll review state machine. Returns True when review is complete."""
    if state.phase == "done":
        return True

    if state.phase == "parallel":
        # Check if all parallel checks are done
        all_done = True
        for i, proc in list(state.parallel_procs.items()):
            if proc.poll() is None:
                all_done = False
            else:
                close_agent(proc)
        if not all_done:
            return False

        state.parallel_procs.clear()
        # Move to isolated phase or collecting
        if state.isolated_indices:
            state.phase = "isolated"
            _launch_next_isolated(
                state, ralpanda_dir, state.task_id, state.checks, model,
            )
        else:
            state.phase = "collecting"

    if state.phase == "isolated":
        proc = state.current_isolated_proc
        if proc and proc.poll() is not None:
            close_agent(proc)
            state.current_isolated_proc = None
            # Launch next isolated or move to collecting
            if not _launch_next_isolated(
                state, ralpanda_dir, state.task_id, state.checks, model,
            ):
                state.phase = "collecting"
        elif proc is None:
            state.phase = "collecting"
        else:
            return False  # Still running

    if state.phase == "collecting":
        _collect_verdicts(state, ralpanda_dir)

        pass_count = sum(1 for r in state.check_results if r["status"] == "pass")
        fail_count = sum(1 for r in state.check_results if r["status"] == "fail")
        infra_count = sum(1 for r in state.check_results if r["status"] == "infra_fail")

        if fail_count == 0 and infra_count == 0:
            # All passed
            _finalize_review_pass(state, tasks_file, history_file)
            state.phase = "done"
            return True

        if fail_count > 0:
            # Need coordinator to create fix-up tasks
            state.phase = "coordinator"
            _launch_coordinator(state, ralpanda_dir, tasks_file, model)
        else:
            # Only infra fails — insert pause + cloned review
            state.phase = "done"
            _finalize_review_infra_fail(
                state, ralpanda_dir, tasks_file, history_file,
            )
            return True

    if state.phase == "coordinator":
        proc = state.coordinator_proc
        if proc and proc.poll() is not None:
            close_agent(proc)
            _process_coordinator_result(
                state, ralpanda_dir, tasks_file, history_file,
            )
            state.phase = "done"
            return True
        return False

    return state.phase == "done"


def _launch_next_isolated(
    state: ReviewState,
    ralpanda_dir: Path,
    task_id: str,
    checks: list[dict],
    model: str,
) -> bool:
    """Launch the next isolated check. Returns False if none remaining."""
    state.current_isolated_idx += 1
    # Find next un-launched isolated index
    for i in state.isolated_indices:
        check = checks[i]
        log_path = dag.task_log_path(ralpanda_dir, task_id, check["name"])
        if log_path.exists():
            continue  # Already ran
        base_sha = git.get_base_sha(ralpanda_dir)
        p = prompt.build_review_check_prompt(
            check["name"], check["prompt"], "isolated",
            task_id, base_sha,
        )
        state.current_isolated_proc = spawn_agent(p, model, log_path)
        state.current_isolated_idx = i
        return True
    return False


def _collect_verdicts(state: ReviewState, ralpanda_dir: Path) -> None:
    """Extract verdicts from all check log files."""
    for i, check in enumerate(state.checks):
        name = check["name"]
        log_path = dag.task_log_path(ralpanda_dir, state.task_id, name)

        last_text = _extract_last_assistant_text(log_path)
        verdict_line = ""
        for line in reversed(last_text.split("\n")):
            if line.startswith("VERDICT:"):
                verdict_line = line
                break

        if "VERDICT: PASS" in verdict_line:
            state.check_results.append({"name": name, "status": "pass", "detail": None})
        elif "VERDICT: INFRA_FAIL" in verdict_line:
            state.check_results.append({"name": name, "status": "infra_fail", "detail": last_text[-500:]})
            state.infra_failed_checks.append(name)
        elif "VERDICT: FAIL" in verdict_line:
            state.check_results.append({"name": name, "status": "fail", "detail": last_text[-500:]})
            state.failed_checks.append(check)
            state.failed_analyses.append(last_text)
        else:
            # No verdict — treat as infra fail
            state.check_results.append({
                "name": name, "status": "infra_fail",
                "detail": "No valid VERDICT line found in check output.",
            })
            state.infra_failed_checks.append(name)


def _extract_last_assistant_text(log_path: Path) -> str:
    """Extract assistant text content from a stream-json log."""
    if not log_path.exists():
        return ""
    texts = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("role") == "assistant" or (
                    obj.get("message", {}).get("role") == "assistant"
                ):
                    for block in obj.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            texts.append(block["text"])
    except OSError:
        pass
    return "\n".join(texts[-50:])  # Last 50 text blocks


def _finalize_review_pass(
    state: ReviewState,
    tasks_file: Path,
    history_file: Path,
) -> None:
    """All checks passed — write outcome and mark done."""
    outcome = {
        "summary": f"All {len(state.checks)} review checks passed.",
        "check_results": state.check_results,
    }
    dag.update_task_outcome(tasks_file, state.task_id, outcome)
    dag.log_event(history_file, "review_passed", state.task_id)


def _finalize_review_infra_fail(
    state: ReviewState,
    ralpanda_dir: Path,
    tasks_file: Path,
    history_file: Path,
) -> None:
    """Only infra fails — insert pause + cloned review."""
    _write_review_outcome(state, tasks_file)
    _insert_fixups_and_clone(
        state, ralpanda_dir, tasks_file, history_file,
        fixup_tasks=[],
    )


def _launch_coordinator(
    state: ReviewState,
    ralpanda_dir: Path,
    tasks_file: Path,
    model: str,
) -> None:
    """Spawn the coordinator agent to generate fix-up tasks."""
    tasks_data = dag.load_tasks(tasks_file)
    tasks = tasks_data["tasks"]
    task = dag.get_task(tasks, state.task_id)
    if not task:
        return

    plan_source = task.get("plan_source", "")
    slug = dag.plan_slug_from_source(plan_source)
    id_prefix = f"ralpanda/{slug}/"

    # Find global max ID number (across all slugs)
    max_num = dag._global_max_num(tasks)

    review_deps = task.get("depends_on", [])

    p = prompt.build_coordinator_prompt(
        state.task_id,
        state.failed_checks,
        state.failed_analyses,
        plan_source,
        id_prefix,
        max_num,
        review_deps,
    )

    log_path = dag.task_log_path(ralpanda_dir, state.task_id, "coordinator")
    state.coordinator_proc = spawn_agent(
        p, model, log_path,
        max_turns=1,
        allowed_tools="",
    )


def _process_coordinator_result(
    state: ReviewState,
    ralpanda_dir: Path,
    tasks_file: Path,
    history_file: Path,
) -> None:
    """Parse coordinator output and insert fix-up tasks + cloned review."""
    _write_review_outcome(state, tasks_file)

    # Extract fix-up tasks from coordinator log
    log_path = dag.task_log_path(ralpanda_dir, state.task_id, "coordinator")
    fixup_tasks = _parse_coordinator_output(log_path)

    if not fixup_tasks:
        dag.log_event(
            history_file, "review_fixup_failed", state.task_id,
            "coordinator could not produce tasks",
        )

    _insert_fixups_and_clone(
        state, ralpanda_dir, tasks_file, history_file,
        fixup_tasks=fixup_tasks,
    )


def _write_review_outcome(state: ReviewState, tasks_file: Path) -> None:
    """Write review outcome to tasks.json."""
    pass_count = sum(1 for r in state.check_results if r["status"] == "pass")
    fail_count = sum(1 for r in state.check_results if r["status"] == "fail")
    infra_count = sum(1 for r in state.check_results if r["status"] == "infra_fail")

    parts = [f"{pass_count} passed"]
    if fail_count:
        parts.append(f"{fail_count} failed")
    if infra_count:
        parts.append(f"{infra_count} infra_fail")

    outcome = {
        "summary": ", ".join(parts) + ".",
        "check_results": state.check_results,
    }
    dag.update_task_outcome(tasks_file, state.task_id, outcome)


def _insert_fixups_and_clone(
    state: ReviewState,
    ralpanda_dir: Path,
    tasks_file: Path,
    history_file: Path,
    fixup_tasks: list[dict],
) -> None:
    """Insert fix-up tasks, optional pause, and cloned review after current review."""
    tasks_data = dag.load_tasks(tasks_file)
    tasks = tasks_data["tasks"]
    task = dag.get_task(tasks, state.task_id)
    if not task:
        return

    plan_source = task.get("plan_source", "")
    slug = dag.plan_slug_from_source(plan_source)

    # Find global max ID (across all slugs, including fixup tasks from coordinator)
    max_num = dag._global_max_num(tasks)
    for t in fixup_tasks:
        try:
            n = int(t["id"].split("/")[-1])
            max_num = max(max_num, n)
        except (ValueError, IndexError):
            pass

    now = dag._now_iso()
    all_new_tasks = list(fixup_tasks)
    clone_extra_deps = [t["id"] for t in fixup_tasks]

    # Insert pause if infra fails
    if state.infra_failed_checks:
        max_num += 1
        pause_id = f"ralpanda/{slug}/{max_num:03d}"
        infra_names = ", ".join(state.infra_failed_checks)
        pause_task = {
            "id": pause_id,
            "title": "Pause: infrastructure issue in review checks",
            "type": "pause",
            "status": "pending",
            "depends_on": [],
            "plan_source": None,
            "description": f"Review checks could not run: {infra_names}. Fix the environment and resume.",
            "acceptance_criteria": [],
            "pause_reason": f"Review checks could not run: {infra_names}",
            "outcome": None,
            "attempt": 0,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
        }
        all_new_tasks.append(pause_task)
        clone_extra_deps.append(pause_id)

    # Clone the review task
    max_num += 1
    clone_id = f"ralpanda/{slug}/{max_num:03d}"
    clone_task = {
        "id": clone_id,
        "title": task.get("title", "Review"),
        "type": "review",
        "status": "pending",
        "depends_on": list(dict.fromkeys(task.get("depends_on", []) + clone_extra_deps)),
        "plan_source": plan_source,
        "description": task.get("description", ""),
        "acceptance_criteria": task.get("acceptance_criteria", []),
        "checks": task.get("checks", []),
        "outcome": None,
        "attempt": 0,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
    }
    all_new_tasks.append(clone_task)

    # Insert all new tasks after current review
    dag.insert_tasks_after(tasks_file, state.task_id, all_new_tasks)

    # Rewire: anything depending on this review now depends on the clone
    with dag.locked_tasks(tasks_file) as data:
        for t in data["tasks"]:
            if t["id"] != state.task_id and t["id"] != clone_id:
                deps = t.get("depends_on", [])
                if state.task_id in deps:
                    t["depends_on"] = [
                        clone_id if d == state.task_id else d
                        for d in deps
                    ]

    # Validate
    reloaded = dag.load_tasks(tasks_file)
    check = dag.validate_tasks(reloaded["tasks"])
    if check != "valid":
        dag.log_event(history_file, "fixup_integrity_failed", state.task_id, check)

    if fixup_tasks:
        dag.log_event(
            history_file, "fixup_tasks_inserted", state.task_id,
            f"count={len(fixup_tasks)},next_review={clone_id}",
        )
    if state.infra_failed_checks:
        dag.log_event(
            history_file, "infra_fail_pause_inserted", state.task_id,
            f"next_review={clone_id}",
        )


def _parse_coordinator_output(log_path: Path) -> list[dict]:
    """Extract the fix-up task array from the coordinator's stream-json log."""
    if not log_path.exists():
        return []

    # Look for the result line
    result_text = ""
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "result":
                    result_text = obj.get("result", "")
                    break
    except OSError:
        return []

    if not result_text:
        return []

    # Try to parse as JSON array directly
    try:
        data = json.loads(result_text)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback: extract JSON array with regex
    match = re.search(r"\[.*\]", result_text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    return []
