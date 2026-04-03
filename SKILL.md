---
name: ralpanda
description: "Use this skill to manage the ralpanda autonomous agent loop: set up a project, decompose plans into tasks, stop/pause/resume the loop, edit tasks, retry failed tasks, or view logs. Triggers on: 'ralpanda', 'ralpanda help', 'stop ralpanda', 'pause ralpanda', 'ralpanda status', 'create tasks', 'set up ralpanda', 'decompose this plan', 'break this into tasks', 'edit tasks', 'add a task'."
argument-hint: "[action or plan text]"
---

# /ralpanda — Autonomous Agent Loop

You are managing the ralpanda autonomous agent loop for this project.

**IMPORTANT: Script location.** All ralpanda scripts live alongside this skill file. To find them, resolve the directory this SKILL.md is in and look under `scripts/`. For example, if this skill is at `~/.claude/skills/ralpanda/SKILL.md`, the loop script is at `~/.claude/skills/ralpanda/scripts/ralpanda-loop`. Use the actual resolved path — never hardcode a dotfiles or home directory path.

We'll call this resolved path `SKILL_DIR` below.

---

## Step 1: Check Setup & Route

Check if `.ralpanda/` exists in the current project directory.

- **If `.ralpanda/` does NOT exist** — this is a first-time setup. Go to **Initial Setup** below, then ask the user what they'd like to do next.
- **If `.ralpanda/` exists** — read the current state (see **Read Current State**), report it briefly, then ask the user:
  > Would you like to **add tasks** from a plan, or **manage** the running loop?

If the user provided an argument or their intent is clear from context, skip the question and go directly to the right section.

- **If the argument is `help`** — go to **Help** below.

---

## Initial Setup

1. Create the directory structure:
   ```
   .ralpanda/
   .ralpanda/logs/
   .ralpanda/plans/
   .ralpanda/sentinels/
   .ralpanda/outcomes/
   ```

2. **Per-Task Acceptance Criteria** — Ask the user:

   > **What checks should every task's agent pass before marking a task as done?**
   >
   > These run during each task — not at the end. List each criterion as a short string (e.g., `"npm test passes"`, `"no new lint warnings"`), or say "none" to skip.
   >
   > If you'd like, I can explore the codebase first and suggest criteria based on what I find.

   Do NOT suggest specific criteria upfront. Only offer to explore and suggest if the user asks for help.

   For each criterion they provide, capture a short description string that will be added verbatim to the task's `acceptance_criteria` array.

3. **End-of-Plan Review Checks** — Ask the user:

   > **What review checks should run once after all tasks are complete?**
   >
   > These are separate from per-task criteria — they run as dedicated review agents at the end. For each check, give a short name and the command/prompt (e.g., name: "typecheck", prompt: "run `npm run typecheck` and report any errors with file paths and fixes").
   >
   > Say "none" to skip, or I can explore the codebase and suggest checks.

   Do NOT suggest specific checks upfront. Only offer to explore and suggest if the user asks for help.

   For each check, get:
   - A short name (e.g., "typecheck", "lint", "tests")
   - The specific prompt for the review agent (should include the command to run AND instructions for what to report if it fails — specifically: what files need to change, what the fix is, etc.)
   - The execution mode: ask whether this check needs to run builds/tests (`"isolated"` — runs alone, one at a time) or only reads code (`"parallel"` — runs concurrently with other parallel checks). Parallel checks get an auto-injected constraint forbidding builds/tests.

4. **Model** — Ask the user which Claude model to use for agent iterations. Default is `opus[1m]`. They can change this later in config.json.

5. **Wait for the user to confirm** the per-task acceptance criteria, review checks, and model before writing config. When presenting the confirmation summary, also mention that a **plan-completeness check** will be automatically added to every review task — this check reads the plan file and tasks.json to verify that every requirement in the plan has been addressed by at least one completed task. Do NOT proceed until they've approved everything.

6. Write `.ralpanda/config.json`:
   ```json
   {
     "model": "opus[1m]",
     "task_acceptance_criteria": [
       "npm run typecheck passes with no errors",
       "npm test passes"
     ],
     "review_checks": [
       {"name": "...", "prompt": "...", "mode": "isolated"},
       ...
     ],
     "max_attempts_per_task": 3
   }
   ```

   Each review check must specify a `mode`:
   - `"isolated"` — needs to run builds, tests, or other resource-intensive commands. These checks run **one at a time**, sequentially.
   - `"parallel"` — only reviews code by reading files and analyzing. These checks run **all at once**, concurrently. The review script auto-injects a strict constraint telling the agent it MUST NOT run builds, tests, compilers, linters, or formatters — only read/search/analyze.

   Default to `"isolated"` if the user doesn't specify. When asking the user about review checks, also ask whether each check needs to run builds/tests (isolated) or just reads code (parallel).

