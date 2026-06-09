"""Entry point for ralpanda: main tick loop with integrated curses TUI."""

from __future__ import annotations

import curses
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import dag, agent, git, hooks, prompt, tui


@dataclass
class LoopState:
    """All mutable state for the orchestration loop."""
    ralpanda_dir: Path
    tasks_file: Path
    history_file: Path
    config: dict
    tasks: list[dict] = field(default_factory=list)
    tasks_mtime: float = 0.0
    state: str = "running"  # running | paused | waiting_tasks | waiting_done | waiting_blocked
    state_info: str = ""    # human-readable context for current state, always set via set_state
    current_task_id: str | None = None
    agent_proc: object | None = None  # subprocess.Popen
    review_state: agent.ReviewState | None = None
    iteration: int = 0
    runs_remaining: int = 1000
    should_exit: bool = False
    force_quit: bool = False
    exit_reason: str | None = None

    # States that must always carry an explanation
    _REQUIRES_INFO = frozenset({"waiting_tasks", "waiting_blocked", "waiting_done", "paused", "idle"})

    def set_state(self, state: str, info: str = "") -> None:
        """Set loop state with required context info.

        States other than 'running' must provide info explaining why.
        """
        if state in self._REQUIRES_INFO and not info:
            raise ValueError(f"state '{state}' requires info (reason/context)")
        self.state = state
        self.state_info = info

    @property
    def model(self) -> str:
        return self.config.get("model", "opus[1m]")

    @property
    def max_attempts(self) -> int:
        return self.config.get("max_attempts_per_task", 3)

    def reload_tasks(self) -> None:
        """Reload tasks from disk."""
        try:
            data = dag.load_tasks(self.tasks_file)
            self.tasks = data.get("tasks", [])
            self.tasks_mtime = self.tasks_file.stat().st_mtime
        except (FileNotFoundError, OSError):
            self.tasks = []
            self.tasks_mtime = 0.0

    def maybe_reload_tasks(self) -> None:
        """Reload tasks if the file has been modified externally."""
        try:
            mtime = self.tasks_file.stat().st_mtime
        except (FileNotFoundError, OSError):
            if self.tasks or self.tasks_mtime:
                self.reload_tasks()
            return
        if mtime != self.tasks_mtime:
            self.reload_tasks()


def load_config(ralpanda_dir: Path) -> dict:
    """Load config.json, returning defaults for missing fields."""
    config_path = ralpanda_dir / "config.json"
    config: dict = {}
    if config_path.exists():
        try:
            import json
            with open(config_path) as f:
                config = json.load(f)
        except Exception:
            pass
    config.setdefault("model", "opus[1m]")
    config.setdefault("max_attempts_per_task", 3)
    config.setdefault(
        "coordinator_max_attempts", agent.COORDINATOR_DEFAULT_MAX_ATTEMPTS,
    )
    config.setdefault(
        "coordinator_max_turns", agent.COORDINATOR_DEFAULT_MAX_TURNS,
    )
    return config


def validate_startup(loop_state: LoopState) -> str | None:
    """Run startup checks. Returns error message or None if OK."""
    if not loop_state.tasks_file.exists():
        loop_state.reload_tasks()
        loop_state.set_state("waiting_tasks", f"waiting for {loop_state.tasks_file}")
        return None

    loop_state.reload_tasks()

    if not loop_state.tasks:
        loop_state.set_state("waiting_tasks", f"{loop_state.tasks_file} has no tasks")
        return None

    # Recover or reset any stale "running" tasks from a previous crashed loop.
    for t in loop_state.tasks:
        if t["status"] == "running":
            attempt = t.get("attempt", 1)
            terminated = agent.terminate_running_metadata_for_task(
                loop_state.ralpanda_dir,
                t["id"],
                attempt,
                loop_state.history_file,
                reason="startup_recovery",
            )
            if not terminated:
                return _unsafe_startup_process_error(t)

            if _recover_startup_running_task(loop_state, t):
                dag.log_event(
                    loop_state.history_file,
                    "startup_recovered_running_task",
                    t["id"],
                )
            else:
                dag.update_task_status(loop_state.tasks_file, t["id"], "pending")
                dag.log_event(
                    loop_state.history_file,
                    "startup_reset_running_task",
                    t["id"],
                    f"attempt={attempt}",
                )
    if any(t["status"] == "running" for t in loop_state.tasks):
        loop_state.reload_tasks()

    return validate_loaded_tasks(loop_state)


