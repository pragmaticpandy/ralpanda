"""Agent lifecycle: spawn, poll, collect outcomes, review orchestration, splits."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import dag, git, prompt

COORDINATOR_DEFAULT_MAX_ATTEMPTS = 3
COORDINATOR_DEFAULT_MAX_TURNS = 3
OUTCOME_STABLE_GRACE_SECONDS = 45
PROCESS_START_TOLERANCE_SECONDS = 10.0
EXPECTED_AGENT_ARGV0 = "claude"
EXPECTED_AGENT_ARGS = (
    "--output-format",
    "stream-json",
    "--verbose",
    "--dangerously-skip-permissions",
)


@dataclass(frozen=True)
class _LiveProcessInfo:
    """Identity details for a live process, collected before signaling it."""
    pid: int
    pgid: int | None
    sid: int | None
    argv: list[str] | None
    argv_source: str | None
    command: str | None
    started_at_unix: float | None


@dataclass(frozen=True)
class _RecordedTerminationResult:
    """Result of trying to terminate a process recorded in running metadata."""
    attempted: bool
    hard_killed: bool = False
    safe_to_forget: bool = True
    skip_reason: str | None = None


# ---------------------------------------------------------------------------
# Spawn agents
# ---------------------------------------------------------------------------

def spawn_agent(
    prompt_text: str,
    model: str,
    log_path: Path,
    *,
    ralpanda_dir: Path | None = None,
    task_id: str | None = None,
    attempt: int | None = None,
    agent_kind: str | None = None,
    agent_namespace: str | None = None,
    expected_outcome_path: Path | None = None,
    tools: str | None = None,
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
    if tools is not None:
        cmd.extend(["--tools", tools])
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
        start_new_session=True,
    )
    # Keep reference to log file so it stays open
    proc._log_file = log_file  # type: ignore[attr-defined]
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    proc._pgid = pgid  # type: ignore[attr-defined]

    if (
        ralpanda_dir is not None
        and task_id is not None
        and attempt is not None
        and agent_kind is not None
        and agent_namespace is not None
        and expected_outcome_path is not None
    ):
        expected_outcome_path = expected_outcome_path.resolve()
        meta_path = dag.running_metadata_path(
            ralpanda_dir,
            task_id,
            attempt,
            agent_namespace,
        )
        process_metadata = {
            "argv0": Path(cmd[0]).name,
            "required_args": list(EXPECTED_AGENT_ARGS),
            "cmdline_sha256": _cmdline_digest(cmd),
            "started_at_unix": time.time(),
        }
        live_process = _read_live_process_info(proc.pid)
        if live_process is not None:
            if live_process.started_at_unix is not None:
                process_metadata["started_at_unix"] = live_process.started_at_unix
            if live_process.argv:
                process_metadata["observed_argv0"] = Path(live_process.argv[0]).name
                process_metadata["observed_cmdline_sha256"] = _cmdline_digest(
                    live_process.argv,
                )
            if live_process.command:
                observed_argv0 = _command_text_argv0(live_process.command)
                if observed_argv0:
                    process_metadata["observed_command_argv0"] = observed_argv0
                process_metadata["observed_command_sha256"] = _text_digest(
                    live_process.command,
                )
        metadata = {
            "schema_version": 1,
            "task_id": task_id,
            "attempt": attempt,
            "agent": {
                "kind": agent_kind,
                "namespace": agent_namespace,
            },
            "expected_outcome_path": str(expected_outcome_path),
            "log_path": str(log_path),
            "started_at": dag._now_iso(),
            "pid": proc.pid,
            "pgid": pgid,
            "process": process_metadata,
        }
        dag.atomic_write_json(meta_path, metadata)
        proc._running_metadata_path = meta_path  # type: ignore[attr-defined]
        proc._task_id = task_id  # type: ignore[attr-defined]
        proc._attempt = attempt  # type: ignore[attr-defined]
        proc._agent_kind = agent_kind  # type: ignore[attr-defined]
        proc._agent_namespace = agent_namespace  # type: ignore[attr-defined]
        proc._expected_outcome_path = expected_outcome_path  # type: ignore[attr-defined]
    return proc


def close_agent(proc: subprocess.Popen) -> None:
    """Close the log file associated with an agent process."""
    log_file = getattr(proc, "_log_file", None)
    if log_file:
        log_file.close()


def cleanup_running_metadata(proc: subprocess.Popen) -> None:
    """Remove running metadata for a process that has been handled."""
    path = getattr(proc, "_running_metadata_path", None)
    if isinstance(path, Path):
        path.unlink(missing_ok=True)


def finish_agent_process(proc: subprocess.Popen) -> None:
    """Close logs and remove running metadata for a finished process."""
    close_agent(proc)
    cleanup_running_metadata(proc)


def terminate_agent_process(
    proc: subprocess.Popen,
    *,
    timeout: int = 5,
) -> bool:
    """Terminate an agent process group, returning True if SIGKILL was needed."""
    hard_killed = False
    pgid = getattr(proc, "_pgid", None)
    if not isinstance(pgid, int):
        pgid = None

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass

    try:
        proc.wait(timeout=timeout)
        return hard_killed
    except Exception:
        hard_killed = True

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

    try:
        proc.wait(timeout=timeout)
    except Exception:
        pass
    return hard_killed


def _process_is_alive(pid: int) -> bool:
    """Return True if *pid* appears to still exist."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _cmdline_digest(argv: list[str]) -> str:
    """Return a non-sensitive digest for a command line."""
    encoded = json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _text_digest(text: str) -> str:
    """Return a non-sensitive digest for process command text."""
    return hashlib.sha256(text.encode()).hexdigest()


def _read_proc_cmdline(pid: int) -> list[str] | None:
    """Read argv from Linux /proc if available."""
    path = Path(f"/proc/{pid}/cmdline")
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _read_proc_start_time(pid: int) -> float | None:
    """Read process start time as a Unix timestamp from Linux /proc."""
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
        stat_tail = stat_text.rsplit(") ", 1)[1]
        stat_fields = stat_tail.split()
        start_ticks = int(stat_fields[19])
        ticks_per_second = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        btime = None
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        if btime is None:
            return None
        return btime + (start_ticks / ticks_per_second)
    except (OSError, IndexError, ValueError, KeyError):
        return None


def _read_ps_field(pid: int, field: str) -> str | None:
    """Read a single ps field while capturing output for the curses TUI."""
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", f"{field}="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _read_ps_start_time(pid: int) -> float | None:
    """Estimate process start time from ps elapsed seconds."""
    value = _read_ps_field(pid, "etimes")
    if value is not None:
        try:
            return time.time() - int(value)
        except ValueError:
            pass

    value = _read_ps_field(pid, "lstart")
    if value is not None:
        try:
            return datetime.strptime(value, "%a %b %d %H:%M:%S %Y").timestamp()
        except ValueError:
            pass

    return None


def _process_value(process: dict, key: str) -> str | None:
    value = process.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _argv_mentions_expected_agent(argv: list[str], expected_argv0: str) -> bool:
    if Path(argv[0]).name == expected_argv0:
        return True
    return any(Path(arg).name == expected_argv0 for arg in argv[1:3])


def _command_mentions_expected_agent(command: str, expected_argv0: str) -> bool:
    if _command_text_argv0(command) == expected_argv0:
        return True
    return f"/{expected_argv0}" in command or f" {expected_argv0}" in command


def _process_observed_argv0(process: dict) -> str | None:
    return _process_value(process, "observed_argv0") or _process_value(
        process,
        "observed_command_argv0",
    )


def _process_observed_cmdline_digest(process: dict) -> str | None:
    return _process_value(process, "observed_cmdline_sha256")


def _process_observed_command_digest(process: dict) -> str | None:
    return _process_value(process, "observed_command_sha256")


def _process_requested_cmdline_digest(process: dict) -> str | None:
    return _process_value(process, "cmdline_sha256")


def _metadata_process(metadata: dict) -> dict:
    process = metadata.get("process", {})
    if isinstance(process, dict):
        return process
    return {}


def _required_process_args(process: dict) -> list[str]:
    required_args = process.get("required_args")
    if isinstance(required_args, list) and all(
        isinstance(arg, str) for arg in required_args
    ):
        return required_args
    return list(EXPECTED_AGENT_ARGS)


