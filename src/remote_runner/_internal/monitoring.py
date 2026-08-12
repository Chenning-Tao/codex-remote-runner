from __future__ import annotations

import argparse
import json
import re
import shlex
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import (
    CURRENT_MANIFEST_SCHEMA,
    LEGACY_CURRENT_MANIFEST_SCHEMA,
    PREVIOUS_MANIFEST_SCHEMA,
    ProjectPaths,
    can_transition,
    load_current_run,
    load_yaml,
    process_title_privacy_mode,
    remote_status_path_for_log,
    resolve_project_config,
    runtime_path,
    update_current_state,
    utc_now,
)
from .progress import parse_progress
from .remote_shell import shell_quote_remote_path, ssh_capture
from .tmux import exact_tmux_target, run_tmux_session


LOG_STAT_RE = re.compile(
    r"^log_mtime=(?P<mtime>\d+)\s+log_size=(?P<size>\d+)$", re.MULTILINE
)
SUCCESS_MARKER_RE = re.compile(
    r"^\s*\[(?:SUCCESS|RUN_SUCCESS)\](?:\s|$)", re.IGNORECASE
)
FAILURE_MARKER_RE = re.compile(
    r"^\s*\[(?:FAILED|FAILURE|RUN_FAILED)\](?:\s|$)", re.IGNORECASE
)
STOPPED_MARKER_RE = re.compile(
    r"^\s*\[(?:STOPPED|CANCELLED|CANCELED)\](?:\s|$)", re.IGNORECASE
)
REMOTE_END_RE = re.compile(
    r"^\s*\[REMOTE_RUNNER_END\].*\bstate=(?P<state>succeeded|failed|stopped)\b",
    re.IGNORECASE,
)
LEGACY_END_RE = re.compile(r"^\s*\[END\].*\brc=(?P<rc>\d+)\s*$", re.IGNORECASE)
REMOTE_STATUS_PREFIX = "remote_status_json="
LOG_TAIL_MARKER = "__REMOTE_RUNNER_LOG_TAIL__"
TERMINAL_STATUSES = {"succeeded", "failed", "stopped"}
REMOTE_STATUS_STATES = TERMINAL_STATUSES | {"running"}
MAX_MONITOR_WORKERS = 8
RUNTIME_ABSENT_ERROR = (
    "verified remote runtime is absent while execution authority remains active"
)

FAILURE_PATTERNS: list[tuple[str, str]] = [
    ("resource", "out of memory"),
    ("resource", "cuda out of memory"),
    ("resource", "no space left on device"),
    ("resource", "disk quota exceeded"),
    ("resource", "too many open files"),
    ("environment", "modulenotfounderror"),
    ("environment", "importerror"),
    ("environment", "python: not found"),
    ("environment", "permission denied"),
    ("code_logic", "traceback (most recent call last)"),
    ("code_logic", "assertionerror"),
    ("code_logic", "valueerror"),
    ("code_logic", "runtimeerror"),
    ("infra", "connection timed out"),
    ("infra", "connection refused"),
    ("infra", "no route to host"),
]


def classify_failure(log_tail: str, stderr: str = "") -> dict[str, str] | None:
    haystack = f"{log_tail}\n{stderr}".lower()
    for category, pattern in FAILURE_PATTERNS:
        if pattern in haystack:
            return {"category": category, "reason": pattern}
    if re.search(r"\bfailed\b", haystack) or re.search(r"\berror\s*:", haystack):
        return {"category": "unknown", "reason": "error marker in log"}
    return None