def _unsafe_startup_process_error(task: dict) -> str:
    """Explain why startup recovery refused to signal a recorded process."""
    task_id = task.get("id", "<unknown>")
    attempt = task.get("attempt", 1)
    return (
        "startup recovery refused to kill a recorded agent process for "
        f"{task_id} attempt {attempt} because it could not verify the PID/PGID "
        "still belongs to the original Claude process. Inspect the metadata "
        "under .ralpanda/running before retrying."
    )


def _recover_startup_running_task(loop_state: LoopState, task: dict) -> bool:
    """Finish a stale running task if current-attempt outcomes are complete."""
    task_id = task["id"]
    task_type = task.get("type", "work")
    attempt = task.get("attempt", 1)

    if task_type == "work":
        namespace = dag.work_agent_namespace()
        outcome, error, _ = agent.collect_expected_outcome(
            loop_state.ralpanda_dir,
            task_id,
            attempt,
            namespace,
            "work",
        )
        if error or not outcome:
            return False
        agent.process_work_result(
            loop_state.ralpanda_dir,
            loop_state.tasks_file,
            task_id,
            0,
            loop_state.max_attempts,
            loop_state.history_file,
        )
        return True

    if task_type in ("review", "review_compatibility"):
        recovered = agent.recover_review_task_from_outcomes(
            loop_state.ralpanda_dir,
            loop_state.tasks_file,
            task_id,
            loop_state.history_file,
        )
        if recovered:
            dag.update_task_status(loop_state.tasks_file, task_id, "done")
        return recovered

    return False


def validate_loaded_tasks(loop_state: LoopState) -> str | None:
    """Validate the currently loaded non-empty task list."""
    # Validate plan_source files exist
    seen_sources = set()
    for t in loop_state.tasks:
        ps = t.get("plan_source")
        if ps and ps not in seen_sources:
            seen_sources.add(ps)
            if not Path(ps).exists():
                return f"plan_source '{ps}' referenced in tasks.json does not exist."

    # Validate task integrity
    check = dag.validate_tasks(loop_state.tasks)
    if check != "valid":
        return f"tasks.json integrity check failed: {check}"

    return None


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