def _expected_process_argv0(process: dict) -> str:
    argv0 = (
        _process_observed_argv0(process)
        or _process_value(process, "argv0")
        or EXPECTED_AGENT_ARGV0
    )
    return argv0


def _missing_required_args_in_argv(
    argv: list[str],
    required_args: list[str],
) -> list[str]:
    return [arg for arg in required_args if arg not in argv]


def _missing_required_args_in_command(
    command: str,
    required_args: list[str],
) -> list[str]:
    return [arg for arg in required_args if arg not in command]


def _digest_matches(value: str, expected_digest: str | None) -> bool:
    if expected_digest is None:
        return False
    return _text_digest(value) == expected_digest


def _cmdline_digest_matches(argv: list[str], expected_digest: str | None) -> bool:
    if expected_digest is None:
        return False
    return _cmdline_digest(argv) == expected_digest


def _legacy_expected_process_metadata(
    process: dict,
) -> tuple[str, list[str], str | None]:
    """Return requested argv0, required args, and optional requested digest."""
    argv0 = _process_value(process, "argv0") or EXPECTED_AGENT_ARGV0
    return argv0, _required_process_args(process), _process_requested_cmdline_digest(
        process,
    )


def _live_argv_matches_process(
    metadata: dict,
    live: _LiveProcessInfo,
    process: dict,
) -> tuple[bool, str]:
    if not live.argv:
        return False, "cannot_verify_command"

    observed_digest = _process_observed_cmdline_digest(process)
    if (
        live.argv_source == "proc"
        and _cmdline_digest_matches(live.argv, observed_digest)
    ):
        return True, "matched"

    requested_argv0, required_args, requested_digest = _legacy_expected_process_metadata(
        process,
    )
    if live.argv_source == "proc" and requested_digest is not None:
        if not _cmdline_digest_matches(live.argv, requested_digest):
            return False, "cmdline_digest_mismatch"
        return True, "matched"

    expected_argv0 = _expected_process_argv0(process) or requested_argv0
    if not _argv_mentions_expected_agent(live.argv, expected_argv0):
        return False, "argv0_mismatch"

    if _missing_required_args_in_argv(live.argv, required_args):
        return False, "missing_expected_args"
    if not _argv_contains_expected_outcome(metadata, live.argv):
        return False, "missing_expected_outcome_path"
    return True, "matched"


def _live_command_matches_process(
    metadata: dict,
    live: _LiveProcessInfo,
    process: dict,
) -> tuple[bool, str]:
    if not live.command:
        return False, "cannot_verify_command"

    observed_digest = _process_observed_command_digest(process)
    if _digest_matches(live.command, observed_digest):
        return True, "matched"

    requested_argv0, required_args, _ = _legacy_expected_process_metadata(process)
    expected_argv0 = _expected_process_argv0(process) or requested_argv0
    if not _command_mentions_expected_agent(live.command, expected_argv0):
        return False, "argv0_mismatch"
    if _missing_required_args_in_command(live.command, required_args):
        return False, "missing_expected_args"
    if not _command_contains_expected_outcome(metadata, live.command):
        return False, "missing_expected_outcome_path"
    return True, "matched"


def _read_live_process_info(pid: int) -> _LiveProcessInfo | None:
    """Collect process identity details for PID-reuse-safe validation."""
    if not _process_is_alive(pid):
        return None

    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None
    try:
        sid = os.getsid(pid)
    except OSError:
        sid = None

    argv = _read_proc_cmdline(pid)
    argv_source = "proc" if argv else None
    command = None
    if argv is None:
        command = _read_ps_field(pid, "command")

    started_at_unix = _read_proc_start_time(pid)
    if started_at_unix is None:
        started_at_unix = _read_ps_start_time(pid)

    return _LiveProcessInfo(
        pid=pid,
        pgid=pgid,
        sid=sid,
        argv=argv,
        argv_source=argv_source,
        command=command,
        started_at_unix=started_at_unix,
    )


def _metadata_started_at_unix(metadata: dict) -> float | None:
    """Return the recorded process start timestamp from current or legacy metadata."""
    process = metadata.get("process", {})
    if isinstance(process, dict):
        started_at_unix = process.get("started_at_unix")
        if isinstance(started_at_unix, (int, float)):
            return float(started_at_unix)

    started_at = metadata.get("started_at")
    if isinstance(started_at, str):
        try:
            parsed = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc).timestamp()

    return None


def _argv_contains_expected_outcome(metadata: dict, argv: list[str]) -> bool:
    expected_outcome_path = metadata.get("expected_outcome_path")
    if not isinstance(expected_outcome_path, str) or not expected_outcome_path:
        return True
    return any(expected_outcome_path in arg for arg in argv)


def _command_contains_expected_outcome(metadata: dict, command: str) -> bool:
    expected_outcome_path = metadata.get("expected_outcome_path")
    if not isinstance(expected_outcome_path, str) or not expected_outcome_path:
        return True
    return expected_outcome_path in command


def _command_text_argv0(command: str) -> str | None:
    first = command.strip().split(maxsplit=1)[0] if command.strip() else ""
    return Path(first).name if first else None


def _live_process_command_matches(
    metadata: dict,
    live: _LiveProcessInfo,
) -> tuple[bool, str]:
    """Verify the live process command still looks like the recorded agent."""
    process = _metadata_process(metadata)
    if live.argv:
        return _live_argv_matches_process(metadata, live, process)
    if live.command:
        return _live_command_matches_process(metadata, live, process)
    return False, "cannot_verify_command"


def _recorded_process_is_expected(metadata: dict) -> tuple[bool, str]:
    """Verify recorded PID/PGID still belongs to the original Claude agent."""
    pid = metadata.get("pid")
    if not isinstance(pid, int):
        return False, "metadata_missing_pid"

    expected_pgid = metadata.get("pgid")
    if not isinstance(expected_pgid, int):
        expected_pgid = pid

    live = _read_live_process_info(pid)
    if live is None:
        return False, "process_not_alive"

    if live.pgid is None:
        return False, "cannot_verify_pgid"
    if live.pgid != expected_pgid:
        return False, "pgid_mismatch"

    if live.sid is None:
        return False, "cannot_verify_sid"
    if live.sid != expected_pgid:
        return False, "sid_mismatch"

    expected_started_at = _metadata_started_at_unix(metadata)
    if expected_started_at is None:
        return False, "metadata_missing_start_time"
    if live.started_at_unix is None:
        return False, "cannot_verify_start_time"
    if (
        abs(live.started_at_unix - expected_started_at)
        > PROCESS_START_TOLERANCE_SECONDS
    ):
        return False, "start_time_mismatch"

    return _live_process_command_matches(metadata, live)


def _terminate_recorded_group(
    metadata: dict,
    *,
    timeout: int = 5,
) -> _RecordedTerminationResult:
    """Terminate a process group from running metadata after identity checks."""
    pid = metadata.get("pid")
    pgid = metadata.get("pgid") or pid
    if not isinstance(pid, int) and not isinstance(pgid, int):
        return _RecordedTerminationResult(
            attempted=False,
            skip_reason="metadata_missing_pid_or_pgid",
        )

    matches, reason = _recorded_process_is_expected(metadata)
    if not matches:
        return _RecordedTerminationResult(
            attempted=False,
            safe_to_forget=(reason == "process_not_alive"),
            skip_reason=reason,
        )

    try:
        if isinstance(pgid, int):
            os.killpg(pgid, signal.SIGTERM)
        elif isinstance(pid, int):
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass

    deadline = time.monotonic() + timeout
    while isinstance(pid, int) and time.monotonic() < deadline:
        if not _process_is_alive(pid):
            return _RecordedTerminationResult(attempted=True)
        time.sleep(0.05)

    hard_killed = False
    if isinstance(pid, int) and _process_is_alive(pid):
        hard_killed = True
        try:
            if isinstance(pgid, int):
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass

    still_alive = isinstance(pid, int) and _process_is_alive(pid)
    return _RecordedTerminationResult(
        attempted=True,
        hard_killed=hard_killed,
        safe_to_forget=not still_alive,
        skip_reason="process_still_alive_after_kill" if still_alive else None,
    )


