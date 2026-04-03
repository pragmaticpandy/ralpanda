"""Entry point for ralpanda: main tick loop with integrated curses TUI."""

from __future__ import annotations

import curses
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import dag, agent, git, prompt, tui


@dataclass
class LoopState:
    """All mutable state for the orchestration loop."""
    ralpanda_dir: Path
    tasks_file: Path
    history_file: Path
    config: dict
    tasks: list[dict] = field(default_factory=list)
    tasks_mtime: float = 0.0
    state: str = "running"  # running | paused | waiting_dirty | waiting_done | waiting_blocked
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
    _REQUIRES_INFO = frozenset({"waiting_blocked", "waiting_dirty", "waiting_done", "paused", "idle"})

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
            pass

    def maybe_reload_tasks(self) -> None:
        """Reload tasks if the file has been modified externally."""
        try:
            mtime = self.tasks_file.stat().st_mtime
        except (FileNotFoundError, OSError):
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
    return config


def validate_startup(loop_state: LoopState) -> str | None:
    """Run startup checks. Returns error message or None if OK."""
    if not loop_state.tasks_file.exists():
        return f"{loop_state.tasks_file} not found. Run /ralpanda first to set up."

    loop_state.reload_tasks()

    # Reset any stale "running" tasks from a previous crashed loop
    stale_count = 0
    for t in loop_state.tasks:
        if t["status"] == "running":
            dag.update_task_status(loop_state.tasks_file, t["id"], "pending")
            stale_count += 1
    if stale_count:
        loop_state.reload_tasks()

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

    # Determine max panels: 3 if selected task is a review, else 2
    selected = tui_state._selected_task()
    is_review = selected and selected.get("type") == "review"
    max_panels = 3 if is_review else 2

    if key == ord("\t"):
        # Cycle focus: 0 (task list) -> 1 (detail/check list) -> 2 (check log, review only) -> 0
        tui_state.focus_panel = (tui_state.focus_panel + 1) % max_panels
        # Reset scroll for the panel we just left
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
            tui_state.detail_scroll = 0

    elif key == curses.KEY_DOWN:
        if tui_state.focus_panel == 1 and is_review:
            # Navigate check list (count includes coordinator entry)
            check_count = len(selected.get("checks", [])) + 1  # +1 for coordinator
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
        # Insert pause
        selected = tui_state._selected_task()
        if selected and selected["status"] == "pending":
            dag.insert_pause_before(loop_state.tasks_file, selected["id"])
            loop_state.reload_tasks()
        else:
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


# ---------------------------------------------------------------------------
# Loop advancement
# ---------------------------------------------------------------------------

def advance_loop(loop_state: LoopState, tui_state: tui.TUIState) -> None:
    """Try to dispatch the next task. Only called when no agent is running."""
    loop_state.reload_tasks()
    tasks = loop_state.tasks

    # Check git is clean
    if not git.is_clean():
        dirty_info = git.dirty_summary()
        if loop_state.state != "waiting_dirty":
            _write_state(loop_state, "waiting_dirty")
            dag.log_event(loop_state.history_file, "waiting_dirty")
        loop_state.set_state("waiting_dirty", dirty_info)
        return

    if loop_state.state == "waiting_dirty":
        loop_state.set_state("running", f"task {loop_state.current_task_id or 'next'}")
        _write_state(loop_state, "running")
        dag.log_event(loop_state.history_file, "dirty_resolved")

    # Check sentinels
    exit_sentinel = loop_state.ralpanda_dir / "sentinels" / "exit"
    if exit_sentinel.exists():
        loop_state.should_exit = True
        loop_state.exit_reason = "sentinel"
        dag.log_event(loop_state.history_file, "loop_exit_sentinel")
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
        else:
            loop_state.set_state("waiting_blocked", dag.blocked_reason(tasks))
        return

    loop_state.set_state("running", next_task["id"])
    _write_state(loop_state, "running")
    loop_state.iteration += 1

    task_id = next_task["id"]
    task_type = next_task.get("type", "work")
    loop_state.current_task_id = task_id
    (loop_state.ralpanda_dir / "current_task").write_text(task_id)

    # Update status and increment attempt
    dag.update_task_status(loop_state.tasks_file, task_id, "running")
    dag.increment_attempt(loop_state.tasks_file, task_id)
    loop_state.reload_tasks()

    attempt = next_task.get("attempt", 0) + 1
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
        _finish_task(loop_state)

    elif task_type == "pause":
        pause_reason = next_task.get("pause_reason") or next_task.get("title", "manual pause")
        loop_state.set_state("paused", f"{task_id}: {pause_reason}")
        _write_state(loop_state, "paused")
        if pause_reason:
            (loop_state.ralpanda_dir / "pause_reason").write_text(pause_reason)
        dag.log_event(loop_state.history_file, "loop_paused", task_id, pause_reason)

    elif task_type == "review":
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
            _finish_task(loop_state)

    else:
        # Work task
        task_prompt = prompt.build_work_prompt(
            next_task, tasks, loop_state.ralpanda_dir,
        )
        log_path = dag.task_log_path(loop_state.ralpanda_dir, task_id)
        proc = agent.spawn_agent(task_prompt, loop_state.model, log_path)
        loop_state.agent_proc = proc
        (loop_state.ralpanda_dir / "agent.pid").write_text(str(proc.pid))