def handle_input(key: int, tui_state: tui.TUIState, loop_state: LoopState) -> None:
    """Process a keypress."""
    tasks = loop_state.tasks
    task_count = sum(1 for item in tui_state._display_list if not isinstance(item, str))

    # Determine max panels: 3 if selected task has review-style checks, else 2
    selected = tui_state._selected_task()
    is_review = tui.is_check_task(selected)
    max_panels = 3 if is_review else 2

    if key == curses.KEY_RIGHT:
        old = tui_state.focus_panel
        tui_state.focus_panel = min(tui_state.focus_panel + 1, max_panels - 1)
        if tui_state.focus_panel != old and old == 0:
            tui_state.detail_scroll = 0
            tui_state.check_detail_scroll = 0

    elif key == curses.KEY_LEFT:
        old = tui_state.focus_panel
        tui_state.focus_panel = max(tui_state.focus_panel - 1, 0)
        if old != tui_state.focus_panel:
            if tui_state.focus_panel == 0:
                tui_state.detail_scroll = 0
                tui_state.check_detail_scroll = 0

    elif key == curses.KEY_UP:
        if tui_state.focus_panel == 1 and is_review:
            # Navigate check list
            tui_state.selected_check_idx = max(0, tui_state.selected_check_idx - 1)
            tui_state.check_detail_scroll = 0
            tui_state._tailing_check_id = ""  # force log reload
        elif tui_state.focus_panel == 1:
            tui_state.detail_scroll = max(0, tui_state.detail_scroll - 1)
        elif tui_state.focus_panel == 2:
            tui_state.check_detail_scroll = max(0, tui_state.check_detail_scroll - 1)
        else:
            tui_state.auto_follow = False
            tui_state.selected_idx = max(0, tui_state.selected_idx - 1)
            tui_state._selected_task_id = ""  # let render persist new selection
            tui_state.detail_scroll = 0

    elif key == curses.KEY_DOWN:
        if tui_state.focus_panel == 1 and is_review:
            # Navigate check list (count includes coordinator entry)
            check_count = len(dag.review_checks(selected))
            if selected.get("type") == "review":
                check_count += 1  # +1 for coordinator
            if check_count:
                tui_state.selected_check_idx = min(check_count - 1, tui_state.selected_check_idx + 1)
            tui_state.check_detail_scroll = 0
            tui_state._tailing_check_id = ""  # force log reload
        elif tui_state.focus_panel == 1:
            tui_state.detail_scroll += 1  # clamped during render
        elif tui_state.focus_panel == 2:
            tui_state.check_detail_scroll += 1  # clamped during render
        else:
            tui_state.auto_follow = False
            if task_count > 0:
                tui_state.selected_idx = min(task_count - 1, tui_state.selected_idx + 1)
            tui_state._selected_task_id = ""  # let render persist new selection
            tui_state.detail_scroll = 0

    elif key == curses.KEY_PPAGE:  # Page Up
        if tui_state.focus_panel == 2:
            tui_state.check_detail_scroll = max(0, tui_state.check_detail_scroll - 20)
        elif tui_state.focus_panel == 1 and not is_review:
            tui_state.detail_scroll = max(0, tui_state.detail_scroll - 20)

    elif key == curses.KEY_NPAGE:  # Page Down
        if tui_state.focus_panel == 2:
            tui_state.check_detail_scroll += 20  # clamped during render
        elif tui_state.focus_panel == 1 and not is_review:
            tui_state.detail_scroll += 20  # clamped during render

    elif key == ord("\n") or key == curses.KEY_ENTER:
        tui_state.auto_follow = False
        tui_state.detail_scroll = 0

    elif key == ord("p"):
        # Insert pause before selected task only
        selected = tui_state._selected_task()
        if loop_state.tasks_file.exists() and selected and selected["status"] == "pending":
            dag.insert_pause_before(loop_state.tasks_file, selected["id"])
            loop_state.reload_tasks()

    elif key == ord("P"):
        # Insert global pause (blocks all pending tasks)
        if loop_state.tasks_file.exists() and loop_state.tasks:
            dag.insert_global_pause(loop_state.tasks_file)
            loop_state.reload_tasks()

    elif key == ord("r"):
        # Resume
        sentinel = loop_state.ralpanda_dir / "sentinels" / "resume"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()

    elif key == ord("q"):
        loop_state.should_exit = True
        loop_state.exit_reason = "quit"

    elif key == ord("Q"):
        loop_state.force_quit = True
        loop_state.should_exit = True
        loop_state.exit_reason = "force_quit"

    elif key == ord("f"):
        tui_state.auto_follow = True

    elif key == ord("c"):
        # Clear tasks from fully-done plans
        if loop_state.tasks_file.exists():
            removed = dag.clear_done_plans(loop_state.tasks_file)
            if removed:
                loop_state.reload_tasks()
                tui_state.selected_idx = 0
                tui_state._selected_task_id = ""
                tui_state.detail_scroll = 0


# ---------------------------------------------------------------------------
# Loop advancement
# ---------------------------------------------------------------------------