7. Add `.ralpanda/` to the **global** gitignore (so it's excluded from every project without polluting each repo's `.gitignore`):
   ```bash
   # Ensure a global gitignore is configured
   git config --global core.excludesfile ~/.gitignore_global 2>/dev/null || true
   GLOBAL_GITIGNORE=$(git config --global core.excludesfile)
   # Append if not already present
   grep -qxF '.ralpanda/' "$GLOBAL_GITIGNORE" 2>/dev/null || echo '.ralpanda/' >> "$GLOBAL_GITIGNORE"
   ```

After setup is complete, ask the user if they'd like to add tasks from a plan now.

---

## Add Tasks From a Plan

**Concurrent editing is safe** — all tasks.json writes use file locking (`fcntl.flock`). You can edit tasks.json while the loop is running. Use the **Locked Write Protocol** below for all modifications.

### Accept the Plan

The user provides a plan via one of:
- **Inline text** in the `/ralpanda` arguments or conversation
- **File path** — read the file
- **Conversation** — ask them to describe what they want built

Save the plan to `.ralpanda/plans/` with a unique, descriptive filename derived from the plan content (e.g., `.ralpanda/plans/add-user-auth.md`, `.ralpanda/plans/refactor-api-layer.md`). Use lowercase kebab-case. If a file with that name already exists, append a numeric suffix (e.g., `add-user-auth-2.md`).

Tell the user: "Plan saved to `.ralpanda/plans/<filename>.md` — tasks will reference this file for execution and completeness checks."

We'll call this path `PLAN_PATH` below.

### Check Base SHA

Before decomposing, check whether the diff baseline needs to be set or reset. Read `.ralpanda/tasks.json` (if it exists) and `.ralpanda/base_sha` (if it exists).

- **No existing tasks, or all tasks are done**: Tell the user:
  > The diff baseline (base_sha) will be set to current HEAD when the first work task runs. Reviews will only cover changes made by these new tasks.

  Ask them to confirm, or if they'd prefer reviews to include earlier work too.

- **Existing pending or in-progress tasks** (i.e., appending to an active plan): Ask the user:
  > There are existing tasks with a base_sha already set. Would you like to:
  > 1. **Keep the current baseline** — new tasks' reviews will cover all changes since the original base_sha (including prior tasks' work)
  > 2. **Insert a `delete_base_sha` gate** — a fresh baseline will be auto-captured when the next work task runs, so their review will only cover changes from that point forward

  If they choose option 2, insert a `delete_base_sha` task during decomposition (see rule 5 below). New work tasks should depend on it.

### Decompose Into Tasks

Analyze the plan and break it into atomic work tasks. Each task must be completable in a **single agent iteration** (one Claude session with full context).

#### Rules for task decomposition:

1. **Atomic sizing**: If you can't describe what the agent should do in 2-3 focused paragraphs, the task is too big. Split it.

2. **Dependency ordering**: Tasks form a DAG. Common ordering:
   - Schema/database changes first
   - Data models and types next
   - Backend logic (services, API routes)
   - Frontend components
   - Integration/wiring
   - Review at the end

3. **Each task gets**:
   - `id`: Format is `ralpanda/<plan-slug>/<NNN>` where:
     - `<plan-slug>` is the plan filename without extension (e.g., `add-user-auth` from `.ralpanda/plans/add-user-auth.md`)
     - `<NNN>` is a zero-padded 3-digit number, **globally unique across all slugs**
     - **CRITICAL**: Use `next_task_id(tasks, plan_slug)` or `next_task_ids(tasks, plan_slug, count)` from `ralpanda.dag` to generate IDs. These functions find the global max across all task IDs and return the next number(s). Never compute IDs manually.
     - Example: `ralpanda/add-user-auth/001`, `ralpanda/add-user-auth/002`
     - For gate tasks (delete_base_sha, pause) without a plan, use `ralpanda/_gate/<NNN>`
   - `title`: Short imperative title
   - `type`: "work" | "review" | "delete_base_sha" | "pause"
   - `status`: "pending"
   - `depends_on`: Array of task IDs that must complete first
   - `plan_source`: PLAN_PATH (the specific plan file for this set of tasks)
   - `description`: Everything the agent needs — what to do, which files to look at, what approach to take. Be specific about file paths when you know them. Include enough context that the agent doesn't need to read the whole plan.
   - `acceptance_criteria`: Specific, verifiable criteria for this task. **Always prepend** the global `task_acceptance_criteria` from `.ralpanda/config.json` to every work task's criteria, then add any task-specific criteria after them.
   - `outcome`: null
   - `attempt`: 0
   - `created_at`: current ISO8601 timestamp
   - `started_at`: null
   - `completed_at`: null

