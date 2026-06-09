"""User-level hook discovery and execution for ralpanda events."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import dag


DEFAULT_TIMEOUT_SECONDS = 30
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def config_home() -> Path:
    """Return the XDG config home used for global ralpanda hooks."""
    configured = os.environ.get("XDG_CONFIG_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config"


def hook_dir(event: str) -> Path:
    """Return the global hook directory for an event."""
    return config_home() / "ralpanda" / "hooks" / event


def discover_hooks(event: str) -> list[Path]:
    """Return executable hook scripts for an event in lexical order."""
    directory = hook_dir(event)
    if not directory.is_dir():
        return []

    scripts: list[Path] = []
    for path in directory.iterdir():
        if path.name.startswith(".") or path.name.endswith("~"):
            continue
        try:
            if path.is_file() and os.access(path, os.X_OK):
                scripts.append(path)
        except OSError:
            continue
    return sorted(scripts, key=lambda p: p.name)


def run_event(
    event: str,
    payload: dict[str, Any],
    ralpanda_dir: Path,
    *,
    history_file: Path | None = None,
    project_root: Path | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Run all hooks subscribed to an event.

    Hooks are executable files under:
      ${XDG_CONFIG_HOME:-~/.config}/ralpanda/hooks/<event>/

    The event payload is sent as JSON on stdin. Hook stdout/stderr is captured
    under the current worktree's .ralpanda/logs/hooks/ directory.
    """
    scripts = discover_hooks(event)
    if not scripts:
        return []

    project_root = (project_root or Path.cwd()).resolve()
    ralpanda_dir = ralpanda_dir.resolve()
    event_payload = dict(payload)
    event_payload.setdefault("event", event)
    event_payload.setdefault("ts", _now_iso())
    event_payload.setdefault("project_root", str(project_root))
    event_payload.setdefault("ralpanda_dir", str(ralpanda_dir))

    payload_text = json.dumps(event_payload, sort_keys=True) + "\n"
    env = _build_env(event_payload)
    log_dir = ralpanda_dir / "logs" / "hooks" / event
    log_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for index, script in enumerate(scripts, start=1):
        log_path = log_dir / _log_name(index, script)
        result = _run_script(
            script,
            event,
            payload_text,
            env,
            project_root,
            log_path,
            timeout_seconds,
        )
        results.append(result)
        if result["status"] != "ok" and history_file:
            dag.log_event(
                history_file,
                f"hook_{result['status']}",
                detail=(
                    f"event={event},script={script},"
                    f"log={result['log_path']}"
                ),
            )

    return results


def _run_script(
    script: Path,
    event: str,
    payload_text: str,
    env: dict[str, str],
    project_root: Path,
    log_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    started_at = _now_iso()
    status = "ok"
    returncode: int | None = None

    with open(log_path, "w") as log:
        log.write(f"event={event}\n")
        log.write(f"script={script}\n")
        log.write(f"started_at={started_at}\n\n")
        log.flush()

        try:
            completed = subprocess.run(
                [str(script)],
                input=payload_text,
                text=True,
                cwd=project_root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
            )
            returncode = completed.returncode
            if returncode != 0:
                status = "failed"
        except subprocess.TimeoutExpired:
            status = "timeout"
            log.write(f"\nHOOK TIMEOUT after {timeout_seconds}s\n")
        except OSError as exc:
            status = "error"
            log.write(f"\nHOOK ERROR: {exc}\n")

        log.write(f"\nfinished_at={_now_iso()}\n")
        log.write(f"status={status}\n")
        if returncode is not None:
            log.write(f"returncode={returncode}\n")

    return {
        "event": event,
        "script": str(script),
        "status": status,
        "returncode": returncode,
        "log_path": str(log_path),
    }


def _build_env(payload: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    task = payload.get("task") or {}
    if not isinstance(task, dict):
        task = {}

    values = {
        "RALPANDA_EVENT": payload.get("event"),
        "RALPANDA_PROJECT_ROOT": payload.get("project_root"),
        "RALPANDA_DIR": payload.get("ralpanda_dir"),
        "RALPANDA_TASK_ID": task.get("id"),
        "RALPANDA_TASK_TYPE": task.get("type"),
        "RALPANDA_TASK_TITLE": task.get("title"),
        "RALPANDA_TASK_RESULT": task.get("status"),
        "RALPANDA_PAUSE_REASON": task.get("pause_reason"),
        "RALPANDA_COMMIT_SHA": payload.get("commit_sha"),
    }

    for key, value in values.items():
        if value is not None:
            env[key] = str(value)
    return env


def _log_name(index: int, script: Path) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{index:02d}-{_safe_name(script.name)}.log"


def _safe_name(value: str) -> str:
    safe = _SAFE_NAME_RE.sub("_", value).strip("._")
    return safe or "hook"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