def advance_loop(loop_state: LoopState, tui_state: tui.TUIState) -> None:
    """Try to dispatch the next task. Only called when no agent is running."""
    loop_state.reload_tasks()
    tasks = loop_state.tasks

    # Check sentinels
    exit_sentinel = loop_state.ralpanda_dir / "sentinels" / "exit"
    if exit_sentinel.exists():
        loop_state.should_exit = True
        loop_state.exit_reason = "sentinel"
        dag.log_event(loop_state.history_file, "loop_exit_sentinel")
        return

    if not loop_state.tasks_file.exists():
        loop_state.set_state("waiting_tasks", f"waiting for {loop_state.tasks_file}")
        return

    if not tasks:
        loop_state.set_state("waiting_tasks", f"{loop_state.tasks_file} has no tasks")
        return

    validation_error = validate_loaded_tasks(loop_state)
    if validation_error:
        loop_state.set_state("waiting_blocked", validation_error)
        return

    # Get next task
    next_task = dag.get_next_task(tasks)

    if next_task is None:
        if dag.all_done(tasks):
            counts = dag.task_counts(tasks)
            done_n = counts.get("done", 0)
            total = len(tasks)
            was_done = loop_state.state == "waiting_done"
            loop_state.set_state("waiting_done", f"all {done_n}/{total} tasks complete")
            if not was_done:
                dag.log_event(loop_state.history_file, "all_tasks_complete")
                _run_loop_completed_hook(loop_state, counts, total)
        else:
            loop_state.set_state("waiting_blocked", dag.blocked_reason(tasks))
        return

    task_id = next_task["id"]
    task_type = next_task.get("type", "work")

    # Git must be on a branch and clean before starting any non-pause task.
    # Check here (not every tick) because git status is expensive.
    if task_type != "pause":
        if not git.is_on_branch():
            reason = "no current git branch; create or checkout a branch before resuming"
            dag.log_event(loop_state.history_file, "git_no_branch", task_id, reason)
            pause_id = dag.insert_no_branch_pause(loop_state.tasks_file, task_id)
            loop_state.reload_tasks()
            if pause_id:
                dag.log_event(loop_state.history_file, "no_branch_pause_inserted", pause_id)
            return

        if not git.is_clean():
            dirty_info = git.dirty_summary()
            dag.log_event(loop_state.history_file, "git_dirty", task_id, dirty_info)
            pause_id = dag.insert_dirty_pause(loop_state.tasks_file, task_id, dirty_info)
            loop_state.reload_tasks()
            if pause_id:
                dag.log_event(loop_state.history_file, "dirty_pause_inserted", pause_id)
            return

    loop_state.set_state("running", next_task["id"])
    _write_state(loop_state, "running")
    loop_state.iteration += 1

    loop_state.current_task_id = task_id
    (loop_state.ralpanda_dir / "current_task").write_text(task_id)

    # Update status and increment attempt
    dag.update_task_status(loop_state.tasks_file, task_id, "running")
    dag.increment_attempt(loop_state.tasks_file, task_id)
    loop_state.reload_tasks()

    current_task = dag.get_task(loop_state.tasks, task_id) or next_task
    attempt = current_task.get("attempt", next_task.get("attempt", 0) + 1)
    dag.log_event(
        loop_state.history_file, "task_started", task_id,
        f"attempt={attempt},type={task_type}",
    )

    # Capture base SHA before first work task
    if task_type == "work" and not (loop_state.ralpanda_dir / "base_sha").exists():
        sha = git.capture_base_sha(loop_state.ralpanda_dir)
        dag.log_event(loop_state.history_file, "base_sha_captured", detail=f"sha={sha}")

    # Reset log tailing for new task
    tui_state.log_lines = []
    tui_state.log_file_pos = 0
    tui_state._tailing_task_id = ""

    # Dispatch by type
    if task_type == "delete_base_sha":
        git.delete_base_sha(loop_state.ralpanda_dir)
        dag.update_task_status(loop_state.tasks_file, task_id, "done")
        dag.log_event(loop_state.history_file, "base_sha_deleted", task_id)
        _run_task_finished_hook(loop_state, task_id)
        _finish_task(loop_state)

    elif task_type == "pause":
        pause_reason = current_task.get("pause_reason") or current_task.get("title", "manual pause")
        loop_state.set_state("paused", f"{task_id}: {pause_reason}")
        _write_state(loop_state, "paused")
        if pause_reason:
            (loop_state.ralpanda_dir / "pause_reason").write_text(pause_reason)
        dag.log_event(loop_state.history_file, "loop_paused", task_id, pause_reason)
        _run_loop_paused_hook(loop_state, current_task, pause_reason)

    elif task_type in ("review", "review_compatibility"):
        review = agent.start_review(
            loop_state.ralpanda_dir,
            loop_state.tasks_file,
            task_id,
            loop_state.model,
        )
        loop_state.review_state = review
        if review.phase == "done":
            # No checks defined
            dag.update_task_status(loop_state.tasks_file, task_id, "done")
            _run_task_finished_hook(loop_state, task_id)
            _finish_task(loop_state)

    else:
        # Work task
        task_prompt = prompt.build_work_prompt(
            current_task, loop_state.tasks, loop_state.ralpanda_dir,
        )
        log_path = dag.task_log_path(loop_state.ralpanda_dir, task_id)
        agent_namespace = dag.work_agent_namespace()
        outcome_path = dag.outcome_path(
            loop_state.ralpanda_dir,
            task_id,
            attempt,
            agent_namespace,
        ).resolve()
        proc = agent.spawn_agent(
            task_prompt,
            loop_state.model,
            log_path,
            ralpanda_dir=loop_state.ralpanda_dir,
            task_id=task_id,
            attempt=attempt,
            agent_kind="work",
            agent_namespace=agent_namespace,
            expected_outcome_path=outcome_path,
        )
        loop_state.agent_proc = proc
        (loop_state.ralpanda_dir / "agent.pid").write_text(str(proc.pid))


