"""Prompt generation for work tasks, review checks, and coordinator agents."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import dag


def build_work_prompt(
    task: dict,
    all_tasks: list[dict],
    ralpanda_dir: Path,
) -> str:
    """Generate the full prompt for a work task agent.

    The agent writes its outcome to .ralpanda/outcomes/<task_id>.json
    and must NEVER read or write tasks.json.
    """
    task_id = task["id"]
    title = task["title"]
    description = task.get("description", "")
    plan_source = task.get("plan_source")
    safe_id = task_id.replace("/", "-")
    outcome_file = f".ralpanda/outcomes/{safe_id}.json"

    # Build acceptance criteria list
    criteria = task.get("acceptance_criteria", [])
    if criteria:
        criteria_text = "\n".join(f"- {c}" for c in criteria)
    else:
        criteria_text = "None specified."

    # Build completed tasks summary
    completed = [
        t for t in all_tasks if t["status"] == "done"
    ]
    if completed:
        summary_lines = []
        for t in completed:
            line = f"- {t['id']}: {t['title']}"
            outcome = t.get("outcome", {}) or {}
            if outcome.get("summary"):
                line += f" → {outcome['summary']}"
            summary_lines.append(line)
        completed_summary = "\n".join(summary_lines)
    else:
        completed_summary = "None yet."

    prompt = f"""You are an autonomous coding agent executing a single atomic task as part of a larger plan.

## Your Task

**ID:** {task_id}
**Title:** {title}

{description}

## Acceptance Criteria

{criteria_text}

"""

    # Add plan source reference
    if plan_source and Path(plan_source).exists():
        prompt += f"""## Plan Reference

The full plan is at `{plan_source}`. You may read it for additional context about the broader goals. However, you MUST only do the work described in this specific task — do not implement other parts of the plan, even if they seem related or easy to do while you're here.

"""

    prompt += f"""## Previously Completed Tasks

{completed_summary}

## Rules

You MUST follow all of these rules strictly:

### 1. Orient Yourself
Before starting any work, read the project's README (or equivalent) to understand the project's purpose, structure, and conventions. This context will help you make better decisions.

### 2. No Git Mutations
NEVER run git add, git commit, git push, git checkout, git reset, git branch, git stash, git rebase, git merge, git cherry-pick, or any other git write operation.
Git reads are fine — you may use git log, git diff, git blame, git show, git status, etc. for context.
The loop script handles all git operations after you finish.

### 3. Stay In Scope
Make ONLY the changes needed for this specific task. Do not refactor unrelated code. Do not "improve" things outside scope. Do not add features not described in the task.

### 4. Write Your Outcome
When you are done, you MUST write your outcome to `{outcome_file}` as a JSON object:

```json
{{
  "status": "done",
  "summary": "1-2 sentence description of what you did and why",
  "files_changed": ["array", "of", "file", "paths", "you", "modified"],
  "decisions": [
    {{
      "what": "Description of a non-obvious choice you made",
      "why": "Your reasoning",
      "alternatives": ["Other options you considered"]
    }}
  ]
}}
```

Include decisions for ANY choice where you normally would have asked the user for guidance. Even small judgment calls — record them so the user can review later.

If you encounter an error that prevents completion, write an outcome with `"status": "failed"`:

```json
{{
  "status": "failed",
  "summary": "What went wrong and what you tried",
  "files_changed": [],
  "decisions": []
}}
```

### 5. NEVER Touch tasks.json
Do NOT read or write `.ralpanda/tasks.json`. The orchestration loop manages task state. You only write your outcome to the file specified above.

### 6. Verify Your Work
Before finishing, check your work against each acceptance criterion. If a criterion involves running a command (like a typecheck), run it and fix any issues.

### 7. Split If Too Large — When In Doubt, Split
If a task involves more than ~3 files or ~2 distinct concerns, split it. Err on the side of splitting — small, focused tasks succeed more often than ambitious ones.

If you decide to split, do NOT do any implementation work. Instead, write your outcome to `{outcome_file}` with `"status": "split"`:

```json
{{
  "status": "split",
  "summary": "Explain why you're splitting and your decomposition strategy",
  "split_into": [
    {{
      "title": "Short imperative title",
      "description": "Detailed description of what to do",
      "acceptance_criteria": ["specific verifiable criteria"],
      "depends_on_subtasks": ["titles of other subtasks this depends on, if any"]
    }}
  ],
  "files_changed": [],
  "decisions": [{{"what": "Why this decomposition", "why": "reasoning", "alternatives": ["other options"]}}]
}}
```

Exit immediately after writing the outcome — the loop handles the rest.

### 8. Do Not Modify Config
Do NOT create or modify .claude/ files, CLAUDE.md, .ralpanda/config.json, or any configuration files unless the task specifically requires it.