def terminate_running_metadata_for_task(
    ralpanda_dir: Path,
    task_id: str,
    attempt: int,
    history_file: Path,
    *,
    reason: str,
) -> bool:
    """Terminate process groups recorded for a task attempt.

    Returns False if a live recorded process could not be safely verified or
    terminated. In that case, the metadata is left in place so startup recovery
    fails closed instead of risking PID/PGID reuse.
    """
    root = (
        ralpanda_dir
        / "running"
        / dag.task_safe_id(task_id)
        / f"attempt-{attempt}"
    )
    if not root.exists():
        return True

    all_safe = True
    for path in sorted(root.rglob("*.json")):
        metadata, error = dag.read_json_file(path)
        if error or not metadata:
            path.unlink(missing_ok=True)
            continue
        result = _terminate_recorded_group(metadata)
        agent_info = metadata.get("agent", {})
        event_name = (
            "agent_recorded_process_killed"
            if result.attempted and result.safe_to_forget
            else "agent_recorded_process_kill_skipped"
        )
        dag.log_event(
            history_file,
            event_name,
            task_id,
            json.dumps({
                "reason": reason,
                "agent_namespace": agent_info.get("namespace"),
                "metadata_path": str(path),
                "kill_attempted": result.attempted,
                "hard_killed": result.hard_killed,
                "safe_to_forget": result.safe_to_forget,
                "skip_reason": result.skip_reason,
            }, sort_keys=True),
        )
        if result.safe_to_forget:
            path.unlink(missing_ok=True)
        else:
            all_safe = False
    return all_safe


# ---------------------------------------------------------------------------
# Outcome collection
# ---------------------------------------------------------------------------

def collect_expected_outcome(
    ralpanda_dir: Path,
    task_id: str,
    attempt: int,
    agent_namespace: str,
    agent_kind: str,
) -> tuple[dict | None, str | None, Path]:
    """Read and validate the exact outcome expected for an agent."""
    return dag.read_valid_outcome(
        ralpanda_dir,
        task_id,
        attempt,
        agent_namespace,
        agent_kind,
    )


