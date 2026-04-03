"""Curses TUI: two-column layout with grouped task list and scrollable detail panel.

For review tasks, the right pane splits into a check list + check detail/log.
"""

from __future__ import annotations

import curses
import json
import time
from dataclasses import dataclass, field
from pathlib import Path


# Task type -> (color_pair_id, icon)
TYPE_DISPLAY = {
    "work":           (1, "w"),
    "review":         (2, "r"),
    "pause":          (3, "p"),
    "delete_base_sha": (5, "g"),
}

# Status icons (overlaid on type color)
STATUS_ICON = {
    "done":    "+",
    "running": ">",
    "pending": "o",
    "failed":  "x",
    "split":   "~",
    "paused":  "=",
}

# Loop state -> label (color is determined by status bar logic)
LOOP_STATE_LABEL = {
    "running":         "RUNNING",
    "paused":          "PAUSED",
    "idle":            "IDLE",
    "waiting_dirty":   "DIRTY",
    "waiting_done":    "ALL DONE",
    "waiting_blocked": "BLOCKED",
}

# Section headers for grouped task list
_SECTION_RUNNING = "RUNNING"
_SECTION_PENDING = "PENDING"
_SECTION_DONE = "DONE"


def init_colors() -> None:
    """Initialize curses color pairs."""
    curses.start_color()
    curses.use_default_colors()
    # Task type colors (foreground)
    curses.init_pair(1, curses.COLOR_CYAN, -1)      # work
    curses.init_pair(2, curses.COLOR_MAGENTA, -1)   # review
    curses.init_pair(3, curses.COLOR_YELLOW, -1)    # pause
    curses.init_pair(5, curses.COLOR_BLUE, -1)     # delete_base_sha (gate)
    # Override color for failed status (regardless of type)
    curses.init_pair(4, curses.COLOR_RED, -1)       # failed
    # Status bar backgrounds
    curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_GREEN)  # loop running
    curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_YELLOW) # loop paused
    curses.init_pair(10, curses.COLOR_BLACK, curses.COLOR_RED)   # loop blocked/idle
    # Detail panel: check pass/fail
    curses.init_pair(11, curses.COLOR_GREEN, -1)    # check pass
    curses.init_pair(12, curses.COLOR_YELLOW, -1)   # check warn


def _dag_depth(tasks: list[dict]) -> dict[str, int]:
    """Compute longest path from roots for each task (topological depth)."""
    deps = {t["id"]: t.get("depends_on", []) for t in tasks}
    cache: dict[str, int] = {}

    def depth(tid: str) -> int:
        if tid in cache:
            return cache[tid]
        cache[tid] = -1  # cycle guard
        d = 0
        for dep in deps.get(tid, []):
            d = max(d, depth(dep) + 1)
        cache[tid] = d
        return d

    for t in tasks:
        depth(t["id"])
    return cache


def _build_display_list(tasks: list[dict]) -> list[dict | str]:
    """Build the grouped display list: section headers + tasks.

    Order: PENDING (last-to-execute first), RUNNING (current), DONE (most recent first).
    Returns a mixed list of section header strings and task dicts.
    """
    running = [t for t in tasks if t["status"] == "running"]
    pending = [t for t in tasks if t["status"] == "pending"]
    done = [t for t in tasks if t["status"] in ("done", "failed", "split")]

    # Pending: deepest DAG depth first (last to execute at top),
    # break ties by array order (reversed so later tasks appear first)
    depths = _dag_depth(tasks)
    task_order = {t["id"]: i for i, t in enumerate(tasks)}
    pending.sort(key=lambda t: (-depths.get(t["id"], 0), -task_order.get(t["id"], 0)))
    # Done: most recently completed first (reverse by completed_at),
    # break ties by array order so instant tasks (gates) that ran later sort first
    done.sort(key=lambda t: (t.get("completed_at") or "", task_order.get(t["id"], 0)), reverse=True)

    _SPACER = ""
    items: list[dict | str] = []
    if pending:
        items.append(_SECTION_PENDING)
        items.extend(pending)
        items.append(_SPACER)
    if running:
        items.append(_SECTION_RUNNING)
        items.extend(running)
        items.append(_SPACER)
    if done:
        items.append(_SECTION_DONE)
        items.extend(done)
    return items