4. **One review task at the end**: This depends on ALL work tasks. The title should be generic (not plan-specific). The review task **must** include a `checks` array — this is what the review orchestrator reads to run checks. Copy the `review_checks` from `.ralpanda/config.json` into the task's `checks` array, then append a plan-completeness check that references the specific `PLAN_PATH`. The `description` field is a human-readable summary only.

   Example — if config.json has typecheck and lint checks, and PLAN_PATH is `.ralpanda/plans/add-user-auth.md`:
   ```json
   {
     "id": "ralpanda/add-user-auth/005",
     "title": "Review: run all checks and verify plan completeness",
     "type": "review",
     "status": "pending",
     "depends_on": ["<all work task IDs>"],
     "plan_source": ".ralpanda/plans/add-user-auth.md",
     "description": "Run all configured review checks and verify plan completeness.",
     "checks": [
       {"name": "typecheck", "prompt": "Run `npm run typecheck` and report any type errors with file paths and suggested fixes.", "mode": "isolated"},
       {"name": "lint", "prompt": "Run `npm run lint` and report any lint errors with file paths and suggested fixes.", "mode": "isolated"},
       {"name": "plan-completeness", "prompt": "Read .ralpanda/plans/add-user-auth.md and .ralpanda/tasks.json. Verify that every requirement in the plan has been addressed by at least one completed task. List any requirements that appear unimplemented or only partially implemented, and describe what work remains for each.", "mode": "parallel"}
     ],
     "acceptance_criteria": [
       "All review checks pass",
       "Plan completeness check confirms all requirements met"
     ],
     "outcome": null,
     "attempt": 0
   }
   ```

   Copy the `mode` field from each check in config.json. The plan-completeness check is always appended with `"mode": "parallel"` since it only reads files.

   The plan-completeness check is always appended as the final entry in the `checks` array, referencing the specific plan file, with `"mode": "parallel"`:
   > `{"name": "plan-completeness", "prompt": "Read <PLAN_PATH> and .ralpanda/tasks.json. Verify that every requirement in the plan has been addressed by at least one completed task. List any requirements that appear unimplemented or only partially implemented, and describe what work remains for each.", "mode": "parallel"}`

5. **`delete_base_sha` gate tasks**: Add a `delete_base_sha` task after every review. This removes the diff baseline file so that when the next plan's first work task runs, a fresh baseline is auto-captured. When orchestrating multiple sequential plans, the pattern is: Plan A work → Plan A review → `delete_base_sha` → Plan B work (auto-captures new baseline) → Plan B review → `delete_base_sha`. The user can move these tasks around in the DAG for complex scenarios. It runs instantly (no agent spawned).

   Example — review task is `ralpanda/add-user-auth/005`:
   ```json
   {
     "id": "ralpanda/add-user-auth/006",
     "title": "Delete base SHA",
     "type": "delete_base_sha",
     "status": "pending",
     "depends_on": ["ralpanda/add-user-auth/005"],
     "plan_source": ".ralpanda/plans/add-user-auth.md",
     "description": "Remove the diff baseline to signal plan completion.",
     "acceptance_criteria": [],
     "outcome": null,
     "attempt": 0
   }
   ```

6. **`pause` gate tasks**: A `pause` task pauses the loop when reached and waits for a resume sentinel. The user can insert pause tasks anywhere in the DAG — for example, between phases to inspect intermediate results, or before a review to manually verify changes. The loop will set its state to "paused" and poll for a resume sentinel. It runs instantly (no agent spawned). Pauses can also be inserted from the TUI with the `p` key.

   Example:
   ```json
   {
     "id": "ralpanda/add-user-auth/004",
     "title": "Pause for manual inspection",
     "type": "pause",
     "status": "pending",
     "depends_on": ["ralpanda/add-user-auth/003"],
     "plan_source": ".ralpanda/plans/add-user-auth.md",
     "description": "Pause the loop for manual inspection before proceeding.",
     "acceptance_criteria": [],
     "outcome": null,
     "attempt": 0
   }
   ```