def _outcome_signature(path: Path) -> tuple[int, int] | None:
    """Return a cheap stability signature for an outcome file."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def stable_outcome_ready(
    proc: subprocess.Popen,
    ralpanda_dir: Path,
    task_id: str,
    attempt: int,
    agent_namespace: str,
    agent_kind: str,
) -> tuple[dict | None, Path | None]:
    """Return a valid outcome once it remains unchanged for the grace period."""
    outcome, error, path = collect_expected_outcome(
        ralpanda_dir,
        task_id,
        attempt,
        agent_namespace,
        agent_kind,
    )
    if error or outcome is None:
        proc._outcome_stability_signature = None  # type: ignore[attr-defined]
        proc._outcome_stability_since = None  # type: ignore[attr-defined]
        return None, path

    signature = _outcome_signature(path)
    if signature is None:
        return None, path

    now = time.monotonic()
    old_signature = getattr(proc, "_outcome_stability_signature", None)
    if signature != old_signature:
        proc._outcome_stability_signature = signature  # type: ignore[attr-defined]
        proc._outcome_stability_since = now  # type: ignore[attr-defined]
        return None, path

    since = getattr(proc, "_outcome_stability_since", None)
    if since is None:
        proc._outcome_stability_since = now  # type: ignore[attr-defined]
        return None, path

    if now - since < OUTCOME_STABLE_GRACE_SECONDS:
        return None, path

    return outcome, path


def terminate_stale_process_after_outcome(
    proc: subprocess.Popen,
    history_file: Path,
    task_id: str,
    *,
    agent_namespace: str,
    outcome_path: Path,
) -> None:
    """Kill a process group after a valid stable outcome became authoritative."""
    hard_killed = terminate_agent_process(proc)
    dag.log_event(
        history_file,
        "agent_stale_process_killed",
        task_id,
        json.dumps({
            "reason": "stable_outcome_present",
            "agent_namespace": agent_namespace,
            "grace_seconds": OUTCOME_STABLE_GRACE_SECONDS,
            "signal_path": str(outcome_path),
            "hard_killed": hard_killed,
        }, sort_keys=True),
    )


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
) -> dict | None:
    """Handle everything after a work agent exits.

    Reads outcome file, updates tasks.json, handles splits, commits.
    Returns hook metadata when the task reaches a final status.
    """
    finished = False

    # Get current attempt count
    tasks_data = dag.load_tasks(tasks_file)
    task = dag.get_task(tasks_data["tasks"], task_id)
    current_attempt = task.get("attempt", 1) if task else 1
    agent_namespace = dag.work_agent_namespace()
    outcome, outcome_error, _ = collect_expected_outcome(
        ralpanda_dir,
        task_id,
        current_attempt,
        agent_namespace,
        "work",
    )

    if outcome:
        status = outcome.get("status")

        # Write outcome to tasks.json
        dag.update_task_outcome(tasks_file, task_id, outcome)

        if exit_code != 0:
            dag.log_event(
                history_file,
                "agent_nonzero_after_valid_outcome",
                task_id,
                f"exit_code={exit_code}",
            )

        if status == "split":
            # Process split
            split_into = outcome.get("payload", {}).get("split_into", [])
            if split_into:
                _process_split(ralpanda_dir, tasks_file, task_id, split_into, history_file)
                finished = True
            else:
                if current_attempt >= max_attempts:
                    dag.update_task_status(tasks_file, task_id, "failed")
                    dag.log_event(
                        history_file,
                        "task_failed",
                        task_id,
                        "split outcome did not include payload.split_into",
                    )
                    finished = True
                else:
                    dag.update_task_status(tasks_file, task_id, "pending")
                    dag.log_event(
                        history_file,
                        "task_retry",
                        task_id,
                        f"invalid_split_outcome,attempt={current_attempt}",
                    )
        elif status == "failed":
            # Agent reported failure in outcome
            if current_attempt >= max_attempts:
                dag.update_task_status(tasks_file, task_id, "failed")
                dag.log_event(history_file, "task_failed", task_id, "agent_reported_failure")
                finished = True
            else:
                dag.update_task_status(tasks_file, task_id, "pending")
                dag.log_event(history_file, "task_retry", task_id, f"attempt={current_attempt}")
        else:
            # Done
            dag.update_task_status(tasks_file, task_id, "done")
            dag.log_event(history_file, "task_completed", task_id)
            finished = True
    else:
        # Missing or invalid outcome is an agent infrastructure failure.
        if current_attempt >= max_attempts:
            dag.update_task_status(tasks_file, task_id, "failed")
            dag.log_event(
                history_file, "task_failed", task_id,
                (
                    f"exit_code={exit_code},max_attempts_reached,"
                    f"outcome_error={outcome_error}"
                ),
            )
            finished = True
        else:
            dag.update_task_status(tasks_file, task_id, "pending")
            dag.log_event(
                history_file, "task_retry", task_id,
                (
                    f"exit_code={exit_code},attempt={current_attempt},"
                    f"outcome_error={outcome_error}"
                ),
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

    if finished:
        return {"commit_sha": sha}
    return None


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
    attempt: int = 1
    task_type: str = "review"
    plan_source: str = ""
    stages: list[dict] = field(default_factory=list)
    current_stage_idx: int = 0
    parallel_indices: list[int] = field(default_factory=list)
    isolated_indices: list[int] = field(default_factory=list)
    phase: str = "init"  # init -> parallel -> isolated -> collecting -> coordinator -> done
    parallel_procs: dict[int, subprocess.Popen] = field(default_factory=dict)
    current_isolated_idx: int = -1
    current_isolated_proc: subprocess.Popen | None = None
    check_results: list[dict] = field(default_factory=list)
    collected_indices: set[int] = field(default_factory=set)
    failed_checks: list[dict] = field(default_factory=list)
    failed_analyses: list[str] = field(default_factory=list)
    infra_failed_checks: list[str] = field(default_factory=list)
    skipped_checks: list[str] = field(default_factory=list)
    coordinator_proc: subprocess.Popen | None = None
    coordinator_attempt: int = 0
    coordinator_max_attempts: int = COORDINATOR_DEFAULT_MAX_ATTEMPTS
    coordinator_max_turns: int = COORDINATOR_DEFAULT_MAX_TURNS

    def __post_init__(self) -> None:
        if not self.stages and self.checks:
            self.stages = [{
                "name": "checks",
                "check_indices": list(range(len(self.checks))),
            }]

        if self.phase == "done":
            return

        self.prepare_current_stage()

    def current_stage(self) -> dict | None:
        """Return the current stage metadata, if any."""
        if self.current_stage_idx >= len(self.stages):
            return None
        return self.stages[self.current_stage_idx]

    def current_stage_name(self) -> str:
        """Return the current stage name for logs/outcomes."""
        stage = self.current_stage()
        if not stage:
            return "checks"
        return stage.get("name") or f"stage-{self.current_stage_idx + 1}"

    def current_stage_indices(self) -> list[int]:
        """Return check indices for the current stage."""
        stage = self.current_stage()
        if not stage:
            return []
        return list(stage.get("check_indices", []))

    def stage_name_for_index(self, check_index: int) -> str:
        """Return the stage name containing a check index."""
        for stage_num, stage in enumerate(self.stages, start=1):
            if check_index in stage.get("check_indices", []):
                return stage.get("name") or f"stage-{stage_num}"
        return "checks"

    def prepare_current_stage(self) -> None:
        """Populate mode-specific indices for the current stage."""
        self.parallel_indices = []
        self.isolated_indices = []
        self.parallel_procs = {}
        self.current_isolated_idx = -1
        self.current_isolated_proc = None

        indices = self.current_stage_indices()
        for i in indices:
            if i < 0 or i >= len(self.checks):
                continue
            check = self.checks[i]
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

    def advance_stage(self) -> bool:
        """Move to the next stage. Returns False when there are no stages left."""
        self.current_stage_idx += 1
        if self.current_stage_idx >= len(self.stages):
            return False
        self.prepare_current_stage()
        return True


def start_review(
    ralpanda_dir: Path,
    tasks_file: Path,
    task_id: str,
    model: str,
) -> ReviewState:
    """Initialize review and launch parallel checks."""
    tasks_data = dag.load_tasks(tasks_file)
    task = dag.get_task(tasks_data["tasks"], task_id)
    checks, stages = dag.review_check_stages(task)
    task_type = task.get("type", "review") if task else "review"
    plan_source = task.get("plan_source", "") if task else ""
    attempt = task.get("attempt", 1) if task else 1

    if not checks:
        return ReviewState(task_id=task_id, checks=[], attempt=attempt, phase="done")

    base_sha = git.get_base_sha(ralpanda_dir)
    state = ReviewState(
        task_id=task_id,
        checks=checks,
        attempt=attempt,
        task_type=task_type,
        plan_source=plan_source,
        stages=stages,
    )

    _launch_current_stage(state, ralpanda_dir, model, base_sha)

    return state


def recover_review_task_from_outcomes(
    ralpanda_dir: Path,
    tasks_file: Path,
    task_id: str,
    history_file: Path,
) -> bool:
    """Recover a running review task entirely from current-attempt outcomes."""
    tasks_data = dag.load_tasks(tasks_file)
    task = dag.get_task(tasks_data["tasks"], task_id)
    if not task:
        return False

    checks, stages = dag.review_check_stages(task)
    task_type = task.get("type", "review")
    state = ReviewState(
        task_id=task_id,
        checks=checks,
        attempt=task.get("attempt", 1),
        task_type=task_type,
        plan_source=task.get("plan_source", ""),
        stages=stages,
    )

    if not checks:
        _finalize_review_pass(state, tasks_file, history_file)
        return True

    for stage_idx, stage in enumerate(state.stages):
        state.current_stage_idx = stage_idx
        stage_indices = list(stage.get("check_indices", []))
        for check_index in stage_indices:
            if check_index < 0 or check_index >= len(state.checks):
                return False
            check = state.checks[check_index]
            agent_kind, agent_namespace, _ = _check_agent_signal(
                state,
                ralpanda_dir,
                check_index,
                check,
            )
            outcome, error, _ = collect_expected_outcome(
                ralpanda_dir,
                state.task_id,
                state.attempt,
                agent_namespace,
                agent_kind,
            )
            if error or not outcome:
                return False
            _record_check_outcome(state, check_index, outcome)

        stage_results = [
            r for r in state.check_results
            if r.get("index") in set(stage_indices)
        ]
        fail_count = sum(1 for r in stage_results if r["status"] == "fail")
        infra_count = sum(1 for r in stage_results if r["status"] == "infra_fail")
        if fail_count == 0 and infra_count == 0:
            continue

        _mark_remaining_stages_skipped(
            state,
            f"Skipped because review stage '{state.current_stage_name()}' did not pass.",
        )

        if state.task_type == "review_compatibility":
            _finalize_review_compatibility_blocker(
                state,
                ralpanda_dir,
                tasks_file,
                history_file,
            )
            return True

        if fail_count > 0:
            return _recover_coordinator_from_outcomes(
                state,
                ralpanda_dir,
                tasks_file,
                history_file,
            )

        _finalize_review_infra_fail(
            state,
            ralpanda_dir,
            tasks_file,
            history_file,
        )
        return True

    _finalize_review_pass(state, tasks_file, history_file)
    return True


def _recover_coordinator_from_outcomes(
    state: ReviewState,
    ralpanda_dir: Path,
    tasks_file: Path,
    history_file: Path,
) -> bool:
    """Recover a failed review by reading coordinator outcome attempts."""
    state.coordinator_max_attempts = _coordinator_config_int(
        ralpanda_dir,
        "coordinator_max_attempts",
        COORDINATOR_DEFAULT_MAX_ATTEMPTS,
    )
    last_error = ""
    last_terminal_attempt = 0

    for coordinator_attempt in range(1, state.coordinator_max_attempts + 1):
        state.coordinator_attempt = coordinator_attempt
        agent_namespace = dag.coordinator_agent_namespace(coordinator_attempt)
        outcome, outcome_error, _ = collect_expected_outcome(
            ralpanda_dir,
            state.task_id,
            state.attempt,
            agent_namespace,
            "coordinator",
        )
        if outcome_error or not outcome:
            break
        last_terminal_attempt = coordinator_attempt
        fixup_tasks, parse_error = _coordinator_tasks_from_outcome_for_insertion(
            outcome,
            outcome_error,
            state,
            tasks_file,
        )
        if fixup_tasks:
            _write_review_outcome(state, tasks_file)
            _insert_fixups_and_clone(
                state,
                ralpanda_dir,
                tasks_file,
                history_file,
                fixup_tasks=fixup_tasks,
            )
            return True
        last_error = parse_error or "coordinator did not produce tasks"

    if last_terminal_attempt >= state.coordinator_max_attempts:
        state.coordinator_attempt = last_terminal_attempt
        _write_review_outcome(state, tasks_file, coordinator_error=last_error)
        _insert_coordinator_failure_pause_and_clone(
            state,
            ralpanda_dir,
            tasks_file,
            history_file,
            last_error,
        )
        return True

    return False


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
        # Check if all parallel checks have terminal outcomes.
        all_done = True
        for i, proc in list(state.parallel_procs.items()):
            if _poll_check_proc(
                state,
                ralpanda_dir,
                i,
                proc,
                history_file,
            ):
                state.parallel_procs.pop(i, None)
            else:
                all_done = False
        if not all_done:
            return False

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
        if proc and _poll_check_proc(
            state,
            ralpanda_dir,
            state.current_isolated_idx,
            proc,
            history_file,
        ):
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
        stage_name = state.current_stage_name()
        stage_indices = set(state.current_stage_indices())
        stage_results = [
            r for r in state.check_results
            if r.get("index") in stage_indices
        ]
        if len(stage_results) < len(stage_indices):
            missing = sorted(stage_indices - set(state.collected_indices))
            for i in missing:
                _record_check_infra_failure(
                    state,
                    i,
                    "No terminal outcome was collected for this check.",
                )
            stage_results = [
                r for r in state.check_results
                if r.get("index") in stage_indices
            ]

        fail_count = sum(1 for r in stage_results if r["status"] == "fail")
        infra_count = sum(1 for r in stage_results if r["status"] == "infra_fail")

        if fail_count == 0 and infra_count == 0:
            if state.advance_stage():
                _launch_current_stage(state, ralpanda_dir, model)
                return False

            # All stages passed
            _finalize_review_pass(state, tasks_file, history_file)
            state.phase = "done"
            return True

        _mark_remaining_stages_skipped(
            state,
            f"Skipped because review stage '{stage_name}' did not pass.",
        )

        if state.task_type == "review_compatibility":
            state.phase = "done"
            _finalize_review_compatibility_blocker(
                state, ralpanda_dir, tasks_file, history_file,
            )
            return True

        if fail_count > 0:
            # Need coordinator to create fix-up tasks
            state.phase = "coordinator"
            if not _launch_coordinator(state, ralpanda_dir, tasks_file, model):
                state.phase = "done"
                error = "coordinator could not start because review task was missing"
                dag.log_event(
                    history_file, "review_fixup_failed", state.task_id, error,
                )
                _write_review_outcome(state, tasks_file, coordinator_error=error)
                return True
        else:
            # Only infra fails — insert pause + cloned review
            state.phase = "done"
            _finalize_review_infra_fail(
                state, ralpanda_dir, tasks_file, history_file,
            )
            return True

    if state.phase == "coordinator":
        proc = state.coordinator_proc
        if proc and _poll_coordinator_proc(
            state,
            ralpanda_dir,
            proc,
            history_file,
        ):
            state.coordinator_proc = None
            done = _process_coordinator_result(
                state, ralpanda_dir, tasks_file, history_file, model,
            )
            if done:
                state.phase = "done"
                return True
            return False
        return False

    return state.phase == "done"


def _launch_current_stage(
    state: ReviewState,
    ralpanda_dir: Path,
    model: str,
    base_sha: str | None = None,
) -> None:
    """Launch checks for the current stage."""
    if base_sha is None:
        base_sha = git.get_base_sha(ralpanda_dir)

    if state.phase == "parallel":
        for i in state.parallel_indices:
            check = state.checks[i]
            log_path = dag.task_log_path(ralpanda_dir, state.task_id, check["name"])
            agent_kind, agent_namespace, outcome_path = _check_agent_signal(
                state, ralpanda_dir, i, check,
            )
            p = _build_check_prompt(
                state,
                check,
                "parallel",
                base_sha,
                outcome_path,
                agent_kind,
                agent_namespace,
            )
            proc = spawn_agent(
                p, model, log_path,
                ralpanda_dir=ralpanda_dir,
                task_id=state.task_id,
                attempt=state.attempt,
                agent_kind=agent_kind,
                agent_namespace=agent_namespace,
                expected_outcome_path=outcome_path,
                allowed_tools="Read Glob Grep Bash Write",
                disallowed_tools="Edit NotebookEdit",
            )
            state.parallel_procs[i] = proc
    elif state.phase == "isolated":
        _launch_next_isolated(
            state, ralpanda_dir, state.task_id, state.checks, model,
        )


def _launch_next_isolated(
    state: ReviewState,
    ralpanda_dir: Path,
    task_id: str,
    checks: list[dict],
    model: str,
) -> bool:
    """Launch the next isolated check. Returns False if none remaining."""
    # Find next un-launched isolated index
    for i in state.isolated_indices:
        if i <= state.current_isolated_idx:
            continue
        check = checks[i]
        log_path = dag.task_log_path(ralpanda_dir, task_id, check["name"])
        base_sha = git.get_base_sha(ralpanda_dir)
        agent_kind, agent_namespace, outcome_path = _check_agent_signal(
            state, ralpanda_dir, i, check,
        )
        p = _build_check_prompt(
            state,
            check,
            "isolated",
            base_sha,
            outcome_path,
            agent_kind,
            agent_namespace,
        )
        state.current_isolated_proc = spawn_agent(
            p,
            model,
            log_path,
            ralpanda_dir=ralpanda_dir,
            task_id=state.task_id,
            attempt=state.attempt,
            agent_kind=agent_kind,
            agent_namespace=agent_namespace,
            expected_outcome_path=outcome_path,
        )
        state.current_isolated_idx = i
        return True
    return False


def _check_agent_signal(
    state: ReviewState,
    ralpanda_dir: Path,
    check_index: int,
    check: dict,
) -> tuple[str, str, Path]:
    """Return kind, namespace, and expected outcome path for a check agent."""
    agent_kind = dag.agent_kind_for_check_task(state.task_type)
    agent_namespace = dag.review_agent_namespace(
        state.task_type,
        state.stage_name_for_index(check_index),
        check.get("name"),
    )
    outcome_path = dag.outcome_path(
        ralpanda_dir,
        state.task_id,
        state.attempt,
        agent_namespace,
    ).resolve()
    return agent_kind, agent_namespace, outcome_path


def _build_check_prompt(
    state: ReviewState,
    check: dict,
    mode: str,
    base_sha: str | None,
    outcome_path: Path,
    agent_kind: str,
    agent_namespace: str,
) -> str:
    """Build the right check-agent prompt for the review task subtype."""
    if state.task_type == "review_compatibility":
        return prompt.build_review_compatibility_prompt(
            check.get("name", "compatibility"),
            check.get("prompt", ""),
            mode,
            state.task_id,
            state.attempt,
            state.plan_source,
            check.get("for_review_check"),
            check.get("review_prompt"),
            str(outcome_path),
            agent_kind,
            agent_namespace,
        )
    return prompt.build_review_check_prompt(
        check["name"], check["prompt"], mode,
        state.task_id, state.attempt, base_sha,
        str(outcome_path), agent_kind, agent_namespace,
    )


def _poll_check_proc(
    state: ReviewState,
    ralpanda_dir: Path,
    check_index: int,
    proc: subprocess.Popen,
    history_file: Path,
) -> bool:
    """Poll one review/check process. Returns True once terminal."""
    if check_index in state.collected_indices:
        finish_agent_process(proc)
        return True

    check = state.checks[check_index]
    agent_kind, agent_namespace, outcome_path = _check_agent_signal(
        state,
        ralpanda_dir,
        check_index,
        check,
    )

    exit_code = proc.poll()
    if exit_code is not None:
        finish_agent_process(proc)
        outcome, error, _ = collect_expected_outcome(
            ralpanda_dir,
            state.task_id,
            state.attempt,
            agent_namespace,
            agent_kind,
        )
        if outcome:
            if exit_code != 0:
                dag.log_event(
                    history_file,
                    "agent_nonzero_after_valid_outcome",
                    state.task_id,
                    (
                        f"agent={agent_namespace},"
                        f"exit_code={exit_code}"
                    ),
                )
            _record_check_outcome(state, check_index, outcome)
        else:
            _record_check_infra_failure(
                state,
                check_index,
                (
                    "Agent process exited without a valid outcome file: "
                    f"{error or 'unknown error'}"
                ),
            )
        return True

    outcome, stable_path = stable_outcome_ready(
        proc,
        ralpanda_dir,
        state.task_id,
        state.attempt,
        agent_namespace,
        agent_kind,
    )
    if outcome and stable_path:
        terminate_stale_process_after_outcome(
            proc,
            history_file,
            state.task_id,
            agent_namespace=agent_namespace,
            outcome_path=stable_path,
        )
        finish_agent_process(proc)
        _record_check_outcome(state, check_index, outcome)
        return True

    return False


def _poll_coordinator_proc(
    state: ReviewState,
    ralpanda_dir: Path,
    proc: subprocess.Popen,
    history_file: Path,
) -> bool:
    """Poll the fix-up coordinator process. Returns True once terminal."""
    agent_namespace = dag.coordinator_agent_namespace(state.coordinator_attempt)
    outcome_path = dag.outcome_path(
        ralpanda_dir,
        state.task_id,
        state.attempt,
        agent_namespace,
    ).resolve()
    exit_code = proc.poll()
    if exit_code is not None:
        finish_agent_process(proc)
        outcome, error, _ = collect_expected_outcome(
            ralpanda_dir,
            state.task_id,
            state.attempt,
            agent_namespace,
            "coordinator",
        )
        if outcome and exit_code != 0:
            dag.log_event(
                history_file,
                "agent_nonzero_after_valid_outcome",
                state.task_id,
                f"agent={agent_namespace},exit_code={exit_code}",
            )
        elif error:
            dag.log_event(
                history_file,
                "coordinator_missing_or_invalid_outcome",
                state.task_id,
                error,
            )
        return True

    outcome, stable_path = stable_outcome_ready(
        proc,
        ralpanda_dir,
        state.task_id,
        state.attempt,
        agent_namespace,
        "coordinator",
    )
    if outcome and stable_path:
        terminate_stale_process_after_outcome(
            proc,
            history_file,
            state.task_id,
            agent_namespace=agent_namespace,
            outcome_path=stable_path,
        )
        finish_agent_process(proc)
        return True

    # Keep the expected path alive for observability even before stability.
    outcome_path.parent.mkdir(parents=True, exist_ok=True)
    return False


def _record_check_outcome(
    state: ReviewState,
    check_index: int,
    outcome: dict,
) -> None:
    """Merge one validated check outcome into ReviewState."""
    if check_index in state.collected_indices:
        return
    check = state.checks[check_index]
    name = check.get("name", f"check-{check_index}")
    stage_name = state.stage_name_for_index(check_index)
    status = outcome["status"]
    detail = _check_detail_from_outcome(outcome)

    state.check_results.append({
        "index": check_index,
        "name": name,
        "stage": stage_name,
        "status": status,
        "summary": outcome.get("summary", ""),
        "detail": detail,
        "payload": outcome.get("payload", {}),
        "agent_namespace": outcome.get("agent", {}).get("namespace"),
    })
    if status == "infra_fail":
        state.infra_failed_checks.append(name)
    elif status == "fail":
        failed_check = dict(check)
        failed_check.setdefault("stage", stage_name)
        state.failed_checks.append(failed_check)
        state.failed_analyses.append(detail or outcome.get("summary", ""))
    state.collected_indices.add(check_index)


def _record_check_infra_failure(
    state: ReviewState,
    check_index: int,
    detail: str,
) -> None:
    """Record a terminal infrastructure failure for a check."""
    if check_index in state.collected_indices:
        return
    check = state.checks[check_index]
    name = check.get("name", f"check-{check_index}")
    state.check_results.append({
        "index": check_index,
        "name": name,
        "stage": state.stage_name_for_index(check_index),
        "status": "infra_fail",
        "summary": detail,
        "detail": detail,
    })
    state.infra_failed_checks.append(name)
    state.collected_indices.add(check_index)


def _check_detail_from_outcome(outcome: dict) -> str:
    """Return human-readable detail from a check outcome envelope."""
    payload = outcome.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    parts: list[str] = []
    summary = outcome.get("summary", "")
    if summary:
        parts.append(summary)
    for key in ("what_failed", "remediation", "details", "evidence"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            items = [
                item if isinstance(item, str) else json.dumps(item, sort_keys=True)
                for item in value
            ]
            if items:
                parts.append("\n".join(items))
    if not parts and payload:
        parts.append(json.dumps(payload, indent=2, sort_keys=True))
    return "\n\n".join(parts)


def _mark_remaining_stages_skipped(state: ReviewState, detail: str) -> None:
    """Record unrun checks as skipped after a stage short-circuits review."""
    for i, check in enumerate(state.checks):
        if i in state.collected_indices:
            continue
        name = check["name"]
        state.check_results.append({
            "index": i,
            "name": name,
            "stage": state.stage_name_for_index(i),
            "status": "skipped",
            "detail": detail,
        })
        state.skipped_checks.append(name)
        state.collected_indices.add(i)


def _finalize_review_pass(
    state: ReviewState,
    tasks_file: Path,
    history_file: Path,
) -> None:
    """All checks passed — write outcome and mark done."""
    check_label = (
        "review compatibility checks"
        if state.task_type == "review_compatibility"
        else "review checks"
    )
    outcome = {
        "summary": f"All {len(state.checks)} {check_label} passed.",
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


def _finalize_review_compatibility_blocker(
    state: ReviewState,
    ralpanda_dir: Path,
    tasks_file: Path,
    history_file: Path,
) -> None:
    """Compatibility failed — insert a pause and a cloned compatibility gate."""
    _write_compatibility_outcome(state, tasks_file)
    _insert_compatibility_pause_and_clone(
        state, ralpanda_dir, tasks_file, history_file,
    )


def _coordinator_config_int(
    ralpanda_dir: Path,
    key: str,
    default: int,
) -> int:
    """Read a positive integer coordinator setting from config.json."""
    config_path = ralpanda_dir / "config.json"
    if not config_path.exists():
        return default

    try:
        with open(config_path) as f:
            value = json.load(f).get(key, default)
        value = int(value)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return default

    return value if value > 0 else default


def _launch_coordinator(
    state: ReviewState,
    ralpanda_dir: Path,
    tasks_file: Path,
    model: str,
) -> bool:
    """Spawn the coordinator agent to generate fix-up tasks."""
    tasks_data = dag.load_tasks(tasks_file)
    tasks = tasks_data["tasks"]
    task = dag.get_task(tasks, state.task_id)
    if not task:
        return False

    if state.coordinator_attempt == 0:
        state.coordinator_max_attempts = _coordinator_config_int(
            ralpanda_dir,
            "coordinator_max_attempts",
            COORDINATOR_DEFAULT_MAX_ATTEMPTS,
        )
        state.coordinator_max_turns = _coordinator_config_int(
            ralpanda_dir,
            "coordinator_max_turns",
            COORDINATOR_DEFAULT_MAX_TURNS,
        )

    state.coordinator_attempt += 1
    agent_namespace = dag.coordinator_agent_namespace(state.coordinator_attempt)
    outcome_path = dag.outcome_path(
        ralpanda_dir,
        state.task_id,
        state.attempt,
        agent_namespace,
    ).resolve()

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
        state.attempt,
        str(outcome_path),
        agent_namespace,
    )

    log_path = dag.task_log_path(ralpanda_dir, state.task_id, "coordinator")
    state.coordinator_proc = spawn_agent(
        p, model, log_path,
        ralpanda_dir=ralpanda_dir,
        task_id=state.task_id,
        attempt=state.attempt,
        agent_kind="coordinator",
        agent_namespace=agent_namespace,
        expected_outcome_path=outcome_path,
        max_turns=state.coordinator_max_turns,
        allowed_tools="Bash Write",
    )
    return True


def _process_coordinator_result(
    state: ReviewState,
    ralpanda_dir: Path,
    tasks_file: Path,
    history_file: Path,
    model: str,
) -> bool:
    """Parse coordinator output and insert fix-up tasks + cloned review."""
    _write_review_outcome(state, tasks_file)

    agent_namespace = dag.coordinator_agent_namespace(state.coordinator_attempt)
    outcome, outcome_error, _ = collect_expected_outcome(
        ralpanda_dir,
        state.task_id,
        state.attempt,
        agent_namespace,
        "coordinator",
    )
    fixup_tasks, parse_error = _coordinator_tasks_from_outcome_for_insertion(
        outcome,
        outcome_error,
        state,
        tasks_file,
    )

    if fixup_tasks:
        _insert_fixups_and_clone(
            state, ralpanda_dir, tasks_file, history_file,
            fixup_tasks=fixup_tasks,
        )
        return True

    error = parse_error or "coordinator could not produce tasks"

    if state.coordinator_attempt < state.coordinator_max_attempts:
        dag.log_event(
            history_file, "review_fixup_retry", state.task_id,
            (
                f"attempt={state.coordinator_attempt}/"
                f"{state.coordinator_max_attempts}: {error}"
            ),
        )
        if not _launch_coordinator(state, ralpanda_dir, tasks_file, model):
            error = "coordinator could not restart because review task was missing"
            dag.log_event(
                history_file, "review_fixup_failed", state.task_id, error,
            )
            _write_review_outcome(state, tasks_file, coordinator_error=error)
            return True
        return False

    dag.log_event(
        history_file, "review_fixup_failed", state.task_id,
        f"attempts={state.coordinator_attempt}: {error}",
    )
    _write_review_outcome(state, tasks_file, coordinator_error=error)
    _insert_coordinator_failure_pause_and_clone(
        state, ralpanda_dir, tasks_file, history_file, error,
    )
    return True


def _coordinator_tasks_from_outcome(
    outcome: dict | None,
    outcome_error: str | None,
) -> tuple[list[dict], str | None]:
    """Extract and validate fix-up tasks from a coordinator outcome."""
    if outcome is None:
        return [], outcome_error or "coordinator outcome does not exist"

    status = outcome.get("status")
    if status == "infra_fail":
        return [], outcome.get("summary") or "coordinator reported infra_fail"
    if status != "tasks_created":
        return [], f"coordinator returned unexpected status: {status!r}"

    payload = outcome.get("payload", {})
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list):
        return [], "coordinator outcome payload.tasks must be an array"
    return _validate_coordinator_tasks(tasks)


def _coordinator_tasks_from_outcome_for_insertion(
    outcome: dict | None,
    outcome_error: str | None,
    state: ReviewState,
    tasks_file: Path,
) -> tuple[list[dict], str | None]:
    """Return coordinator tasks only if inserting them preserves DAG integrity."""
    fixup_tasks, error = _coordinator_tasks_from_outcome(outcome, outcome_error)
    if error or not fixup_tasks:
        return fixup_tasks, error

    integrity_error = _validate_coordinator_fixup_insertion(
        state,
        tasks_file,
        fixup_tasks,
    )
    if integrity_error:
        return [], integrity_error

    return fixup_tasks, None


def _validate_coordinator_fixup_insertion(
    state: ReviewState,
    tasks_file: Path,
    fixup_tasks: list[dict],
) -> str | None:
    """Validate the final task graph that coordinator fix-ups would create."""
    tasks_data = dag.load_tasks(tasks_file)
    tasks = tasks_data["tasks"]
    try:
        candidate_tasks = _preview_fixup_inserted_tasks(
            state,
            tasks,
            fixup_tasks,
        )
    except ValueError as exc:
        return str(exc)

    check = dag.validate_tasks(candidate_tasks)
    if check != "valid":
        return f"coordinator fix-up task graph invalid: {check}"
    return None


def _write_review_outcome(
    state: ReviewState,
    tasks_file: Path,
    coordinator_error: str | None = None,
) -> None:
    """Write review outcome to tasks.json."""
    pass_count = sum(1 for r in state.check_results if r["status"] == "pass")
    fail_count = sum(1 for r in state.check_results if r["status"] == "fail")
    infra_count = sum(1 for r in state.check_results if r["status"] == "infra_fail")
    skipped_count = sum(1 for r in state.check_results if r["status"] == "skipped")

    parts = [f"{pass_count} passed"]
    if fail_count:
        parts.append(f"{fail_count} failed")
    if infra_count:
        parts.append(f"{infra_count} infra_fail")
    if skipped_count:
        parts.append(f"{skipped_count} skipped")

    outcome = {
        "summary": ", ".join(parts) + ".",
        "check_results": state.check_results,
    }
    if coordinator_error:
        outcome["summary"] += (
            " Fix-up coordinator failed; inserted a pause before retrying review."
        )
        outcome["fixup_generation"] = {
            "status": "failed",
            "attempts": state.coordinator_attempt,
            "max_attempts": state.coordinator_max_attempts,
            "detail": coordinator_error,
        }
    dag.update_task_outcome(tasks_file, state.task_id, outcome)


def _write_compatibility_outcome(state: ReviewState, tasks_file: Path) -> None:
    """Write review compatibility outcome to tasks.json."""
    violation_count = sum(1 for r in state.check_results if r["status"] == "fail")
    infra_count = sum(1 for r in state.check_results if r["status"] == "infra_fail")
    pass_count = sum(1 for r in state.check_results if r["status"] == "pass")
    skipped_count = sum(1 for r in state.check_results if r["status"] == "skipped")

    parts = [f"{pass_count} passed"]
    if violation_count:
        parts.append(f"{violation_count} compatibility violation")
    if infra_count:
        parts.append(f"{infra_count} infra_fail")
    if skipped_count:
        parts.append(f"{skipped_count} skipped")

    outcome = {
        "summary": "Review compatibility blocked: " + ", ".join(parts) + ".",
        "check_results": state.check_results,
    }
    dag.update_task_outcome(tasks_file, state.task_id, outcome)


def _copy_review_check_fields(task: dict) -> dict:
    """Preserve whichever review check schema the task uses."""
    fields: dict = {}
    if "checks" in task:
        fields["checks"] = task.get("checks", [])
    if "check_stages" in task:
        fields["check_stages"] = task.get("check_stages", [])
    if "review_check_stages" in task:
        fields["review_check_stages"] = task.get("review_check_stages", [])
    if "stages" in task:
        fields["stages"] = task.get("stages", [])
    return fields


def _insert_compatibility_pause_and_clone(
    state: ReviewState,
    ralpanda_dir: Path,
    tasks_file: Path,
    history_file: Path,
) -> None:
    """Insert a human pause and rerun gate after review compatibility blocks."""
    tasks_data = dag.load_tasks(tasks_file)
    tasks = tasks_data["tasks"]
    task = dag.get_task(tasks, state.task_id)
    if not task:
        return

    plan_source = task.get("plan_source", "")
    slug = dag.plan_slug_from_source(plan_source)
    max_num = dag._global_max_num(tasks)
    now = dag._now_iso()

    failed_names = [
        r["name"]
        for r in state.check_results
        if r["status"] in ("fail", "infra_fail")
    ]
    failed_label = ", ".join(failed_names) if failed_names else "unknown"

    max_num += 1
    pause_id = f"ralpanda/{slug}/{max_num:03d}"
    pause_task = {
        "id": pause_id,
        "title": "Pause: review compatibility issue",
        "type": "pause",
        "status": "pending",
        "depends_on": [],
        "plan_source": plan_source,
        "description": (
            "Review compatibility checks blocked the plan. Fix the plan, "
            "the affected review checks, or both, then resume."
        ),
        "acceptance_criteria": [],
        "pause_reason": (
            f"Review compatibility checks blocked the plan: {failed_label}. "
            "Fix the plan or review checks, then resume."
        ),
        "outcome": None,
        "attempt": 0,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
    }

    max_num += 1
    clone_id = f"ralpanda/{slug}/{max_num:03d}"
    clone_task = {
        "id": clone_id,
        "title": task.get("title", "Check review compatibility with plan"),
        "type": "review_compatibility",
        "status": "pending",
        "depends_on": list(dict.fromkeys(task.get("depends_on", []) + [pause_id])),
        "plan_source": plan_source,
        "description": task.get("description", ""),
        "acceptance_criteria": task.get("acceptance_criteria", []),
        "target_review_task_id": task.get("target_review_task_id"),
        "outcome": None,
        "attempt": 0,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
    }
    clone_task.update(_copy_review_check_fields(task))

    dag.insert_tasks_after(tasks_file, state.task_id, [pause_task, clone_task])

    # Rewire: downstream work/review tasks should wait for the cloned gate.
    with dag.locked_tasks(tasks_file) as data:
        for t in data["tasks"]:
            if t["id"] not in (state.task_id, clone_id):
                deps = t.get("depends_on", [])
                if state.task_id in deps:
                    t["depends_on"] = [
                        clone_id if d == state.task_id else d
                        for d in deps
                    ]

    reloaded = dag.load_tasks(tasks_file)
    check = dag.validate_tasks(reloaded["tasks"])
    if check != "valid":
        dag.log_event(
            history_file, "review_compatibility_integrity_failed",
            state.task_id, check,
        )

    dag.log_event(
        history_file, "review_compatibility_pause_inserted",
        state.task_id, f"next_gate={clone_id}",
    )


def _insert_coordinator_failure_pause_and_clone(
    state: ReviewState,
    ralpanda_dir: Path,
    tasks_file: Path,
    history_file: Path,
    reason: str,
) -> None:
    """Insert a human pause and cloned review when fix-up generation fails."""
    tasks_data = dag.load_tasks(tasks_file)
    tasks = tasks_data["tasks"]
    task = dag.get_task(tasks, state.task_id)
    if not task:
        return

    plan_source = task.get("plan_source", "")
    slug = dag.plan_slug_from_source(plan_source)
    existing_pause_deps = _pending_pause_ids(tasks)
    max_num = dag._global_max_num(tasks)
    now = dag._now_iso()

    max_num += 1
    pause_id = f"ralpanda/{slug}/{max_num:03d}"
    log_path = dag.task_log_path(ralpanda_dir, state.task_id, "coordinator")
    pause_task = {
        "id": pause_id,
        "title": "Pause: coordinator could not create fix-up tasks",
        "type": "pause",
        "status": "pending",
        "depends_on": [],
        "plan_source": plan_source,
        "description": (
            "The review failed, but the fix-up coordinator did not produce a "
            f"valid non-empty task array after {state.coordinator_attempt} "
            f"attempts. Inspect {log_path} and either fix the coordinator "
            "prompt/output issue or add remediation tasks manually before "
            "resuming."
        ),
        "acceptance_criteria": [],
        "pause_reason": (
            "Fix-up coordinator failed to create remediation tasks: " + reason
        ),
        "outcome": None,
        "attempt": 0,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
    }

    max_num += 1
    clone_id = f"ralpanda/{slug}/{max_num:03d}"
    clone_task = {
        "id": clone_id,
        "title": task.get("title", "Review"),
        "type": "review",
        "status": "pending",
        "depends_on": list(dict.fromkeys(
            task.get("depends_on", []) + existing_pause_deps + [pause_id],
        )),
        "plan_source": plan_source,
        "description": task.get("description", ""),
        "acceptance_criteria": task.get("acceptance_criteria", []),
        "outcome": None,
        "attempt": 0,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
    }
    clone_task.update(_copy_review_check_fields(task))

    dag.insert_tasks_after(tasks_file, state.task_id, [pause_task, clone_task])

    # Rewire: anything depending on this review now depends on the clone.
    with dag.locked_tasks(tasks_file) as data:
        for t in data["tasks"]:
            if t["id"] not in (state.task_id, clone_id):
                deps = t.get("depends_on", [])
                if state.task_id in deps:
                    t["depends_on"] = [
                        clone_id if d == state.task_id else d
                        for d in deps
                    ]

    reloaded = dag.load_tasks(tasks_file)
    check = dag.validate_tasks(reloaded["tasks"])
    if check != "valid":
        dag.log_event(
            history_file, "coordinator_failure_integrity_failed",
            state.task_id, check,
        )

    dag.log_event(
        history_file, "coordinator_failure_pause_inserted",
        state.task_id,
        f"attempts={state.coordinator_attempt},next_review={clone_id}",
    )


def _pending_pause_ids(tasks: list[dict]) -> list[str]:
    """Return IDs of pending pause tasks that should block newly-added work."""
    return [
        t["id"]
        for t in tasks
        if t.get("type") == "pause" and t.get("status") == "pending"
    ]


def _add_existing_pause_deps(task: dict, pause_ids: list[str]) -> dict:
    """Return a task copy that depends on already-pending pauses."""
    task = dict(task)
    if pause_ids and task.get("type") != "pause":
        task["depends_on"] = list(dict.fromkeys(
            task.get("depends_on", []) + pause_ids,
        ))
    return task


def _build_fixup_insert_tasks(
    state: ReviewState,
    tasks: list[dict],
    task: dict,
    fixup_tasks: list[dict],
) -> tuple[list[dict], str]:
    """Build fix-up tasks, optional pause, and cloned review for insertion."""
    plan_source = task.get("plan_source", "")
    slug = dag.plan_slug_from_source(plan_source)
    existing_pause_deps = _pending_pause_ids(tasks)

    # Find global max ID (across all slugs, including fixup tasks from coordinator)
    max_num = dag._global_max_num(tasks)
    for t in fixup_tasks:
        try:
            n = int(t["id"].split("/")[-1])
            max_num = max(max_num, n)
        except (ValueError, IndexError):
            pass

    now = dag._now_iso()
    all_new_tasks = [
        _add_existing_pause_deps(copy.deepcopy(t), existing_pause_deps)
        for t in fixup_tasks
    ]
    clone_extra_deps = [t["id"] for t in all_new_tasks]

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
        "depends_on": list(dict.fromkeys(
            task.get("depends_on", []) + existing_pause_deps + clone_extra_deps,
        )),
        "plan_source": plan_source,
        "description": task.get("description", ""),
        "acceptance_criteria": task.get("acceptance_criteria", []),
        "outcome": None,
        "attempt": 0,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
    }
    clone_task.update(_copy_review_check_fields(task))
    all_new_tasks.append(clone_task)
    return all_new_tasks, clone_id


def _tasks_with_fixup_inserted(
    tasks: list[dict],
    review_task_id: str,
    all_new_tasks: list[dict],
    clone_id: str,
) -> list[dict]:
    """Return the task list after fix-up insertion and downstream rewiring."""
    copied_tasks = copy.deepcopy(tasks)
    copied_new_tasks = copy.deepcopy(all_new_tasks)
    for i, task in enumerate(copied_tasks):
        if task["id"] == review_task_id:
            updated = copied_tasks[:i + 1] + copied_new_tasks + copied_tasks[i + 1:]
            break
    else:
        raise ValueError(f"review task {review_task_id} is missing from tasks.json")

    # Rewire: anything depending on this review now depends on the clone.
    for task in updated:
        if task["id"] != review_task_id and task["id"] != clone_id:
            deps = task.get("depends_on", [])
            if review_task_id in deps:
                task["depends_on"] = [
                    clone_id if dep_id == review_task_id else dep_id
                    for dep_id in deps
                ]
    return updated


def _preview_fixup_inserted_tasks(
    state: ReviewState,
    tasks: list[dict],
    fixup_tasks: list[dict],
) -> list[dict]:
    """Return the final task graph that fix-up insertion would produce."""
    task = dag.get_task(tasks, state.task_id)
    if not task:
        raise ValueError(f"review task {state.task_id} is missing from tasks.json")
    all_new_tasks, clone_id = _build_fixup_insert_tasks(
        state,
        tasks,
        task,
        fixup_tasks,
    )
    return _tasks_with_fixup_inserted(
        tasks,
        state.task_id,
        all_new_tasks,
        clone_id,
    )


def _insert_fixups_and_clone(
    state: ReviewState,
    ralpanda_dir: Path,
    tasks_file: Path,
    history_file: Path,
    fixup_tasks: list[dict],
) -> None:
    """Insert fix-up tasks, optional pause, and cloned review after current review."""
    if not fixup_tasks and not state.infra_failed_checks:
        dag.log_event(
            history_file, "fixup_insert_skipped", state.task_id,
            "no fix-up tasks or review infra failures",
        )
        return

    inserted = False
    clone_id = ""
    check = "valid"
    with dag.locked_tasks(tasks_file) as data:
        tasks = data["tasks"]
        task = dag.get_task(tasks, state.task_id)
        if not task:
            return

        all_new_tasks, clone_id = _build_fixup_insert_tasks(
            state,
            tasks,
            task,
            fixup_tasks,
        )
        candidate_tasks = _tasks_with_fixup_inserted(
            tasks,
            state.task_id,
            all_new_tasks,
            clone_id,
        )
        check = dag.validate_tasks(candidate_tasks)
        if check == "valid":
            data["tasks"] = candidate_tasks
            inserted = True

    if not inserted:
        dag.log_event(history_file, "fixup_integrity_failed", state.task_id, check)
        return

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


def _validate_coordinator_tasks(data: list) -> tuple[list[dict], str | None]:
    """Validate the minimum schema needed before inserting coordinator tasks."""
    if not data:
        return [], "coordinator returned an empty task array"

    required_fields = {
        "id",
        "title",
        "type",
        "status",
        "depends_on",
        "plan_source",
        "description",
        "acceptance_criteria",
        "outcome",
        "attempt",
        "created_at",
        "started_at",
        "completed_at",
    }

    for i, task in enumerate(data):
        if not isinstance(task, dict):
            return [], f"coordinator task {i} is not an object"

        missing = sorted(required_fields - set(task))
        if missing:
            return [], (
                f"coordinator task {i} is missing fields: {', '.join(missing)}"
            )

        if task.get("type") != "work":
            return [], f"coordinator task {i} type must be 'work'"
        if task.get("status") != "pending":
            return [], f"coordinator task {i} status must be 'pending'"
        if not isinstance(task.get("depends_on"), list):
            return [], f"coordinator task {i} depends_on must be an array"
        if not isinstance(task.get("id"), str) or not task.get("id"):
            return [], f"coordinator task {i} id must be a non-empty string"
        if not all(isinstance(dep_id, str) for dep_id in task["depends_on"]):
            return [], (
                f"coordinator task {i} depends_on entries must be strings"
            )
        if not isinstance(task.get("acceptance_criteria"), list):
            return [], (
                f"coordinator task {i} acceptance_criteria must be an array"
            )

    return data, None
