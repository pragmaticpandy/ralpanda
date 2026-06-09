"""Prompt generation for work tasks, review checks, and coordinator agents."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import dag


def _absolute_outcome_file(path: str | Path) -> str:
    """Return an absolute outcome path for prompt text."""
    return str(Path(path).resolve())


def build_work_prompt(
    task: dict,
    all_tasks: list[dict],
    ralpanda_dir: Path,
) -> str:
    """Generate the full prompt for a work task agent.

    The agent writes an attempt-scoped outcome envelope and must NEVER read or
    write tasks.json.
    """
    task_id = task["id"]
    title = task["title"]
    description = task.get("description", "")
    plan_source = task.get("plan_source")
    attempt = task.get("attempt", 1)
    agent_namespace = dag.work_agent_namespace()
    outcome_file = _absolute_outcome_file(dag.outcome_path(
        ralpanda_dir,
        task_id,
        attempt,
        agent_namespace,
    ))

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
When you are done, you MUST write your outcome to this exact path:

`{outcome_file}`

This is an absolute path. Use it exactly as shown even if you change directories. Do not derive, shorten, or rewrite it as a relative path. Create parent directories if needed.

Write the file atomically: write a temporary file in the same directory, then rename it into `{outcome_file}`.

The file MUST be a JSON object using this envelope:

```json
{{
  "schema_version": 1,
  "task_id": "{task_id}",
  "attempt": {attempt},
  "agent": {{
    "kind": "work",
    "namespace": "{agent_namespace}"
  }},
  "status": "done",
  "summary": "1-2 sentence description of what you did and why",
  "payload": {{
    "files_changed": ["array", "of", "file", "paths", "you", "modified"],
    "decisions": [
      {{
        "what": "Description of a non-obvious choice you made",
        "why": "Your reasoning",
        "alternatives": ["Other options you considered"]
      }}
    ]
  }}
}}
```

Include decisions for ANY choice where you normally would have asked the user for guidance. Even small judgment calls — record them so the user can review later.

If you encounter an error that prevents completion, write an outcome with `"status": "failed"`:

```json
{{
  "schema_version": 1,
  "task_id": "{task_id}",
  "attempt": {attempt},
  "agent": {{
    "kind": "work",
    "namespace": "{agent_namespace}"
  }},
  "status": "failed",
  "summary": "What went wrong and what you tried",
  "payload": {{
    "files_changed": [],
    "decisions": []
  }}
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
  "schema_version": 1,
  "task_id": "{task_id}",
  "attempt": {attempt},
  "agent": {{
    "kind": "work",
    "namespace": "{agent_namespace}"
  }},
  "status": "split",
  "summary": "Explain why you're splitting and your decomposition strategy",
  "payload": {{
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
    attempt: int,
    base_sha: str | None,
    outcome_file: str,
    agent_kind: str,
    agent_namespace: str,
) -> str:
    """Generate prompt for a single review check agent."""
    outcome_file = _absolute_outcome_file(outcome_file)
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

**You are running as a parallel review check alongside other checks. You MUST NOT run any builds, tests, compilers, linters, formatters, or other resource-intensive commands.** Only review code by reading files and analyzing them. Do not run commands like npm, npx, node, make, cargo, go, python, pytest, jest, tsc, eslint, or similar. Use only Read, Glob, Grep, lightweight Bash commands (like git diff, wc, cat, etc.), and the minimum write needed for the outcome file."""

    return f"""You are a review check agent. Your job is to run ONE specific check and report the result.

## Check: {check_name}

{check_prompt}
{diff_context}{parallel_constraint}
## Instructions

1. Run the check as described above.
2. Analyze the results carefully.
3. Write your outcome to this exact path:

   `{outcome_file}`

   This is an absolute path. Use it exactly as shown even if you change directories. Do not derive, shorten, or rewrite it as a relative path. Create parent directories if needed.

4. Write the outcome atomically: write a temporary file in the same directory, then rename it into `{outcome_file}`.

5. The outcome MUST be a JSON object using this envelope:

```json
{{
  "schema_version": 1,
  "task_id": "{task_id}",
  "attempt": {attempt},
  "agent": {{
    "kind": "{agent_kind}",
    "namespace": "{agent_namespace}"
  }},
  "status": "pass",
  "summary": "Short human-readable result.",
  "payload": {{}}
}}
```

Use exactly one of these status values:
- `pass` when the check ran and passed.
- `fail` when the check ran and found problems.
- `infra_fail` only when the check itself could not run due to infrastructure/environment issues (e.g. Docker not running, service unavailable, missing toolchain, network error).

6. If the check FAILS, the `summary` must be non-empty and `payload` MUST include detailed remediation data:
   - What failed
   - Affected file paths when applicable
   - The concrete change needed in each file
   - Suggested fix ordering if useful

   Prefer keys like `what_failed`, `remediation`, `affected_files`, and `evidence`. Be concrete — "fix the type error" is not enough; say what code needs to change and why.

7. Do NOT edit project files. The only file you may write is the outcome file at `{outcome_file}`. Otherwise only read, search, and run commands.

## Context

This is a review check for task {task_id} in the ralpanda autonomous agent loop."""