7. **Context carries forward**: Later task descriptions can reference what earlier tasks should have accomplished, but shouldn't assume specific implementation details.

8. **Task-specific verification**: For each work task, suggest any task-specific verification steps beyond the default review checks (e.g., "manually test the login flow", "verify the migration is reversible", "check the API response shape matches the spec"). Add these to the task's `acceptance_criteria`.

10. **Present the full task list to the user for confirmation** before writing. Show each task with its title, dependencies, description summary, and acceptance criteria (including any task-specific verification). Ask the user to approve, reorder, merge, split, or modify tasks. Do NOT write `tasks.json` until they confirm.

### Validate and Write

1. Validate there are no duplicate task IDs (including IDs from existing tasks if appending).
2. Validate the DAG has no cycles.
3. Validate every `depends_on` reference points to an existing task ID.
4. Write `.ralpanda/tasks.json` using the **Locked Write Protocol**:
   ```json
   {
     "version": 1,
     "created_at": "<ISO8601>",
     "tasks": [...]
   }
   ```
   Note: there is no top-level `plan_source` — each task carries its own `plan_source` pointing to the specific plan file, which allows tasks from different plans to coexist.

### Print Summary

Show the user:
- Total task count (N work + 1 review + 1 delete_base_sha)
- DAG depth (longest dependency chain)
- Task list with IDs, titles, and dependencies:
  ```
  ralpanda/add-user-auth/001  Add User model and migration          (no deps)
  ralpanda/add-user-auth/002  Create auth middleware                  -> .../001
  ralpanda/add-user-auth/003  Add login/register API endpoints       -> .../001, .../002
  ralpanda/add-user-auth/004  Add session management                 -> .../003
  ralpanda/add-user-auth/005  Review: run all checks                 -> .../001–004
  ralpanda/add-user-auth/006  Delete base SHA                        -> .../005
  ```

Tell them to start the loop by running the script directly in their terminal:
```
SKILL_DIR/scripts/ralpanda-loop
```
(Use the actual resolved absolute path.)

---

## Manage the Loop

### Read Current State

**MANDATORY**: You MUST complete this section before executing ANY action below — even if you checked state moments ago in the same conversation. State can change between turns (the loop may restart, a task may finish, PIDs may change). Never assume prior state is still valid. Never tell the user "already done" or "nothing to do" without re-reading state first.

Read the current state:
1. Check if `.ralpanda/` exists. If not, tell the user to run `/ralpanda` first to set up.
2. Read `.ralpanda/loop.state` (running/paused/idle, or missing = not started)
3. **Validate the loop PID** — if `loop.state` says "running" or "paused":
   a. Read `.ralpanda/loop.pid`. If the file is missing, the loop is dead.
   b. Check if the PID is alive **and** is actually the loop process:
      ```bash
      kill -0 $(cat .ralpanda/loop.pid) 2>/dev/null && ps -p $(cat .ralpanda/loop.pid) -o command= | grep -q ralpanda
      ```
   c. If the PID is dead or belongs to a different process, the loop crashed or was killed. Clean up the stale state:
      - Write `"idle"` to `.ralpanda/loop.state`
      - Remove `.ralpanda/loop.pid`, `.ralpanda/current_task`, `.ralpanda/agent.pid`
      - Tell the user: "The loop state said running but the process is dead (likely crashed or was killed). Cleaned up stale state."
      - If there's a task with status "running" in tasks.json, reset it to "pending" so it can be retried.
4. Read `.ralpanda/current_task` (which task is active, if any)
5. Read `.ralpanda/tasks.json` to get task counts

Report the current state briefly, then handle the user's requested action.

If no action is given, show the status and list available actions.

### Actions

#### `help`
Print a quick-reference of everything `/ralpanda` can do, then stop. Output exactly this (no extra commentary):