### 9. Handle Errors Gracefully
If you encounter an error that prevents completion, still write an outcome with a summary explaining what went wrong and what you tried. This helps the next iteration.
"""
    return prompt


def build_review_check_prompt(
    check_name: str,
    check_prompt: str,
    mode: str,
    task_id: str,
    base_sha: str | None,
) -> str:
    """Generate prompt for a single review check agent."""
    diff_context = ""
    if base_sha:
        diff_context = f"""
## Diff Under Review

The changes being reviewed are between `{base_sha}` and HEAD. To see the full diff:
```
git diff {base_sha}..HEAD
```

To see only changed file names:
```
git diff --name-only {base_sha}..HEAD
```

Scope your review to ONLY these changes. Do not flag pre-existing issues in unchanged code.
"""

    parallel_constraint = ""
    if mode == "parallel":
        parallel_constraint = """

## CRITICAL: Code Review Only

**You are running as a parallel review check alongside other checks. You MUST NOT run any builds, tests, compilers, linters, formatters, or other resource-intensive commands.** Only review code by reading files and analyzing them. Do not run commands like npm, npx, node, make, cargo, go, python, pytest, jest, tsc, eslint, or similar. Use only Read, Glob, Grep, and lightweight Bash commands (like git diff, wc, cat, etc.)."""

    return f"""You are a review check agent. Your job is to run ONE specific check and report the result.

## Check: {check_name}

{check_prompt}
{diff_context}{parallel_constraint}
## Instructions

1. Run the check as described above.
2. Analyze the results carefully.
3. The ABSOLUTE LAST LINE of your response must be your verdict — one of these three, exactly as shown, on its own line with nothing after it:
   `VERDICT: PASS`
   `VERDICT: FAIL`
   `VERDICT: INFRA_FAIL`

   Use INFRA_FAIL only when the check itself could not run due to infrastructure/environment issues (e.g. Docker not running, service unavailable, missing toolchain, network error) — NOT when the check ran and found problems.

   IMPORTANT: Do NOT quote or mention the other verdict strings anywhere in your response. Only the one you are issuing should appear.

4. If the check FAILS, you MUST also provide a detailed remediation plan BEFORE the verdict line:
   - List every specific change needed to make this check pass
   - For each change: specify the file path, what needs to change, and why
   - Be concrete — "fix the type error" is not enough; "change line 42 of src/foo.ts to accept string | undefined instead of string" is
   - If the fix requires multiple steps, list them in order
   - Group related fixes together

5. You are READ-ONLY. Do NOT edit any files. Only read, search, and run commands.

## Context

This is a review check for task {task_id} in the ralpanda autonomous agent loop."""


def build_coordinator_prompt(
    task_id: str,
    failed_checks: list[dict],
    failed_analyses: list[str],
    plan_source: str,
    id_prefix: str,
    next_num: int,
    review_deps: list[str],
) -> str:
    """Generate prompt for the fix-up task coordinator agent."""
    failure_doc = f"# Review Check Failures for {task_id}\n\n"
    failure_doc += "The following review checks failed. For each failure, create one or more fix-up work tasks.\n\n"

    for i, check in enumerate(failed_checks):
        name = check.get("name", f"check_{i}")
        analysis = failed_analyses[i] if i < len(failed_analyses) else "No analysis available."
        failure_doc += f"""
## Failed Check: {name}

### Analysis from review agent:
{analysis}

---
"""

    deps_json = json.dumps(review_deps) if review_deps else "[]"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return f"""You are a task creation agent for the ralpanda autonomous loop.

Review checks have failed. Based on the detailed failure analyses below, you must create fix-up work tasks.

{failure_doc}

## Instructions

1. Analyze each failure and determine what work tasks are needed to fix them.
2. One failing check may require MULTIPLE fix-up tasks if the fixes are in different areas.
3. Each task must be atomic — completable in one agent session.
4. Output ONLY a JSON array of task objects. No other text before or after.

The task objects must have this exact schema:
```json
[
  {{
    "id": "{id_prefix}<NNN>",
    "title": "Short imperative title",
    "type": "work",
    "status": "pending",
    "depends_on": {deps_json},
    "plan_source": "{plan_source}",
    "description": "Detailed description of what to fix and where",
    "acceptance_criteria": ["specific verifiable criteria"],
    "outcome": null,
    "attempt": 0,
    "created_at": "{now}",
    "started_at": null,
    "completed_at": null
  }}
]
```

Start task IDs from {id_prefix}{next_num + 1:03d}.
The depends_on for each fix-up task should be: {deps_json}.
Fix-up tasks CAN depend on each other if there's an ordering requirement.

Output ONLY the JSON array. No markdown fences, no explanation."""