def build_review_compatibility_prompt(
    check_name: str,
    check_prompt: str,
    mode: str,
    task_id: str,
    attempt: int,
    plan_source: str,
    for_review_check: str | None,
    review_prompt: str | None,
    outcome_file: str,
    agent_kind: str,
    agent_namespace: str,
) -> str:
    """Generate prompt for a pre-plan review-compatibility check agent."""
    outcome_file = _absolute_outcome_file(outcome_file)
    target_name = for_review_check or check_name
    target_prompt = review_prompt or "(The original end-of-plan review prompt was not provided.)"

    parallel_constraint = ""
    if mode == "parallel":
        parallel_constraint = """

## CRITICAL: Code Review Only

**You are running as a parallel review-compatibility check alongside other checks. You MUST NOT run any builds, tests, compilers, linters, formatters, or other resource-intensive commands.** Only review the plan, tasks, and code by reading files and analyzing them. Do not run commands like npm, npx, node, make, cargo, go, python, pytest, jest, tsc, eslint, or similar. Use only Read, Glob, Grep, lightweight Bash commands (like git diff, wc, cat, etc.), and the minimum write needed for the outcome file."""

    return f"""You are a pre-plan review-compatibility check agent. Your job is to run ONE specific check before implementation work starts.

## Compatibility Check: {check_name}

{check_prompt}

## Target End-of-Plan Review Check

Name: {target_name}

Prompt:
{target_prompt}

## Plan and Task Context

Read the plan at `{plan_source}` and the current task graph at `.ralpanda/tasks.json`.

Determine whether the target end-of-plan review check is compatible with the plan that is about to be implemented.
{parallel_constraint}
## Instructions

1. Read the plan, the generated tasks, and any relevant source files needed to understand the review check's assumptions.
2. Decide whether the plan can reasonably be implemented while still satisfying the target review check.
3. Write your outcome to this exact path:

   `{outcome_file}`

   This is an absolute path. Use it exactly as shown even if you change directories. Do not derive, shorten, or rewrite it as a relative path. Create parent directories if needed.

4. Write the outcome atomically: write a temporary file in the same directory, then rename it into `{outcome_file}`.

5. The outcome MUST be a JSON object using this envelope:

```json
{{
  "schema_version": 1,
  "task_id": "{task_id}",
  "attempt": {attempt},
  "agent": {{
    "kind": "{agent_kind}",
    "namespace": "{agent_namespace}"
  }},
  "status": "pass",
  "summary": "Short human-readable result.",
  "payload": {{}}
}}
```

Use exactly one of these status values:
- `pass` only when the review check remains applicable, actionable, and not contradicted by the plan.
- `fail` when the plan contradicts the review check, makes the required pattern unusable, obsoletes the rule, or makes the rule ambiguous enough that implementation agents are likely to be trapped by it.
- `infra_fail` only when this compatibility check could not run due to infrastructure/environment issues, such as unreadable required files or missing toolchain needed only for inspection.

6. If the check FAILS, the `summary` must be non-empty and `payload` MUST include a detailed compatibility report:
   - Name the incompatible target review check
   - Explain exactly which part of the plan conflicts with it
   - Say whether the user should change the plan, change the review check, or change both
   - Propose concrete replacement wording when the review check should change

Do NOT edit project files. The only file you may write is the outcome file at `{outcome_file}`. Otherwise only read, search, and run commands.

## Context

This is a pre-plan review-compatibility check for task {task_id} in the ralpanda autonomous agent loop."""


def build_coordinator_prompt(
    task_id: str,
    failed_checks: list[dict],
    failed_analyses: list[str],
    plan_source: str,
    id_prefix: str,
    next_num: int,
    review_deps: list[str],
    attempt: int,
    outcome_file: str,
    agent_namespace: str,
) -> str:
    """Generate prompt for the fix-up task coordinator agent."""
    outcome_file = _absolute_outcome_file(outcome_file)
    failure_doc = f"# Review Check Failures for {task_id}\n\n"
    failure_doc += "The following review checks failed. For each failure, create one or more fix-up work tasks.\n\n"

    for i, check in enumerate(failed_checks):
        name = check.get("name", f"check_{i}")
        stage = check.get("stage")
        analysis = failed_analyses[i] if i < len(failed_analyses) else "No analysis available."
        stage_line = f"Stage: {stage}\n\n" if stage else ""
        failure_doc += f"""
## Failed Check: {name}

{stage_line}
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
4. Do not inspect project files. The review analyses above are your complete input. Use tools only to write the outcome file.
5. Write your outcome to this exact path:

   `{outcome_file}`

   This is an absolute path. Use it exactly as shown even if you change directories. Do not derive, shorten, or rewrite it as a relative path. Create parent directories if needed.

6. Write the outcome atomically: write a temporary file in the same directory, then rename it into `{outcome_file}`.

7. The outcome MUST be a JSON object using this envelope:

```json
{{
  "schema_version": 1,
  "task_id": "{task_id}",
  "attempt": {attempt},
  "agent": {{
    "kind": "coordinator",
    "namespace": "{agent_namespace}"
  }},
  "status": "tasks_created",
  "summary": "Created remediation tasks for the failed review checks.",
  "payload": {{
    "tasks": []
  }}
}}
```

Use `status: "tasks_created"` only when `payload.tasks` contains a valid non-empty array of task objects.
Use `status: "infra_fail"` if you cannot create remediation tasks due to an infrastructure or prompt/input issue; include a non-empty summary explaining why.

The task objects in `payload.tasks` must have this exact schema:
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

Your final response can be brief, but the outcome file is authoritative."""