def poll_agents(loop_state: LoopState) -> None:
    """Poll running agent or review state machine."""
    # Handle force quit
    if loop_state.force_quit and loop_state.agent_proc:
        agent.terminate_agent_process(loop_state.agent_proc)
        agent.finish_agent_process(loop_state.agent_proc)
        loop_state.agent_proc = None
        if loop_state.current_task_id:
            dag.update_task_status(
                loop_state.tasks_file,
                loop_state.current_task_id,
                "pending",
            )
            dag.log_event(
                loop_state.history_file,
                "agent_force_quit",
                loop_state.current_task_id,
            )
        _finish_task(loop_state)
        return

    # Poll work agent
    if loop_state.agent_proc and not loop_state.review_state:
        exit_code = loop_state.agent_proc.poll()
        if exit_code is None and loop_state.current_task_id:
            loop_state.reload_tasks()
            task = dag.get_task(loop_state.tasks, loop_state.current_task_id)
            attempt = task.get("attempt", 1) if task else 1
            agent_namespace = dag.work_agent_namespace()
            outcome, outcome_path = agent.stable_outcome_ready(
                loop_state.agent_proc,
                loop_state.ralpanda_dir,
                loop_state.current_task_id,
                attempt,
                agent_namespace,
                "work",
            )
            if outcome and outcome_path:
                agent.terminate_stale_process_after_outcome(
                    loop_state.agent_proc,
                    loop_state.history_file,
                    loop_state.current_task_id,
                    agent_namespace=agent_namespace,
                    outcome_path=outcome_path,
                )
                exit_code = loop_state.agent_proc.returncode
                if exit_code is None:
                    exit_code = 0

        if exit_code is not None:
            agent.finish_agent_process(loop_state.agent_proc)
            loop_state.agent_proc = None
            (loop_state.ralpanda_dir / "agent.pid").unlink(missing_ok=True)

            finish = agent.process_work_result(
                loop_state.ralpanda_dir,
                loop_state.tasks_file,
                loop_state.current_task_id,
                exit_code,
                loop_state.max_attempts,
                loop_state.history_file,
            )
            if finish and loop_state.current_task_id:
                _run_task_finished_hook(
                    loop_state,
                    loop_state.current_task_id,
                    finish.get("commit_sha"),
                )

            _post_task(loop_state)
            _finish_task(loop_state)

    # Poll review state machine
    if loop_state.review_state:
        done = agent.poll_review(
            loop_state.review_state,
            loop_state.ralpanda_dir,
            loop_state.tasks_file,
            loop_state.model,
            loop_state.history_file,
        )
        if done:
            # Determine exit behavior based on review results
            rs = loop_state.review_state
            fail_count = sum(1 for r in rs.check_results if r["status"] == "fail")
            infra_count = sum(1 for r in rs.check_results if r["status"] == "infra_fail")

            if fail_count == 0 and infra_count == 0:
                dag.update_task_status(loop_state.tasks_file, rs.task_id, "done")
            else:
                # Fix-ups/pause/clone were inserted by poll_review
                dag.update_task_status(loop_state.tasks_file, rs.task_id, "done")

            dag.log_event(loop_state.history_file, "task_completed", rs.task_id)

            # Commit tasks.json changes
            sha = git.commit_task(loop_state.tasks_file, rs.task_id)
            if sha:
                dag.log_event(loop_state.history_file, "committed", rs.task_id, f"sha={sha}")

            loop_state.review_state = None
            _run_task_finished_hook(loop_state, rs.task_id, sha)
            _post_task(loop_state)
            _finish_task(loop_state)

    # Handle paused state — check for resume sentinel
    if loop_state.state == "paused" and loop_state.current_task_id:
        resume_sentinel = loop_state.ralpanda_dir / "sentinels" / "resume"
        if resume_sentinel.exists():
            resume_sentinel.unlink()
            (loop_state.ralpanda_dir / "pause_reason").unlink(missing_ok=True)
            dag.update_task_status(
                loop_state.tasks_file, loop_state.current_task_id, "done",
            )
            dag.log_event(
                loop_state.history_file, "loop_resumed", loop_state.current_task_id,
            )
            loop_state.set_state("running", "resumed from pause")
            _write_state(loop_state, "running")
            _finish_task(loop_state)