def poll_agents(loop_state: LoopState) -> None:
    """Poll running agent or review state machine."""
    # Handle force quit
    if loop_state.force_quit and loop_state.agent_proc:
        loop_state.agent_proc.terminate()
        try:
            loop_state.agent_proc.wait(timeout=5)
        except Exception:
            loop_state.agent_proc.kill()
        agent.close_agent(loop_state.agent_proc)
        loop_state.agent_proc = None
        _finish_task(loop_state)
        return

    # Poll work agent
    if loop_state.agent_proc and not loop_state.review_state:
        exit_code = loop_state.agent_proc.poll()
        if exit_code is not None:
            agent.close_agent(loop_state.agent_proc)
            loop_state.agent_proc = None
            (loop_state.ralpanda_dir / "agent.pid").unlink(missing_ok=True)

            agent.process_work_result(
                loop_state.ralpanda_dir,
                loop_state.tasks_file,
                loop_state.current_task_id,
                exit_code,
                loop_state.max_attempts,
                loop_state.history_file,
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
        try:
            loop_state.agent_proc.terminate()
            loop_state.agent_proc.wait(timeout=5)
        except Exception:
            try:
                loop_state.agent_proc.kill()
            except Exception:
                pass
        agent.close_agent(loop_state.agent_proc)

    # Kill any review procs
    if loop_state.review_state:
        for proc in loop_state.review_state.parallel_procs.values():
            try:
                proc.terminate()
            except Exception:
                pass
            agent.close_agent(proc)
        if loop_state.review_state.current_isolated_proc:
            try:
                loop_state.review_state.current_isolated_proc.terminate()
            except Exception:
                pass
            agent.close_agent(loop_state.review_state.current_isolated_proc)
        if loop_state.review_state.coordinator_proc:
            try:
                loop_state.review_state.coordinator_proc.terminate()
            except Exception:
                pass
            agent.close_agent(loop_state.review_state.coordinator_proc)

    # Reset any task that was running back to pending so it can be retried
    if loop_state.current_task_id:
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
            and loop_state.state in ("running", "waiting_dirty", "waiting_done", "waiting_blocked")
            and not loop_state.should_exit
        ):
            advance_loop(loop_state, tui_state)

        # 4. Tail log for the selected task (not just the running one)
        selected = tui_state._selected_task()
        tail_task_id = selected["id"] if selected else loop_state.current_task_id
        tui.tail_log(tui_state, loop_state.ralpanda_dir, tail_task_id)

        # 4b. Tail check log for review tasks
        if selected and selected.get("type") == "review":
            checks = selected.get("checks", [])
            idx = tui_state.selected_check_idx
            if idx < len(checks):
                check_name = checks[idx].get("name", f"check-{idx}")
            elif idx == len(checks):
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
    for subdir in ("logs", "sentinels", "outcomes"):
        (ralpanda_dir / subdir).mkdir(parents=True, exist_ok=True)
    (ralpanda_dir / "loop.pid").write_text(str(os.getpid()))
    _write_state(loop_state, "running")

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