```
/ralpanda — Autonomous Agent Loop

Setup & Planning:
  /ralpanda                -- Set up a new project (first time) or show status
  /ralpanda <plan>         -- Decompose a plan into tasks and add them to the DAG

Loop Control:
  /ralpanda start          -- Show the command to start the loop (run it yourself)
  /ralpanda graceful-pause -- Insert a pause task after the current task
  /ralpanda graceful-stop  -- Finish current task, then stop the loop
  /ralpanda force-stop     -- Kill the current agent; loop stops via exit sentinel
  /ralpanda resume         -- Resume a paused loop

Monitoring:
  /ralpanda status         -- Show loop state, task counts, and next runnable task
  /ralpanda log <task_id>  -- Show a summary of a task's execution log

Task Management:
  /ralpanda edit           -- Modify task descriptions, criteria, deps, or ordering
  /ralpanda add            -- Add new tasks to the DAG
  /ralpanda retry <id>     -- Reset a failed task so it can be retried

Other:
  /ralpanda bump-runs [N]  -- Reset runs_remaining counter (default: 1000)
  /ralpanda help           -- Show this help message

TUI Keyboard Shortcuts (in the loop terminal):
  ↑/↓    Navigate task list
  p      Insert pause (before selected task, or global if none selected)
  r      Resume paused loop
  q      Graceful quit (finish current task)
  Q      Force quit (kill agent immediately)
  f      Auto-follow running task
```

#### `start`
The loop must be started by the user directly in their terminal. Tell them to run:
```
SKILL_DIR/scripts/ralpanda-loop
```
(Use the actual resolved absolute path.)

Do NOT launch the loop script from within Claude. The user needs to run it themselves so it has its own terminal session. The loop includes an integrated curses TUI — no separate dashboard is needed.

#### `graceful-pause`
Insert a pause task that will activate after the current task completes. Use the **Locked Write Protocol** to insert the pause.

1. Read `.ralpanda/tasks.json` and `.ralpanda/current_task`.
2. Use `next_task_id()` / `next_task_ids()` from `ralpanda.dag` to generate task IDs.
3. If a task is currently running:
   - Find all pending tasks that depend on the running task.
   - Insert a `pause` task that depends on the running task.
   - Update those downstream pending tasks to also depend on the pause task.
4. If no task is running but pending tasks exist:
   - Find the next runnable pending task.
   - Insert a `pause` task with the same `depends_on` as the next runnable task.
   - Add the pause task's ID to the next runnable task's `depends_on`.
5. If no tasks are pending: tell the user there's nothing to pause.

Report: "Pause task inserted. The loop will pause after the current task finishes."

Do NOT automatically resume — the user will either make edits and then `/ralpanda resume`, or just `/ralpanda resume` directly.

#### `graceful-stop`
Graceful stop — the loop will finish the current task and then exit. Always create the sentinel regardless of current loop state — the user knows what they want.

```bash
touch .ralpanda/sentinels/exit
```

Report: "Exit sentinel created. The loop will stop after the current task finishes."

After reporting, wait 3 seconds and re-check `.ralpanda/loop.state` and `.ralpanda/loop.pid`. If the loop has already exited, confirm to the user. If it's still running (expected if a task is in progress), note that it's still finishing the current task.

#### `force-stop`
Immediately terminate the current agent iteration. The loop itself will see the exit sentinel and stop cleanly.

1. Read `.ralpanda/agent.pid` — if exists, send SIGTERM:
   ```bash
   kill $(cat .ralpanda/agent.pid) 2>/dev/null || true
   ```
2. Create exit sentinel so the loop stops after handling the killed agent:
   ```bash
   touch .ralpanda/sentinels/exit
   ```
3. **Do NOT** kill the loop process unless the user explicitly asks for it. The loop will see the exit sentinel and stop cleanly after the current iteration.

Report: "Agent killed. Exit sentinel created — loop will stop after cleanup."

4. **Verify**: Wait 3 seconds, then re-check `.ralpanda/loop.pid` and `.ralpanda/loop.state`. If the loop is still alive or a new PID has appeared, warn the user.

If the user says "force stop" without further context, assume they mean the current agent iteration, not the loop process itself.

#### `resume`
Resume a paused loop.

```bash
touch .ralpanda/sentinels/resume
```

Report: "Resume sentinel created. The loop will continue shortly."

#### `status`
Read and report:
- Loop state (from `.ralpanda/loop.state`)
- Current task (from `.ralpanda/current_task`)
- Task breakdown from `.ralpanda/tasks.json`:
  - Count by status (pending, running, done, failed, split)
  - List of failed tasks (if any) with their titles
  - Next runnable task
- Loop PID (from `.ralpanda/loop.pid`)
- Runs remaining (from `.ralpanda/runs_remaining`) — if below 100, ask the user if they want to bump it back to 1000