def _run_task_finished_hook(
    loop_state: LoopState,
    task_id: str,
    commit_sha: str | None = None,
) -> None:
    """Emit task.finished for non-pause tasks that reach a final state."""
    loop_state.reload_tasks()
    task = dag.get_task(loop_state.tasks, task_id)
    if not task or task.get("type") == "pause":
        return

    payload = _event_payload(loop_state)
    payload["task"] = _task_payload(task)
    if commit_sha:
        payload["commit_sha"] = commit_sha

    hooks.run_event(
        "task.finished",
        payload,
        loop_state.ralpanda_dir,
        history_file=loop_state.history_file,
    )


def _run_loop_paused_hook(
    loop_state: LoopState,
    task: dict,
    pause_reason: str,
) -> None:
    loop_state.reload_tasks()
    paused_task = dag.get_task(loop_state.tasks, task["id"]) or task
    if "pause_reason" not in paused_task and pause_reason:
        paused_task = {**paused_task, "pause_reason": pause_reason}

    payload = _event_payload(loop_state)
    payload["task"] = _task_payload(paused_task)
    hooks.run_event(
        "loop.paused",
        payload,
        loop_state.ralpanda_dir,
        history_file=loop_state.history_file,
    )


def _run_loop_completed_hook(
    loop_state: LoopState,
    counts: dict[str, int],
    total: int,
) -> None:
    payload = _event_payload(loop_state, counts=counts, total=total)
    hooks.run_event(
        "loop.completed",
        payload,
        loop_state.ralpanda_dir,
        history_file=loop_state.history_file,
    )