def parse_terminal_marker(log_tail: str) -> dict[str, str] | None:
    terminal: dict[str, str] | None = None
    for line in log_tail.splitlines():
        stripped = line.strip()
        remote_end = REMOTE_END_RE.match(line)
        legacy_end = LEGACY_END_RE.match(line)
        if remote_end:
            terminal = {
                "status": remote_end.group("state").lower(),
                "source": "remote_runner_log",
                "marker": stripped,
            }
        elif legacy_end:
            terminal = {
                "status": "succeeded" if int(legacy_end.group("rc")) == 0 else "failed",
                "source": "legacy_exit_code",
                "marker": stripped,
            }
        elif SUCCESS_MARKER_RE.match(line):
            terminal = {
                "status": "succeeded",
                "source": "log_marker",
                "marker": stripped,
            }
        elif FAILURE_MARKER_RE.match(line):
            terminal = {"status": "failed", "source": "log_marker", "marker": stripped}
        elif STOPPED_MARKER_RE.match(line):
            terminal = {"status": "stopped", "source": "log_marker", "marker": stripped}
        elif "EXPERIMENT DONE" in line or re.search(
            r"\bStatus:\s*SUCCESS\b", line, re.IGNORECASE
        ):
            terminal = {
                "status": "succeeded",
                "source": "legacy_marker",
                "marker": stripped,
            }
    return terminal