#### `bump-runs [N]`
Reset the runs_remaining counter. Default to 1000 if no number given:
```bash
echo "1000" > .ralpanda/runs_remaining
```
Report: "Runs remaining reset to N."

#### `retry <task_id>`
Reset a failed task so it can be retried. Use the **Locked Write Protocol**.

Find the task and:
1. Set `status` to `"pending"`
2. Set `attempt` to `0`
3. Set `outcome` to `null`
4. Set `started_at` and `completed_at` to `null`

Write the updated file. Report: "Task X reset to pending."

#### `edit`
The user wants to modify tasks. Use the **Locked Write Protocol**.

Ask them what they want to change:
- Edit a task's description, acceptance criteria, or dependencies
- Reorder tasks
- Remove a task
- Change a task's type

Make the requested changes, validate the DAG still has no cycles, and write back.

#### `add`
The user wants to add new tasks. Use the **Locked Write Protocol**.

1. Read `.ralpanda/tasks.json` to understand existing tasks
2. Use `next_task_id()` / `next_task_ids()` from `ralpanda.dag` to generate task IDs
3. **Base SHA check** — follow the same flow as "Check Base SHA" in "Add Tasks From a Plan". Check whether existing tasks are all done vs. in-progress, and ask the user whether to delete the baseline or keep it. If they want a delete, insert a `delete_base_sha` gate task.
4. Ask where in the DAG the new task(s) should go (what they depend on, what depends on them)
5. Create the task entries (if a `delete_base_sha` gate was inserted, make new work tasks depend on it)
6. Update dependencies of existing tasks if needed
7. Validate DAG
8. Write back

#### `log <task_id>`
Read the log file for a specific task:
```
.ralpanda/logs/<task_id>.jsonl
```
(Note: `/` in task IDs is replaced with `-` in filenames.)

Parse the stream-json output and present a summary:
- Key actions the agent took
- Files it modified
- Any errors or issues
- The outcome it wrote

If the task has review sub-logs (e.g., `<task_id>-typecheck.jsonl`), summarize those too.

---

## General Rules

- **Always re-read state before every action.** Never assume state from a previous turn is still valid. The loop can restart, tasks can finish, and PIDs can change between turns. This is the single most important rule — violating it leads to stale assumptions and wrong answers.
- Always validate task integrity (no duplicate IDs + no DAG cycles) after any modification to tasks.json.
- **Always use the Locked Write Protocol** when modifying tasks.json (see below).
- Be concise in status reports. Use a table format when listing tasks.
- If `.ralpanda/` exists and has a valid config but the user's intent is ambiguous, default to showing status.
- **Verify destructive actions.** After killing processes or creating sentinels, wait a few seconds and confirm the expected state change actually happened. If a new PID appears where you expected none, warn the user about possible auto-restart mechanisms.

### Locked Write Protocol

All modifications to `tasks.json` **must** use file locking to prevent corruption from concurrent access. The loop process and external Claude sessions (like this one) can both read and write tasks.json safely using `fcntl.flock`.

**How to use it**: Run a short Python script via Bash that acquires an exclusive lock, reads, modifies, and atomically writes back:

```bash
python3 -c "
import fcntl, json, os, sys
tasks_file = '.ralpanda/tasks.json'
fd = os.open(tasks_file, os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
try:
    with os.fdopen(os.dup(fd), 'r') as f:
        f.seek(0)
        data = json.load(f)
    # --- YOUR MODIFICATIONS HERE ---
    # Example: data['tasks'][0]['status'] = 'pending'
    # ---
    tmp = tasks_file + '.tmp.' + str(os.getpid())
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
    os.replace(tmp, tasks_file)
finally:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
"
```

Alternatively, you can use the `locked_tasks` context manager from the ralpanda Python package:

```python
# From SKILL_DIR/scripts/
from ralpanda.dag import locked_tasks
with locked_tasks(Path('.ralpanda/tasks.json')) as data:
    # modify data['tasks'] as needed
    pass
# automatically written back on exit
```

The lock is held only for the read-modify-write window (milliseconds). This is safe to use while the loop is running — the loop uses the same locking protocol.

**Important**: The spawned claude agent iterations NEVER read or write tasks.json. They write their outcomes to `.ralpanda/outcomes/<task_id>.json` and the loop merges the outcome into tasks.json after the agent exits. This eliminates the primary source of write contention.