def _event_payload(
    loop_state: LoopState,
    *,
    counts: dict[str, int] | None = None,
    total: int | None = None,
) -> dict:
    loop_state.reload_tasks()
    tasks = loop_state.tasks
    next_task = dag.get_next_task(tasks)
    loop = {
        "state": loop_state.state,
        "state_info": loop_state.state_info,
        "current_task_id": loop_state.current_task_id,
        "next_task_id": next_task["id"] if next_task else None,
        "counts": counts if counts is not None else dag.task_counts(tasks),
        "total_tasks": total if total is not None else len(tasks),
    }
    return {"loop": loop}


def _task_payload(task: dict) -> dict:
    keys = (
        "id",
        "title",
        "type",
        "status",
        "plan_source",
        "attempt",
        "created_at",
        "started_at",
        "completed_at",
        "pause_reason",
        "outcome",
        "usage",
    )
    return {key: task[key] for key in keys if key in task}


def _post_task(loop_state: LoopState) -> None:
    """Run post-task checks (integrity, runs_remaining)."""
    loop_state.reload_tasks()
    check = dag.validate_tasks(loop_state.tasks)
    if check != "valid":
        dag.log_event(
            loop_state.history_file, "integrity_check_failed",
            loop_state.current_task_id or "", check,
        )

    # Decrement runs_remaining
    loop_state.runs_remaining -= 1
    runs_file = loop_state.ralpanda_dir / "runs_remaining"
    runs_file.write_text(str(loop_state.runs_remaining))
    if loop_state.runs_remaining <= 0:
        loop_state.should_exit = True
        loop_state.exit_reason = "runs_exhausted"
        dag.log_event(loop_state.history_file, "runs_exhausted")


def _finish_task(loop_state: LoopState) -> None:
    """Clear current task tracking."""
    loop_state.current_task_id = None
    (loop_state.ralpanda_dir / "current_task").unlink(missing_ok=True)
    loop_state.reload_tasks()


def _write_state(loop_state: LoopState, state: str) -> None:
    """Write loop.state file."""
    (loop_state.ralpanda_dir / "loop.state").write_text(state)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup(loop_state: LoopState) -> None:
    """Clean up on exit."""
    # Kill agent if running
    if loop_state.agent_proc:
        agent.terminate_agent_process(loop_state.agent_proc)
        agent.finish_agent_process(loop_state.agent_proc)

    # Kill any review procs
    if loop_state.review_state:
        for proc in loop_state.review_state.parallel_procs.values():
            agent.terminate_agent_process(proc)
            agent.finish_agent_process(proc)
        if loop_state.review_state.current_isolated_proc:
            agent.terminate_agent_process(
                loop_state.review_state.current_isolated_proc,
            )
            agent.finish_agent_process(
                loop_state.review_state.current_isolated_proc,
            )
        if loop_state.review_state.coordinator_proc:
            agent.terminate_agent_process(loop_state.review_state.coordinator_proc)
            agent.finish_agent_process(loop_state.review_state.coordinator_proc)

    # Reset any task that was running back to pending so it can be retried
    if loop_state.current_task_id and loop_state.tasks_file.exists():
        dag.update_task_status(loop_state.tasks_file, loop_state.current_task_id, "pending")

    # Write state files
    _write_state(loop_state, "idle")
    for name in ("loop.pid", "current_task", "agent.pid", "pause_reason"):
        (loop_state.ralpanda_dir / name).unlink(missing_ok=True)

    dag.log_event(loop_state.history_file, "loop_stopped")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main_loop(stdscr, loop_state: LoopState) -> None:
    """The main tick loop inside curses.wrapper."""
    tui.init_colors()
    curses.halfdelay(5)  # getch returns ERR after 500ms
    curses.curs_set(0)   # hide cursor
    stdscr.keypad(True)  # enable special key sequences (arrows, etc.)

    tui_state = tui.TUIState(stdscr=stdscr)

    while not loop_state.should_exit:
        # 1. Handle input
        try:
            key = stdscr.getch()
        except curses.error:
            key = curses.ERR
        if key != curses.ERR:
            handle_input(key, tui_state, loop_state)

        # 2. Poll agents
        poll_agents(loop_state)

        # 3. Advance loop if idle
        if (
            loop_state.agent_proc is None
            and loop_state.review_state is None
            and loop_state.state in ("running", "waiting_tasks", "waiting_done", "waiting_blocked")
            and not loop_state.should_exit
        ):
            advance_loop(loop_state, tui_state)

        # 4. Tail log for the selected task (not just the running one)
        selected = tui_state._selected_task()
        tail_task_id = selected["id"] if selected else loop_state.current_task_id
        tui.tail_log(tui_state, loop_state.ralpanda_dir, tail_task_id)

        # 4b. Tail check log for review-style tasks
        if tui.is_check_task(selected):
            checks = dag.review_checks(selected)
            idx = tui_state.selected_check_idx
            if idx < len(checks):
                check_name = checks[idx].get("name", f"check-{idx}")
            elif selected.get("type") == "review" and idx == len(checks):
                check_name = "coordinator"
            else:
                check_name = None
            tui.tail_check_log(tui_state, loop_state.ralpanda_dir, selected["id"], check_name)

        # 5. Reload tasks if changed externally
        loop_state.maybe_reload_tasks()

        # 6. Render
        tui_state.render(loop_state)