def parse_remote_status(
    probe_output: str,
    *,
    expected_run_id: str | None = None,
    require_run_id: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    encoded_status: str | None = None
    for line in probe_output.splitlines():
        if line.startswith(REMOTE_STATUS_PREFIX):
            encoded_status = line[len(REMOTE_STATUS_PREFIX) :]
    if encoded_status is None:
        return None, None
    try:
        data = json.loads(encoded_status)
    except json.JSONDecodeError as exc:
        return None, f"invalid remote status JSON: {exc.msg}"
    if not isinstance(data, dict):
        return None, "invalid remote status JSON: expected an object"
    schema_version = data.get("schema_version")
    if schema_version not in {1, 2}:
        return None, "invalid remote status JSON: unsupported schema_version"
    if schema_version == 2:
        for field in ("assigned_cores", "server_cores"):
            value = data.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                return (
                    None,
                    f"invalid remote status JSON: {field} must be a positive integer",
                )
        if int(data["assigned_cores"]) > int(data["server_cores"]):
            return (
                None,
                "invalid remote status JSON: assigned_cores exceeds server_cores",
            )
    workload_class = data.get("workload_class", "standard")
    if workload_class not in {"standard", "test"}:
        return None, "invalid remote status JSON: unsupported workload_class"
    data["workload_class"] = workload_class
    run_id = data.get("run_id")
    if require_run_id and run_id != expected_run_id:
        return None, "invalid remote status JSON: run_id mismatch"
    if run_id is not None and expected_run_id is not None and run_id != expected_run_id:
        return None, "invalid remote status JSON: run_id mismatch"
    state = data.get("state")
    exit_code = data.get("exit_code")
    if state not in REMOTE_STATUS_STATES:
        return None, f"invalid remote status state: {state!r}"
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        return None, "invalid remote status JSON: exit_code must be an integer or null"
    if state == "running" and exit_code is not None:
        return (
            None,
            "invalid remote status JSON: running state must have null exit_code",
        )
    if state == "succeeded" and exit_code != 0:
        return None, "invalid remote status JSON: succeeded state must have exit_code 0"
    if state == "failed" and (exit_code is None or exit_code == 0):
        return (
            None,
            "invalid remote status JSON: failed state must have a nonzero exit_code",
        )
    return data, None


def split_probe_output(probe_output: str) -> tuple[str, str]:
    marker = f"{LOG_TAIL_MARKER}\n"
    if marker not in probe_output:
        return probe_output, ""
    metadata, log_tail = probe_output.split(marker, 1)
    return metadata, log_tail


def _probe_value(metadata: str, key: str) -> str | None:
    prefix = f"{key}="
    for line in metadata.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


def _unsupported(
    run_id: str,
    registry_kind: str,
    path: Path,
    error: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "label": run_id,
        "registry_kind": registry_kind,
        "registry_path": str(path),
        "authoritative_status": None,
        "stored_status": None,
        "observation": "unsupported",
        "error": error,
        "progress": {"kind": "unknown_eta"},
    }


def _current_state_projection(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "authoritative_status": state["status"],
        "stored_status": state["status"],
        "revision": state["revision"],
        "created_at": state["created_at"],
        "updated_at": state["updated_at"],
        "started_at": state["started_at"],
        "finished_at": state["finished_at"],
        "exit_code": state["exit_code"],
        "error": state["error"],
    }


def _current_row(paths: ProjectPaths, run_id: str) -> dict[str, Any]:
    manifest, state = load_current_run(paths, run_id)
    remote_runtime = runtime_path(run_id)
    row = {
        "run_id": run_id,
        "label": manifest["label"],
        "registry_kind": "current",
        "registry_path": str(paths.runs_dir / run_id / "manifest.yaml"),
        "server": manifest["server"],
        "ssh": manifest["ssh"],
        "task_id": manifest["task_id"],
        "workload_class": manifest.get("workload_class", "standard"),
        "assigned_cores": manifest.get(
            "assigned_cores", manifest["configured_cores"]
        ),
        "server_cores": manifest["configured_cores"],
        **_current_state_projection(state),
        "tmux_session": run_tmux_session(run_id),
        "remote_log": f"{remote_runtime}/log",
        "remote_status": f"{remote_runtime}/status.json",
        "remote_pgid": f"{remote_runtime}/pgid",
        "observation": "not_probed",
        "progress": {"kind": "unknown_eta"},
    }
    privacy_mode = process_title_privacy_mode(manifest)
    if privacy_mode is not None:
        row["privacy_mode"] = privacy_mode
    return row


def _historical_row(
    run_id: str,
    registry_kind: str,
    path: Path,
    manifest: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = state or {}
    plan = manifest.get("launch_plan")
    plan = plan if isinstance(plan, dict) else {}
    remote_runtime = plan.get("runtime_path")
    if not isinstance(remote_runtime, str) or not remote_runtime:
        remote_runtime = f"~/.rr/{run_id}"
    remote_log = manifest.get("remote_log") or plan.get("remote_log")
    remote_status = manifest.get("remote_status") or plan.get("remote_status")
    if not isinstance(remote_log, str) or not remote_log:
        remote_log = f"{remote_runtime}/l" if registry_kind == "v2" else ""
    if not isinstance(remote_status, str) or not remote_status:
        remote_status = (
            f"{remote_runtime}/s"
            if registry_kind == "v2"
            else remote_status_path_for_log(remote_log)
        )
    stored_status = state.get("status", manifest.get("status"))
    return {
        "run_id": run_id,
        "label": manifest.get("label", run_id),
        "registry_kind": registry_kind,
        "registry_path": str(path),
        "server": manifest.get("server"),
        "ssh": manifest.get("ssh"),
        "task_id": manifest.get("task_id"),
        "workload_class": manifest.get("workload_class", "standard"),
        "authoritative_status": None,
        "stored_status": stored_status,
        "tmux_session": manifest.get("tmux_session")
        or plan.get("tmux_session")
        or run_id,
        "remote_log": remote_log,
        "remote_status": remote_status,
        "remote_pgid": f"{remote_runtime}/pgid",
        "observation": "not_probed",
        "progress": {"kind": "unknown_eta"},
    }


def load_registry_rows(
    paths: ProjectPaths,
    *,
    only_run_id: str | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    if not paths.runs_dir.exists():
        return []
    directory_ids = {path.name for path in paths.runs_dir.iterdir() if path.is_dir()}
    flat_ids = {path.stem for path in paths.runs_dir.glob("*.yaml")}
    run_ids = sorted(directory_ids | flat_ids)
    if only_run_id is not None:
        run_ids = [run_id for run_id in run_ids if run_id == only_run_id]

    rows: list[dict[str, Any]] = []
    for run_id in run_ids:
        directory = paths.runs_dir / run_id
        flat = paths.runs_dir / f"{run_id}.yaml"
        if directory.exists() and flat.exists():
            rows.append(
                _unsupported(
                    run_id, "conflict", directory, "flat and directory records conflict"
                )
            )
            continue
        if flat.exists():
            if active_only:
                continue
            try:
                rows.append(_historical_row(run_id, "legacy", flat, load_yaml(flat)))
            except (OSError, RuntimeError, ValueError) as exc:
                rows.append(_unsupported(run_id, "legacy", flat, str(exc)))
            continue

        manifest_path = directory / "manifest.yaml"
        if not manifest_path.is_file():
            if active_only:
                continue
            rows.append(
                _unsupported(run_id, "directory", directory, "missing manifest.yaml")
            )
            continue
        if active_only:
            state_path = directory / "state.yaml"
            try:
                state = load_yaml(state_path) if state_path.is_file() else {}
            except (OSError, RuntimeError, ValueError):
                continue
            if state.get("status") in TERMINAL_STATUSES:
                continue
        try:
            manifest = load_yaml(manifest_path)
            schema = manifest.get("schema_version")
            if schema in {
                LEGACY_CURRENT_MANIFEST_SCHEMA,
                PREVIOUS_MANIFEST_SCHEMA,
                CURRENT_MANIFEST_SCHEMA,
            }:
                rows.append(_current_row(paths, run_id))
            elif schema == 2:
                if active_only:
                    continue
                state_path = directory / "state.yaml"
                state = load_yaml(state_path) if state_path.is_file() else {}
                rows.append(
                    _historical_row(run_id, "v2", manifest_path, manifest, state)
                )
            else:
                rows.append(
                    _unsupported(
                        run_id,
                        "directory",
                        manifest_path,
                        f"unsupported schema: {schema!r}",
                    )
                )
        except (OSError, RuntimeError, ValueError) as exc:
            rows.append(_unsupported(run_id, "directory", manifest_path, str(exc)))
    return rows


def remote_probe(row: dict[str, Any], timeout: int) -> dict[str, Any]:
    for field in ("ssh", "tmux_session", "remote_status", "remote_log", "remote_pgid"):
        if not isinstance(row.get(field), str) or not row[field]:
            return {
                "observation": "unsupported",
                "error": f"run has no usable {field}",
                "progress": {"kind": "unknown_eta"},
            }
    tmux_arg = shlex.quote(exact_tmux_target(str(row["tmux_session"])))
    log_arg = shell_quote_remote_path(str(row["remote_log"]))
    status_arg = shell_quote_remote_path(str(row["remote_status"]))
    pgid_arg = shell_quote_remote_path(str(row["remote_pgid"]))
    remote_command = "\n".join(
        [
            "set +e",
            f"tmux has-session -t {tmux_arg} >/dev/null 2>&1; echo tmux_alive=$?",
            f"test -f {log_arg}; echo log_exists=$?",
            f"test -f {status_arg}; echo status_exists=$?",
            f"test -f {pgid_arg}; echo pgid_exists=$?",
            f"pgid=$(head -n 1 {pgid_arg} 2>/dev/null); "
            'if test -n "$pgid"; then kill -0 -- "-$pgid" >/dev/null 2>&1; '
            "echo pgid_alive=$?; else echo pgid_alive=1; fi",
            f"if test -f {status_arg}; then printf '{REMOTE_STATUS_PREFIX}'; head -n 1 {status_arg}; fi",
            f"test -f {log_arg} && stat -c 'log_mtime=%Y log_size=%s' {log_arg}",
            f"echo {LOG_TAIL_MARKER}",
            f"test -f {log_arg} && tail -200 {log_arg}",
            "true",
        ]
    )
    code, stdout, stderr = ssh_capture(str(row["ssh"]), remote_command, timeout)
    if code != 0:
        return {
            "observation": "unreachable",
            "ssh_reachable": False,
            "error": stderr.strip() or f"ssh exited {code}",
            "failure": {
                "category": "infra",
                "reason": stderr.strip() or f"ssh exited {code}",
            },
            "progress": {"kind": "unknown_eta"},
        }

    metadata, log_tail = split_probe_output(stdout)
    tmux_alive = _probe_value(metadata, "tmux_alive") == "0"
    pgid_alive = _probe_value(metadata, "pgid_alive") == "0"
    log_exists = _probe_value(metadata, "log_exists") == "0"
    status_exists = _probe_value(metadata, "status_exists") == "0"
    status_record, status_error = parse_remote_status(
        metadata,
        expected_run_id=str(row["run_id"]),
        require_run_id=row.get("registry_kind") == "current",
    )
    terminal_marker = parse_terminal_marker(log_tail)
    failure = classify_failure(log_tail, stderr)
    current = row.get("registry_kind") == "current"

    if (
        status_record is not None
        and status_record["state"] in TERMINAL_STATUSES
        and pgid_alive
    ):
        observation = "running"
        source = "live_process"
        status_error = (
            status_error or "remote terminal status conflicts with a live process group"
        )
    elif status_record is not None and status_record["state"] in TERMINAL_STATUSES:
        observation = str(status_record["state"])
        source = "remote_status"
    elif tmux_alive or pgid_alive:
        observation = "running"
        source = "live_process"
    elif status_record is not None and status_record["state"] == "running":
        observation = "unknown"
        source = "stale_remote_status"
        status_error = (
            status_error
            or "remote status is running but tmux and process group are absent"
        )
    elif not current and terminal_marker is not None:
        observation = terminal_marker["status"]
        source = str(terminal_marker["source"])
    elif (
        row.get("authoritative_status") == "registered"
        and not log_exists
        and not status_exists
    ):
        observation = "registered"
        source = "no_remote_runtime"
    else:
        observation = "unknown"
        source = "insufficient_evidence"

    result: dict[str, Any] = {
        "observation": observation,
        "observation_source": source,
        "ssh_reachable": True,
        "tmux_alive": tmux_alive,
        "pgid_alive": pgid_alive,
        "log_exists": log_exists,
        "remote_status_exists": status_exists,
        "progress": parse_progress(log_tail) or {"kind": "unknown_eta"},
    }
    stat_match = LOG_STAT_RE.search(metadata)
    if stat_match:
        result["log_mtime"] = int(stat_match.group("mtime"))
        result["log_size"] = int(stat_match.group("size"))
    if status_record is not None:
        result["remote_status_record"] = status_record
    if terminal_marker is not None:
        result["terminal_evidence"] = terminal_marker
    if status_error is not None:
        result["error"] = status_error
    if failure is not None:
        result["failure"] = failure
    return result


def reconcile_current(
    paths: ProjectPaths,
    row: dict[str, Any],
    probe: dict[str, Any],
) -> dict[str, Any]:
    desired: str | None = None
    remote = probe.get("remote_status_record")
    if (
        isinstance(remote, dict)
        and remote.get("state") in TERMINAL_STATUSES
        and not probe.get("pgid_alive")
    ):
        desired = str(remote["state"])
    elif probe.get("observation") == "running" and (
        probe.get("tmux_alive")
        or probe.get("pgid_alive")
        or (isinstance(remote, dict) and remote.get("state") == "running")
    ):
        desired = "running"

    _manifest, current = load_current_run(paths, str(row["run_id"]))
    if current["status"] in TERMINAL_STATUSES:
        updated = dict(row)
        updated.update(_current_state_projection(current))
        return updated

    runtime_absent = (
        current["status"] == "running"
        and probe.get("ssh_reachable") is True
        and probe.get("tmux_alive") is False
        and probe.get("pgid_alive") is False
        and probe.get("observation") not in TERMINAL_STATUSES
    )
    if runtime_absent:
        if current.get("error") != RUNTIME_ABSENT_ERROR:
            current = update_current_state(
                paths,
                str(row["run_id"]),
                int(current["revision"]),
                {"error": RUNTIME_ABSENT_ERROR},
                action="monitor_attention",
            )
        updated = dict(row)
        updated.update(_current_state_projection(current))
        return updated

    if desired is None:
        return row

    if current["status"] == desired:
        if current.get("error") == RUNTIME_ABSENT_ERROR:
            current = update_current_state(
                paths,
                str(row["run_id"]),
                int(current["revision"]),
                {"error": None},
                action="monitor_recovered",
            )
        updated = dict(row)
        updated.update(_current_state_projection(current))
        return updated
    if not can_transition(str(current["status"]), desired):
        return row

    changes: dict[str, Any] = {"status": desired, "error": None}
    if isinstance(remote, dict):
        if remote.get("started_at") is not None:
            changes["started_at"] = remote["started_at"]
        if desired in TERMINAL_STATUSES:
            changes["finished_at"] = remote.get("finished_at") or utc_now()
            changes["exit_code"] = remote.get("exit_code")
    elif desired == "running":
        changes["started_at"] = current.get("started_at") or utc_now()
    updated_state = update_current_state(
        paths,
        str(row["run_id"]),
        int(current["revision"]),
        changes,
        action="monitor_reconciled",
    )
    updated = dict(row)
    updated.update(_current_state_projection(updated_state))
    return updated


def monitor_row(
    paths: ProjectPaths,
    row: dict[str, Any],
    timeout: int,
    *,
    no_write: bool,
) -> dict[str, Any]:
    if row.get("observation") == "unsupported":
        return row
    if (
        row.get("registry_kind") == "current"
        and row.get("authoritative_status") in TERMINAL_STATUSES
    ):
        cached = dict(row)
        cached["observation"] = row["authoritative_status"]
        cached["observation_source"] = "local_terminal"
        return cached
    probe = remote_probe(row, timeout)
    combined = dict(row)
    combined.update(probe)
    if row.get("registry_kind") == "current" and not no_write:
        try:
            combined = reconcile_current(paths, combined, probe)
        except RuntimeError:
            _manifest, current = load_current_run(paths, str(row["run_id"]))
            combined.update(_current_state_projection(current))
    return combined


def monitor_rows(
    paths: ProjectPaths,
    rows: list[dict[str, Any]],
    timeout: int,
    *,
    no_write: bool,
    isolate_errors: bool = False,
) -> list[dict[str, Any]]:
    def monitor(row: dict[str, Any]) -> dict[str, Any]:
        try:
            return monitor_row(paths, row, timeout, no_write=no_write)
        except Exception as exc:
            if not isolate_errors:
                raise
            failed = dict(row)
            failed["observation"] = "unknown"
            failed["error"] = f"monitor failed for this run: {exc}"
            return failed

    if len(rows) <= 1:
        return [monitor(row) for row in rows]
    workers = min(MAX_MONITOR_WORKERS, len(rows))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(monitor, rows))


def summarize(rows: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        observation = str(row.get("observation", "unknown"))
        counts[observation] = counts.get(observation, 0) + 1
    lines = ["Remote Runner Summary"]
    lines.append(
        " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
        or "no runs"
    )
    for row in rows:
        raw_progress = row.get("progress")
        progress: dict[str, Any] = (
            raw_progress if isinstance(raw_progress, dict) else {}
        )
        progress_text = str(progress.get("kind", "unknown_eta"))
        scope = progress.get("scope")
        stage = progress.get("stage")
        if scope is not None and stage is not None:
            progress_text = f"{scope}:{stage}"
        if progress.get("percent") is not None:
            progress_text += f" {float(progress['percent']):.1f}%"
        if progress.get("eta_seconds") is not None:
            progress_text += f" ETA={float(progress['eta_seconds']):.0f}s"
        if progress.get("kind") == "invalid_progress" and progress.get("error"):
            progress_text += f" error={progress['error']!r}"
        state = row.get("authoritative_status")
        if state is None and row.get("stored_status") is not None:
            state = f"legacy:{row['stored_status']}"
        error = f" error={row.get('error')!r}" if row.get("error") else ""
        privacy = (
            f" privacy={row['privacy_mode']}"
            if row.get("privacy_mode") is not None
            else ""
        )
        lines.append(
            f"{row['run_id']} kind={row.get('registry_kind')} label={row.get('label')!r} "
            f"server={row.get('server')} state={state} observation={row.get('observation')} "
            f"progress={progress_text}{privacy}{error}"
        )
    return "\n".join(lines)


def query_controller(args: argparse.Namespace) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    action_args: tuple[str, ...] = ()
    if args.run_id is not None:
        action_args += ("--run-id", args.run_id)
    elif args.task_id is not None:
        action_args += ("--task-id", args.task_id)
    return call_controller(
        config,
        "status",
        timeout=args.timeout,
        action_args=action_args,
    )