@dataclass
class TUIState:
    """Rendering state for the curses TUI."""
    stdscr: object  # curses.window (no type stub)
    selected_idx: int = 0
    scroll_offset: int = 0
    detail_scroll: int = 0  # scroll offset for detail panel
    log_lines: list[str] = field(default_factory=list)
    log_file_pos: int = 0
    auto_follow: bool = True
    focus_panel: int = 0  # 0=task list, 1=detail/check list, 2=check log (review only)
    # Track which task's log we're tailing (to reset pos on task change)
    _tailing_task_id: str = ""
    # Track selected task's status to reset detail_scroll on status change
    _detail_task_status: str = ""
    # Cached display list (rebuilt each render)
    _display_list: list = field(default_factory=list)

    # -- Review check panel state --
    selected_check_idx: int = 0
    check_detail_scroll: int = 0
    check_log_lines: list = field(default_factory=list)
    check_log_file_pos: int = 0
    _tailing_check_id: str = ""  # "{task_id}:{check_name}" to detect changes

    def safe_addstr(self, y: int, x: int, text: str, attr: int = 0, max_width: int = 0) -> None:
        """Write text to screen, truncating to fit and avoiding curses errors."""
        scr = self.stdscr
        height, width = scr.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        avail = width - x
        if max_width > 0:
            avail = min(avail, max_width)
        if avail <= 0:
            return
        text = text[:avail]
        try:
            scr.addstr(y, x, text, attr)
        except curses.error:
            pass  # Writing to bottom-right corner raises

    def _selected_task(self) -> dict | None:
        """Return the currently selected task (skipping section headers)."""
        if not self._display_list:
            return None
        # Map selected_idx to actual task (skip headers)
        task_idx = 0
        for item in self._display_list:
            if isinstance(item, str):
                continue
            if task_idx == self.selected_idx:
                return item
            task_idx += 1
        return None

    def render(self, loop_state) -> None:
        """Full screen render. Called every tick (~500ms)."""
        scr = self.stdscr
        scr.erase()
        height, width = scr.getmaxyx()

        if height < 5 or width < 40:
            self.safe_addstr(0, 0, "Terminal too small", curses.A_BOLD)
            scr.noutrefresh()
            curses.doupdate()
            return

        # Build grouped display list
        self._display_list = _build_display_list(loop_state.tasks)

        # Reserve bottom 2 lines for status bar
        status_y = height - 2
        content_height = status_y

        # Split: left 40%, right 60%
        left_width = max(25, width * 2 // 5)
        right_x = left_width + 1
        right_width = width - right_x

        self._render_task_list(loop_state, 0, 0, left_width, content_height)
        self._render_divider(left_width, 0, content_height)

        # For review tasks, split the right pane into check list + check detail
        selected = self._selected_task()
        if selected and selected.get("type") == "review":
            # Three-column: task list | check list | check detail/log
            mid_width = max(20, right_width * 2 // 5)
            far_x = right_x + mid_width + 1
            far_width = width - far_x

            self._render_check_list(loop_state, selected, right_x, 0, mid_width, content_height)
            self._render_divider(right_x + mid_width, 0, content_height)
            self._render_check_detail(loop_state, selected, far_x, 0, far_width, content_height)
        else:
            # Clamp focus_panel for non-review tasks
            if self.focus_panel > 1:
                self.focus_panel = 1
            self._render_detail_panel(loop_state, right_x, 0, right_width, content_height)

        self._render_status_bar(loop_state, 0, status_y, width)

        scr.noutrefresh()
        curses.doupdate()

    def _render_task_list(self, loop_state, x: int, y: int, width: int, height: int) -> None:
        """Render grouped task list with section headers."""
        display = self._display_list

        if not display:
            self.safe_addstr(y, x, " No tasks loaded", curses.A_DIM, width)
            return

        # Count actual tasks (not headers) for index clamping
        task_count = sum(1 for item in display if not isinstance(item, str))
        if task_count == 0:
            return

        # Auto-follow running task
        if self.auto_follow and loop_state.current_task_id:
            idx = 0
            for item in display:
                if isinstance(item, str):
                    continue
                if item["id"] == loop_state.current_task_id:
                    self.selected_idx = idx
                    break
                idx += 1

        # Clamp selected index
        self.selected_idx = max(0, min(self.selected_idx, task_count - 1))

        # Build flat list of (is_header, label_or_task, is_selected) for rendering
        rows: list[tuple] = []  # (type, data, is_selected)
        task_idx = 0
        for item in display:
            if isinstance(item, str):
                rows.append(("header", item, False))
            else:
                is_sel = (task_idx == self.selected_idx)
                rows.append(("task", item, is_sel))
                task_idx += 1

        # Find which row the selected task is at (for scroll)
        selected_row = 0
        for i, (typ, _, is_sel) in enumerate(rows):
            if is_sel:
                selected_row = i
                break

        # Scrolling
        if selected_row < self.scroll_offset:
            self.scroll_offset = selected_row
        elif selected_row >= self.scroll_offset + height:
            self.scroll_offset = selected_row - height + 1
        # Keep headers visible: if header is right above selected, include it
        if self.scroll_offset > 0 and selected_row == self.scroll_offset:
            if rows[self.scroll_offset - 1][0] == "header":
                self.scroll_offset -= 1

        for row_i in range(height):
            ri = self.scroll_offset + row_i
            if ri >= len(rows):
                break
            typ, data, is_sel = rows[ri]

            if typ == "header":
                if not data:
                    continue  # spacer — blank line
                label = f" ── {data} ──"
                self.safe_addstr(y + row_i, x, label.ljust(width)[:width], curses.A_BOLD | curses.A_DIM, width)
            else:
                task = data
                status = task.get("status", "pending")
                task_type = task.get("type", "work")
                icon = STATUS_ICON.get(status, "?")

                # Color: failed overrides type color; otherwise use type
                if status == "failed":
                    pair_id = 4
                else:
                    pair_id = TYPE_DISPLAY.get(task_type, (1, "w"))[0]

                parts = task["id"].split("/")
                short_id = parts[-1] if len(parts) > 1 else task["id"]
                line = f" {icon} {short_id} {task.get('title', '')}"

                # Right-aligned metadata columns: tokens + duration
                dur = _fmt_duration(task)
                tok = _fmt_tokens(task)
                suffix = ""
                if tok:
                    suffix += f" {tok}"
                if dur:
                    suffix += f" {dur}"
                if suffix:
                    suffix += " "
                    max_text = width - len(suffix)
                    if max_text > 10:
                        line = line[:max_text].ljust(max_text) + suffix
                    else:
                        line = line.ljust(width)[:width]
                else:
                    line = line.ljust(width)[:width]

                color = curses.color_pair(pair_id)
                attr = curses.A_REVERSE if is_sel else 0
                if status == "running":
                    attr |= curses.A_BOLD
                elif status in ("done", "split"):
                    pass  # normal weight

                self.safe_addstr(y + row_i, x, line[:width], color | attr, width)

    def _render_divider(self, x: int, y: int, height: int) -> None:
        """Render vertical divider."""
        for row in range(height):
            self.safe_addstr(y + row, x, "│", curses.A_DIM)

    def _render_detail_panel(self, loop_state, x: int, y: int, width: int, height: int) -> None:
        """Render the right-side detail panel.

        Layout: metadata at top, then log below (newest first).
        Tab key toggles focus to detail panel for scrolling.
        """
        task = self._selected_task()
        if not task:
            self.safe_addstr(y, x, " No task selected", curses.A_DIM, width)
            return

        task_id = task["id"]
        status = task.get("status", "pending")
        task_type = task.get("type", "work")

        # Reset detail scroll when the selected task's status changes
        # (e.g., running->done adds outcome content, shifting lines)
        task_status_key = f"{task_id}:{status}"
        if task_status_key != self._detail_task_status:
            self._detail_task_status = task_status_key
            self.detail_scroll = 0

        if status == "failed":
            pair_id = 4
        else:
            pair_id = TYPE_DISPLAY.get(task_type, (1, "w"))[0]

        # Build all detail lines first, then render with scroll
        lines: list[tuple[str, int]] = []  # (text, attr)

        # -- Metadata section --
        lines.append((f" {task_id}", curses.A_BOLD))
        lines.append((f" {task.get('title', '')}", curses.color_pair(pair_id)))

        dur = _fmt_duration(task)
        status_text = f" [{status}]  type:{task.get('type', 'work')}  attempt:{task.get('attempt', 0)}"
        if dur:
            status_text += f"  time:{dur}"
        lines.append((status_text, curses.A_DIM))

        # -- Plan source --
        plan_source = task.get("plan_source", "")
        if plan_source:
            lines.append((f" Plan: {plan_source}", curses.A_DIM))

        # -- Dependencies --
        deps = task.get("depends_on", [])
        if deps:
            dep_strs = [d.split("/")[-1] for d in deps]
            lines.append((f" Deps: {', '.join(dep_strs)}", curses.A_DIM))

        # -- Description --
        lines.append(("", 0))
        desc = task.get("description", "")
        lines.append((" Description:", curses.A_BOLD))
        if desc:
            for wl in _wrap(desc, width - 3):
                lines.append((f"  {wl}", 0))
        else:
            lines.append(("  (empty)", curses.A_DIM))

        # -- Acceptance criteria --
        criteria = task.get("acceptance_criteria", [])
        lines.append((" Criteria:", curses.A_BOLD))
        if criteria:
            for c in criteria:
                lines.append((f"  • {c}", 0))
        else:
            lines.append(("  (empty)", curses.A_DIM))

        # -- Outcome --
        outcome = task.get("outcome")
        lines.append(("", 0))
        lines.append((" Outcome:", curses.A_BOLD))
        if outcome and isinstance(outcome, dict):
            summary = outcome.get("summary", "")
            if summary:
                lines.append((" Summary:", curses.A_BOLD))
                for wl in _wrap(summary, width - 3):
                    lines.append((f"  {wl}", 0))
            else:
                lines.append(("  summary: (empty)", curses.A_DIM))

            files = outcome.get("files_changed", [])
            lines.append((" Files:", curses.A_BOLD))
            if files:
                for f in files[:10]:
                    lines.append((f"  • {f}", 0))
            else:
                lines.append(("  (none)", curses.A_DIM))

            decisions = outcome.get("decisions", [])
            lines.append((" Decisions:", curses.A_BOLD))
            if decisions:
                for d in decisions[:5]:
                    lines.append((f"  • {d.get('what', '')}", 0))
                    why = d.get("why", "")
                    if why:
                        lines.append((f"    → {why}", curses.A_DIM))
            else:
                lines.append(("  (none)", curses.A_DIM))

            check_results = outcome.get("check_results", [])
            lines.append((" Checks:", curses.A_BOLD))
            if check_results:
                for cr in check_results:
                    name = cr.get("name", "?")
                    st = cr.get("status", "?")
                    if st == "pass":
                        lines.append((f"  ✓ {name}", curses.color_pair(11)))
                    elif st == "fail":
                        lines.append((f"  ✗ {name}", curses.color_pair(4)))
                    else:
                        lines.append((f"  ⚠ {name}", curses.color_pair(12)))
            else:
                lines.append(("  (none)", curses.A_DIM))
        else:
            lines.append(("  (empty)", curses.A_DIM))

        # -- Usage --
        lines.append(("", 0))
        usage = task.get("usage")
        if usage and isinstance(usage, dict):
            parts = []
            peak = usage.get("peak_context", 0)
            if peak:
                parts.append(f"ctx:{peak / 1_000_000 * 100:.0f}%")
            cost = usage.get("cost_usd")
            if cost is not None:
                parts.append(f"${cost:.2f}")
            output_tok = usage.get("output_tokens", 0)
            if output_tok:
                parts.append(f"out:{output_tok:,}")
            lines.append((f" Usage: {' | '.join(parts) if parts else '(empty)'}", curses.A_DIM))
        else:
            lines.append((" Usage: (empty)", curses.A_DIM))

        # -- Divider --
        lines.append((" " + "─" * (width - 2), curses.A_DIM))

        # -- Log section (newest first) --
        log_path_str = _task_log_path_str(loop_state.ralpanda_dir, task_id)
        lines.append((f" Agent log: {log_path_str}", curses.A_BOLD))
        if self.log_lines:
            lines.append(("  (text + tool_use only, newest first)", curses.A_DIM))
            _render_log_lines(self.log_lines, lines, width)
        else:
            lines.append(("  (empty)", curses.A_DIM))

        # -- Render with scroll --
        # Clamp detail_scroll
        max_scroll = max(0, len(lines) - height)
        self.detail_scroll = max(0, min(self.detail_scroll, max_scroll))

        # Focus indicator
        if self.focus_panel == 1:
            self.safe_addstr(y, x + width - 4, " ◀ ", curses.A_BOLD | curses.color_pair(2))

        for i in range(height):
            li = self.detail_scroll + i
            if li >= len(lines):
                break
            text, attr = lines[li]
            self.safe_addstr(y + i, x, text[:width], attr, width)

    def _render_check_list(self, loop_state, task: dict, x: int, y: int, width: int, height: int) -> None:
        """Render the check list for a review task (middle pane)."""
        checks = task.get("checks", [])
        task_id = task["id"]
        status = task.get("status", "pending")
        outcome = task.get("outcome")

        # Build check entries: each check + coordinator at bottom
        entries: list[dict] = []
        for i, check in enumerate(checks):
            name = check.get("name", f"check-{i}")
            mode = check.get("mode", "isolated")

            # Determine live status
            if outcome and isinstance(outcome, dict):
                # Review is done — use outcome check_results
                cr = next((r for r in outcome.get("check_results", []) if r["name"] == name), None)
                check_status = cr["status"] if cr else "unknown"
            elif status == "running":
                # Infer from log file existence + verdict
                check_status = _infer_check_status(loop_state.ralpanda_dir, task_id, name)
            else:
                check_status = "pending"

            entries.append({"name": name, "mode": mode, "status": check_status, "is_coordinator": False})

        # Coordinator entry
        if outcome and isinstance(outcome, dict):
            has_failures = any(r["status"] == "fail" for r in outcome.get("check_results", []))
            coord_status = "done" if has_failures else "skipped"
            # Check if coordinator log exists even if we think it's skipped
            coord_log = _check_log_path(loop_state.ralpanda_dir, task_id, "coordinator")
            if coord_log.exists() and coord_status == "skipped":
                coord_status = "done"
        elif status == "running":
            coord_status = _infer_check_status(loop_state.ralpanda_dir, task_id, "coordinator")
            if coord_status == "pending":
                coord_status = "waiting"
        else:
            coord_status = "waiting"
        entries.append({"name": "coordinator", "mode": "", "status": coord_status, "is_coordinator": True})

        # Clamp selected_check_idx
        self.selected_check_idx = max(0, min(self.selected_check_idx, len(entries) - 1))

        # Header
        dur = _fmt_duration(task)
        header = f" Review: {task_id.split('/')[-1]}"
        if dur:
            header += f"  ({dur})"
        self.safe_addstr(y, x, header[:width].ljust(width), curses.A_BOLD | curses.color_pair(2), width)

        # Focus indicator
        if self.focus_panel == 1:
            self.safe_addstr(y, x + width - 4, " ◀ ", curses.A_BOLD | curses.color_pair(2))

        # Render entries
        for i, entry in enumerate(entries):
            row = y + 1 + i
            if row >= y + height:
                break

            is_sel = (i == self.selected_check_idx)
            name = entry["name"]
            cs = entry["status"]

            # Icon and color
            if cs == "pass":
                icon, color = "✓", curses.color_pair(11)
            elif cs == "fail":
                icon, color = "✗", curses.color_pair(4)
            elif cs == "infra_fail":
                icon, color = "⚠", curses.color_pair(12)
            elif cs == "running":
                icon, color = "▸", curses.color_pair(2) | curses.A_BOLD
            elif cs in ("skipped", "waiting"):
                icon, color = "·", curses.A_DIM
            elif cs == "done":
                icon, color = "+", curses.color_pair(11)
            else:  # pending
                icon, color = "○", curses.A_DIM

            if entry["is_coordinator"]:
                # Visual separator before coordinator
                if row > y + 1 and row - 1 < y + height:
                    pass  # entries are rendered sequentially, separator via label
                label = f" {icon} ── coordinator ──"
            else:
                mode_tag = f" [{entry['mode'][:1]}]" if entry["mode"] else ""
                label = f" {icon} {name}{mode_tag}"

            attr = curses.A_REVERSE if is_sel else 0
            self.safe_addstr(row, x, label.ljust(width)[:width], color | attr, width)

    def _render_check_detail(self, loop_state, task: dict, x: int, y: int, width: int, height: int) -> None:
        """Render detail/log for the selected check (far right pane)."""
        checks = task.get("checks", [])
        task_id = task["id"]
        outcome = task.get("outcome")

        # Determine which entry is selected
        entry_count = len(checks) + 1  # checks + coordinator
        idx = max(0, min(self.selected_check_idx, entry_count - 1))
        is_coordinator = (idx == len(checks))

        if is_coordinator:
            check_name = "coordinator"
            check_prompt = ""
            check_mode = ""
        else:
            check = checks[idx]
            check_name = check.get("name", f"check-{idx}")
            check_prompt = check.get("prompt", "")
            check_mode = check.get("mode", "isolated")

        lines: list[tuple[str, int]] = []

        # Header
        lines.append((f" {check_name}", curses.A_BOLD))

        if is_coordinator:
            lines.append((" Fix-up task generator", curses.A_DIM))
            # Status
            coord_log = _check_log_path(loop_state.ralpanda_dir, task_id, "coordinator")
            if coord_log.exists():
                lines.append((" Status: ran", curses.color_pair(11)))
            elif outcome:
                has_failures = any(
                    r["status"] == "fail"
                    for r in (outcome.get("check_results", []) if isinstance(outcome, dict) else [])
                )
                if has_failures:
                    lines.append((" Status: expected but no log found", curses.color_pair(12)))
                else:
                    lines.append((" Status: did not need to run (all checks passed)", curses.A_DIM))
            else:
                lines.append((" Status: waiting for checks to complete", curses.A_DIM))
        else:
            lines.append((f" Mode: {check_mode}", curses.A_DIM))

            # Check status from outcome
            if outcome and isinstance(outcome, dict):
                cr = next((r for r in outcome.get("check_results", []) if r["name"] == check_name), None)
                if cr:
                    st = cr["status"]
                    if st == "pass":
                        lines.append((" Status: PASS", curses.color_pair(11)))
                    elif st == "fail":
                        lines.append((" Status: FAIL", curses.color_pair(4)))
                        detail = cr.get("detail", "")
                        if detail:
                            lines.append(("", 0))
                            lines.append((" Failure detail:", curses.A_BOLD))
                            for wl in _wrap(detail, width - 3):
                                lines.append((f"  {wl}", curses.color_pair(4)))
                    else:
                        lines.append((f" Status: {st.upper()}", curses.color_pair(12)))
                        detail = cr.get("detail", "")
                        if detail:
                            lines.append(("", 0))
                            lines.append((" Detail:", curses.A_BOLD))
                            for wl in _wrap(detail, width - 3):
                                lines.append((f"  {wl}", curses.color_pair(12)))

            # Prompt
            if check_prompt:
                lines.append(("", 0))
                lines.append((" Prompt:", curses.A_BOLD))
                for wl in _wrap(check_prompt, width - 3):
                    lines.append((f"  {wl}", curses.A_DIM))

        # Divider before log
        lines.append(("", 0))
        lines.append((" " + "─" * (width - 2), curses.A_DIM))

        # Log section
        log_path = _check_log_path(loop_state.ralpanda_dir, task_id, check_name)
        lines.append((f" Log: {log_path}", curses.A_BOLD))

        if self.check_log_lines:
            lines.append(("  (newest first)", curses.A_DIM))
            _render_log_lines(self.check_log_lines, lines, width)
        elif not log_path.exists():
            lines.append(("  (no log file yet)", curses.A_DIM))
        else:
            lines.append(("  (empty)", curses.A_DIM))

        # Render with scroll
        max_scroll = max(0, len(lines) - height)
        self.check_detail_scroll = max(0, min(self.check_detail_scroll, max_scroll))

        # Focus indicator
        if self.focus_panel == 2:
            self.safe_addstr(y, x + width - 4, " ◀ ", curses.A_BOLD | curses.color_pair(2))

        for i in range(height):
            li = self.check_detail_scroll + i
            if li >= len(lines):
                break
            text, attr = lines[li]
            self.safe_addstr(y + i, x, text[:width], attr, width)

    def _render_status_bar(self, loop_state, x: int, y: int, width: int) -> None:
        """Render the bottom status bar (2 lines).

        Color changes based on loop state:
        - running: black on green
        - paused: black on yellow
        - blocked/stopped/idle: black on red
        """
        state = loop_state.state
        label = LOOP_STATE_LABEL.get(state, state.upper())

        if state == "paused":
            bar_attr = curses.color_pair(9) | curses.A_BOLD
        elif state == "running":
            bar_attr = curses.color_pair(8) | curses.A_BOLD
        else:
            # blocked, dirty, idle, done — all use red bar
            bar_attr = curses.color_pair(10) | curses.A_BOLD

        # Line 1: state badge + state_info + iteration
        line1 = f" {label}"
        info = getattr(loop_state, "state_info", "")
        if info:
            line1 += f"  ▸ {info}"
        elif loop_state.current_task_id:
            line1 += f"  ▸ {loop_state.current_task_id}"
        self.safe_addstr(y, x, line1.ljust(width)[:width], bar_attr, width)

        # Line 2: total time + base SHA (left) | key hints (right)
        # Use width-1 on the last terminal row to avoid bottom-right corner curses bug
        total_secs = _total_completed_time(loop_state.tasks)
        if total_secs > 0:
            if total_secs < 60:
                time_str = f"{total_secs}s"
            else:
                mins, s = divmod(total_secs, 60)
                if mins < 60:
                    time_str = f"{mins}m{s:02d}s"
                else:
                    hrs, m = divmod(mins, 60)
                    time_str = f"{hrs}h{m:02d}m"
            left = f" Total: {time_str}"
        else:
            left = ""

        from . import git
        base_sha = git.get_base_sha(loop_state.ralpanda_dir)
        sha_part = f"Base: {base_sha[:7] if base_sha else 'not set'}"
        if left:
            left += f"  {sha_part}"
        else:
            left = f" {sha_part}"

        hints = "q:quit  Q:force kill  p:pause  r:resume  Tab:focus  ↑↓:nav"
        pad = width - 1 - len(left) - len(hints)
        if pad > 2:
            line2 = left + " " * pad + hints
        else:
            line2 = left
        line2 = line2.ljust(width - 1)[:width - 1]

        self.safe_addstr(y + 1, x, line2, bar_attr, width - 1)
        # Fill the bottom-right corner cell using insch to avoid the curses addstr bug
        try:
            self.stdscr.insch(y + 1, width - 1, " ", bar_attr)
        except curses.error:
            pass


# ---------------------------------------------------------------------------
# Log tailing
# ---------------------------------------------------------------------------

def _tail_log_file(
    log_path: Path,
    lines: list,
    file_pos: int,
) -> int:
    """Incrementally read new entries from a stream-json log file.

    Appends (timestamp, text) tuples to `lines`. Returns the new file position.
    """
    if not log_path.exists():
        return file_pos

    try:
        with open(log_path, "r", errors="replace") as f:
            f.seek(file_pos)
            new_data = f.read()
            file_pos = f.tell()
    except OSError:
        return file_pos

    if not new_data:
        return file_pos

    for line in new_data.strip().split("\n"):
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        ts = time.strftime("%H:%M:%S")

        msg = obj.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") == "text":
                text_lines = [l for l in block["text"].split("\n") if l.strip()]
                for i, tl in enumerate(text_lines):
                    prefix = ts if i == 0 else ""
                    lines.append((prefix, tl))
            elif block.get("type") == "tool_use":
                tool = block.get("name", "")
                inp = json.dumps(block.get("input", {}))[:200]
                lines.append((ts, f"[tool: {tool}] {inp}"))

    # Keep bounded
    if len(lines) > 2000:
        del lines[:-1000]

    return file_pos


def tail_log(tui_state: TUIState, ralpanda_dir: Path, task_id: str | None) -> None:
    """Incrementally read new lines from the active task's log file."""
    if not task_id:
        return

    if task_id != tui_state._tailing_task_id:
        tui_state._tailing_task_id = task_id
        tui_state.log_file_pos = 0
        tui_state.log_lines = []

    safe_id = task_id.replace("/", "-")
    log_path = ralpanda_dir / "logs" / f"{safe_id}.jsonl"
    tui_state.log_file_pos = _tail_log_file(log_path, tui_state.log_lines, tui_state.log_file_pos)


def tail_check_log(tui_state: TUIState, ralpanda_dir: Path, task_id: str | None, check_name: str | None) -> None:
    """Incrementally read new lines from a check subagent's log file."""
    if not task_id or not check_name:
        return

    check_key = f"{task_id}:{check_name}"
    if check_key != tui_state._tailing_check_id:
        tui_state._tailing_check_id = check_key
        tui_state.check_log_file_pos = 0
        tui_state.check_log_lines = []

    log_path = _check_log_path(ralpanda_dir, task_id, check_name)
    tui_state.check_log_file_pos = _tail_log_file(log_path, tui_state.check_log_lines, tui_state.check_log_file_pos)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render_log_lines(
    log_entries: list[tuple[str, str]],
    lines: list[tuple[str, int]],
    width: int,
) -> None:
    """Convert log entries into display lines (newest-first, word-wrapped).

    log_entries: list of (timestamp, text) tuples from tail_log / tail_check_log.
    lines: output list of (text, curses_attr) tuples to append to.
    width: available panel width.
    """
    log_width = width - 1  # leading space
    indent = " " * 9  # align under text after "HH:MM:SS "
    cont_width = log_width - 9

    # Group entries into blocks: each block starts with a timestamp
    blocks: list[list[tuple[str, str]]] = []
    for ts_val, text in log_entries:
        if ts_val:  # new block (has timestamp)
            blocks.append([(ts_val, text)])
        elif blocks:  # continuation of current block
            blocks[-1].append(("", text))
        else:  # orphan continuation, start new block
            blocks.append([("", text)])

    # Render blocks in reverse (newest first)
    for block in reversed(blocks):
        for ts_val, text in block:
            is_tool = "[tool:" in text
            if ts_val:
                full = f"{ts_val} {text}"
                if is_tool:
                    if len(full) > log_width:
                        full = full[:log_width - 3] + "..."
                    lines.append((f" {full}", curses.A_DIM))
                else:
                    first, *rest = _wrap(full, log_width) or [""]
                    lines.append((f" {first}", 0))
                    if rest:
                        remainder = " ".join(rest)
                        for wl in _wrap(remainder, cont_width):
                            lines.append((f" {indent}{wl}", 0))
            else:
                if is_tool:
                    display = f"{indent}{text}"
                    if len(display) > log_width:
                        display = display[:log_width - 3] + "..."
                    lines.append((f" {display}", curses.A_DIM))
                else:
                    for wl in _wrap(text, cont_width):
                        lines.append((f" {indent}{wl}", 0))


def _check_log_path(ralpanda_dir: Path, task_id: str, check_name: str) -> Path:
    """Return the log file path for a check subagent."""
    safe_id = task_id.replace("/", "-")
    return ralpanda_dir / "logs" / f"{safe_id}-{check_name}.jsonl"


def _fmt_tokens(task: dict) -> str:
    """Return compact token count string for a task, or '' if not available."""
    usage = task.get("usage")
    if not usage or not isinstance(usage, dict):
        return ""
    out = usage.get("output_tokens", 0)
    if not out:
        return ""
    if out >= 1_000_000:
        return f"{out / 1_000_000:.1f}Mt"
    if out >= 1_000:
        return f"{out / 1_000:.1f}kt"
    return f"{out}t"


def _infer_check_status(ralpanda_dir: Path, task_id: str, check_name: str) -> str:
    """Infer a check's live status from its log file.

    Returns: 'pending' | 'running' | 'pass' | 'fail' | 'infra_fail'
    """
    log_path = _check_log_path(ralpanda_dir, task_id, check_name)
    if not log_path.exists():
        return "pending"

    # Read last few KB looking for a VERDICT line
    try:
        size = log_path.stat().st_size
        with open(log_path, "r", errors="replace") as f:
            if size > 8192:
                f.seek(size - 8192)
                f.readline()  # skip partial line
            tail = f.read()
    except OSError:
        return "running"

    for line in reversed(tail.split("\n")):
        if "VERDICT: PASS" in line:
            return "pass"
        if "VERDICT: FAIL" in line:
            return "fail"
        if "VERDICT: INFRA_FAIL" in line:
            return "infra_fail"

    return "running"


def _task_log_path_str(ralpanda_dir: Path, task_id: str) -> str:
    """Return the log file path as a string for display."""
    safe_id = task_id.replace("/", "-")
    return str(ralpanda_dir / "logs" / f"{safe_id}.jsonl")


def _parse_iso(s: str):
    """Parse an ISO 8601 timestamp, handling trailing Z."""
    from datetime import datetime, timezone
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def _total_completed_time(tasks: list[dict]) -> int:
    """Return total seconds spent on completed tasks, excluding pauses."""
    total = 0
    for t in tasks:
        if t.get("type") == "pause":
            continue
        started = t.get("started_at")
        completed = t.get("completed_at")
        if not started or not completed:
            continue
        try:
            secs = int((_parse_iso(completed) - _parse_iso(started)).total_seconds())
            if secs > 0:
                total += secs
        except (ValueError, TypeError):
            pass
    return total


def _fmt_duration(task: dict) -> str:
    """Return a compact human-readable duration string for a task, or '' if not available."""
    from datetime import datetime, timezone
    started = task.get("started_at")
    if not started:
        return ""
    try:
        t0 = _parse_iso(started)
        completed = task.get("completed_at")
        if completed:
            t1 = _parse_iso(completed)
        elif task.get("status") == "running":
            t1 = datetime.now(timezone.utc)
        else:
            return ""
        secs = int((t1 - t0).total_seconds())
        if secs < 0:
            return ""
        if secs < 60:
            return f"{secs}s"
        mins, s = divmod(secs, 60)
        if mins < 60:
            return f"{mins}m{s:02d}s"
        hrs, m = divmod(mins, 60)
        return f"{hrs}h{m:02d}m"
    except (ValueError, TypeError):
        return ""


def _wrap(text: str, width: int) -> list[str]:
    """Simple word-wrap for display."""
    if width <= 0:
        return []
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        words = paragraph.split()
        current = ""
        for word in words:
            if current and len(current) + 1 + len(word) > width:
                lines.append(current)
                current = word
            elif current:
                current += " " + word
            else:
                current = word
        if current:
            lines.append(current)
    return lines