def main() -> None:
    """Entry point."""
    ralpanda_dir = Path(os.environ.get("RALPANDA_DIR", ".ralpanda"))
    tasks_file = ralpanda_dir / "tasks.json"
    history_file = ralpanda_dir / "history.jsonl"

    config = load_config(ralpanda_dir)

    loop_state = LoopState(
        ralpanda_dir=ralpanda_dir,
        tasks_file=tasks_file,
        history_file=history_file,
        config=config,
    )

    # Validate
    error = validate_startup(loop_state)
    if error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)

    # Initialize
    for subdir in ("logs", "sentinels", "outcomes", "running"):
        (ralpanda_dir / subdir).mkdir(parents=True, exist_ok=True)
    (ralpanda_dir / "loop.pid").write_text(str(os.getpid()))
    _write_state(loop_state, loop_state.state)

    # Clear stale sentinels
    for name in ("exit", "resume"):
        (ralpanda_dir / "sentinels" / name).unlink(missing_ok=True)

    # Initialize runs_remaining
    runs_file = ralpanda_dir / "runs_remaining"
    if runs_file.exists():
        try:
            loop_state.runs_remaining = int(runs_file.read_text().strip())
        except ValueError:
            loop_state.runs_remaining = 1000
    else:
        runs_file.write_text("1000")

    dag.log_event(history_file, "loop_started")

    # Install signal handler
    original_sigint = signal.getsignal(signal.SIGINT)
    original_sigterm = signal.getsignal(signal.SIGTERM)

    def _signal_handler(signum, frame):
        loop_state.should_exit = True
        loop_state.exit_reason = "signal"

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        curses.wrapper(lambda stdscr: main_loop(stdscr, loop_state))
    finally:
        cleanup(loop_state)
        signal.signal(signal.SIGINT, original_sigint)
        signal.signal(signal.SIGTERM, original_sigterm)

    print(f"\nralpanda: loop finished ({loop_state.exit_reason or 'unknown'}).")
    # Print final counts
    try:
        data = dag.load_tasks(tasks_file)
        counts = dag.task_counts(data["tasks"])
        import json
        print(json.dumps(counts, indent=2))
    except Exception:
        pass


if __name__ == "__main__":
    main()
