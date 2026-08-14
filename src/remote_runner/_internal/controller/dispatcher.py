from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from .. import launch, monitoring, registration
from ..execution_registry import (
    TERMINAL_STATUSES,
    load_current_run,
    project_paths,
    registry_kind,
    update_current_state,
    utc_now,
    write_yaml,
)
from ..machine_identity import normalize_machine_fingerprint, normalize_server_identity
from ..output_paths import resolve_output_path
from ..remote_shell import remote_python_stdin_command, ssh_connection_options
from ..scheduling import CapacityCandidate, rank_candidates
from ..worktree import prepare_remote_worktree
from .registry import (
    ControllerPaths,
    LeaseOwnership,
    acquire_dispatch_lease,
    controller_paths,
    dispatch_lease_authority_gone,
    eligible_prepared_servers,
    has_unexpired_dispatch_lease,
    list_drained_servers,
    load_job,
    list_jobs,
    list_owned_dispatch_leases,
    list_queued,
    list_server_capacities,
    placement_update_active,
    recover_dispatching_state,
    release_dispatch_lease,
    resolve_server_identity,
    renew_dispatch_lease,
    transition_queued_state,
)
from .output_sync_worker import ensure_output_sync_worker


SERVER_STATE_PROBE_PROGRAM = r"""import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def exact_tmux_target(session_name):
    return "=" + session_name


def machine_fingerprint():
    material = None
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            material = "machine-id:" + value
            break
    if material is None and sys.platform == "darwin":
        try:
            completed = subprocess.run(
                ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', completed.stdout)
            if match is not None:
                material = "ioplatformuuid:" + match.group(1)
    if material is None:
        return None
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def memory_snapshot():
    total = None
    available = None
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            parts = raw.strip().split()
            if not parts:
                continue
            multiplier = 1024 if len(parts) > 1 and parts[1] == "kB" else 1
            values[key] = int(parts[0]) * multiplier
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        if available is None and total is not None:
            available = sum(
                values.get(key, 0)
                for key in ("MemFree", "Buffers", "Cached")
            )
    except (OSError, ValueError):
        pass

    if total is None:
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            total_pages = int(os.sysconf("SC_PHYS_PAGES"))
            available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
            if page_size > 0 and total_pages > 0:
                total = page_size * total_pages
                if available_pages >= 0:
                    available = page_size * available_pages
        except (OSError, ValueError, TypeError):
            pass

    if total is None:
        try:
            sysctl = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                text=True,
                timeout=2,
            )
            if sysctl.returncode == 0:
                total = int(sysctl.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

        if total is not None and total > 0:
            try:
                vm_stat = subprocess.run(
                    ["vm_stat"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    text=True,
                    timeout=2,
                )
                if vm_stat.returncode == 0:
                    header = vm_stat.stdout.splitlines()[0]
                    page_size = int(header.split("page size of", 1)[1].split()[0])
                    pages = {}
                    for line in vm_stat.stdout.splitlines()[1:]:
                        key, raw = line.split(":", 1)
                        pages[key] = int(raw.strip().rstrip("."))
                    available = page_size * sum(
                        pages.get(key, 0)
                        for key in ("Pages free", "Pages speculative", "Pages purgeable")
                    )
            except (IndexError, OSError, ValueError, subprocess.SubprocessError):
                pass

    if total is None or total <= 0:
        return {
            "memory_total_bytes": None,
            "memory_available_bytes": None,
            "memory_used_bytes": None,
            "memory_used_percent": None,
        }

    if available is not None:
        available = max(0, min(total, available))
        used = total - available
        used_percent = (used / total) * 100.0
    else:
        used = None
        used_percent = None
    return {
        "memory_total_bytes": total,
        "memory_available_bytes": available,
        "memory_used_bytes": used,
        "memory_used_percent": used_percent,
    }


active = []
root = Path.home() / ".rr"
if root.is_dir():
    for status_path in root.glob("rr-*/status.json"):
        try:
            value = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or value.get("state") != "running":
            continue
        runtime = status_path.parent
        alive = False
        try:
            pgid = int((runtime / "pgid").read_text(encoding="utf-8").strip())
            if pgid > 1:
                os.killpg(pgid, 0)
                alive = True
        except PermissionError:
            alive = True
        except (FileNotFoundError, OSError, ValueError):
            pass
        if not alive:
            alive = subprocess.run(
                ["tmux", "has-session", "-t", exact_tmux_target(status_path.parent.name)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0
        if alive:
            workload_class = value.get("workload_class", "standard")
            if workload_class not in {"standard", "test"}:
                workload_class = "standard"
            item = {
                "run_id": str(value.get("run_id", status_path.parent.name)),
                "workload_class": workload_class,
            }
            label = value.get("label")
            if isinstance(label, str) and label:
                item["label"] = label
            for field in ("assigned_cores", "server_cores"):
                field_value = value.get(field)
                if (
                    isinstance(field_value, int)
                    and not isinstance(field_value, bool)
                    and field_value > 0
                ):
                    item[field] = field_value
            active.append(item)
load1, load5, load15 = os.getloadavg()
print(json.dumps({
    "active_runs": sorted(active, key=lambda item: item["run_id"]),
    "active_run_ids": sorted(item["run_id"] for item in active),
    "load1": load1,
    "load5": load5,
    "load15": load15,
    "remote_cores": os.cpu_count(),
    "machine_fingerprint": machine_fingerprint(),
    **memory_snapshot(),
}))
"""
MAX_CAPACITY_PROBE_WORKERS = 8


@dataclass(frozen=True)
class DispatchOutcome:
    action: str
    run_id: str | None
    server: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ProbedServer:
    server: dict[str, Any]
    capacity: CapacityCandidate
    active_standard_count: int
    active_test_count: int
    active_run_ids: tuple[str, ...] = ()
    active_assigned_cores: int = 0
    allocation_unknown: bool = False
    lease_ownership: LeaseOwnership | None = None


@dataclass(frozen=True)
class PlannedDispatch:
    job: dict[str, Any]
    state: dict[str, Any]
    selected: ProbedServer
    alternatives: tuple[ProbedServer, ...] = ()


@dataclass(frozen=True)
class CapacitySnapshot:
    reachable: dict[tuple[object, ...], ProbedServer]
    failures: tuple[str, ...]
    drained_servers: frozenset[str]


class LeaseOwnershipLost(RuntimeError):
    pass


class DispatchLeaseHeartbeat:
    def __init__(
        self,
        paths: ControllerPaths,
        ownership: LeaseOwnership,
        *,
        ttl_seconds: int,
    ) -> None:
        self._paths = paths
        self._ownership = ownership
        self._ttl_seconds = ttl_seconds
        self._interval = max(0.1, min(float(ttl_seconds) / 3.0, 10.0))
        self._stop = Event()
        self._lock = Lock()
        self._error: BaseException | None = None
        self._thread = Thread(
            target=self._run,
            name=f"rr-lease-{ownership.run_id}",
            daemon=True,
        )

    @property
    def ownership(self) -> LeaseOwnership:
        with self._lock:
            return self._ownership

    def start(self) -> None:
        self._thread.start()

    def _renew(self) -> None:
        with self._lock:
            if self._error is not None:
                raise LeaseOwnershipLost(str(self._error)) from self._error
            renewed = renew_dispatch_lease(
                self._paths,
                self._ownership,
                ttl_seconds=self._ttl_seconds,
            )
            if renewed is None:
                self._error = LeaseOwnershipLost(
                    f"dispatch lease ownership lost for {self._ownership.run_id} on "
                    f"machine_id {self._ownership.machine_id}"
                )
                raise self._error
            self._ownership = renewed

    def assert_owned(self) -> None:
        self._renew()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._renew()
            except BaseException as exc:
                with self._lock:
                    if self._error is None:
                        self._error = exc
                return

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._interval * 2.0))


def _optional_non_negative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer or null")
    return value


def probe_server_state(ssh: str, python: str, timeout: int) -> dict[str, Any]:
    argv = [
        "ssh",
        *ssh_connection_options(timeout),
        ssh,
        remote_python_stdin_command(python),
    ]
    try:
        completed = subprocess.run(
            argv,
            input=SERVER_STATE_PROBE_PROGRAM.encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"active-run probe timed out after {exc.timeout}s") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.decode(errors="replace").strip()
            or f"active-run probe exited {completed.returncode}"
        )
    try:
        value = json.loads(completed.stdout.decode())
        active_runs = value["active_runs"]
        load1 = float(value["load1"])
        load5 = float(value["load5"])
        load15 = float(value["load15"])
        remote_cores = value.get("remote_cores")
        machine_fingerprint = normalize_machine_fingerprint(
            value.get("machine_fingerprint")
        )
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError("active-run probe returned invalid JSON") from exc
    try:
        memory_total_bytes = _optional_non_negative_int(
            value.get("memory_total_bytes"), "memory_total_bytes"
        )
        memory_available_bytes = _optional_non_negative_int(
            value.get("memory_available_bytes"), "memory_available_bytes"
        )
        memory_used_bytes = _optional_non_negative_int(
            value.get("memory_used_bytes"), "memory_used_bytes"
        )
        memory_used_percent = value.get("memory_used_percent")
        if memory_used_percent is not None:
            if (
                isinstance(memory_used_percent, bool)
                or not isinstance(memory_used_percent, (int, float))
                or not math.isfinite(float(memory_used_percent))
                or not 0 <= float(memory_used_percent) <= 100
            ):
                raise ValueError("memory_used_percent must be between 0 and 100")
            memory_used_percent = float(memory_used_percent)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"active-run probe returned invalid memory data: {exc}") from exc
    for name, item in (
        ("memory_total_bytes", memory_total_bytes),
        ("memory_available_bytes", memory_available_bytes),
        ("memory_used_bytes", memory_used_bytes),
    ):
        if item is not None and memory_total_bytes is not None and item > memory_total_bytes:
            raise RuntimeError(
                f"active-run probe returned invalid memory data: {name} exceeds total"
            )
    if not isinstance(active_runs, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("run_id"), str)
        and item.get("workload_class") in {"standard", "test"}
        for item in active_runs
    ):
        raise RuntimeError("active-run probe returned invalid active runs")
    normalized = tuple(
        {
            "run_id": str(item["run_id"]),
            "workload_class": str(item["workload_class"]),
            **(
                {"label": str(item["label"])}
                if isinstance(item.get("label"), str) and item["label"]
                else {}
            ),
            **(
                {"assigned_cores": int(item["assigned_cores"])}
                if isinstance(item.get("assigned_cores"), int)
                and not isinstance(item["assigned_cores"], bool)
                and int(item["assigned_cores"]) > 0
                else {}
            ),
            **(
                {"server_cores": int(item["server_cores"])}
                if isinstance(item.get("server_cores"), int)
                and not isinstance(item["server_cores"], bool)
                and item["server_cores"] > 0
                else {}
            ),
        }
        for item in active_runs
    )
    return {
        "reachable": True,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "remote_cores": remote_cores,
        "machine_fingerprint": machine_fingerprint,
        "memory_total_bytes": memory_total_bytes,
        "memory_available_bytes": memory_available_bytes,
        "memory_used_bytes": memory_used_bytes,
        "memory_used_percent": memory_used_percent,
        "active_runs": normalized,
        "active_run_ids": tuple(item["run_id"] for item in normalized),
    }


def _active_runs(probe: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    raw = probe.get("active_runs")
    if raw is None:
        raw = tuple(
            {"run_id": str(run_id), "workload_class": "standard"}
            for run_id in probe.get("active_run_ids", ())
        )
    if not isinstance(raw, (list, tuple)) or not all(
        isinstance(item, dict)
        and isinstance(item.get("run_id"), str)
        and item.get("workload_class") in {"standard", "test"}
        for item in raw
    ):
        raise RuntimeError("active-run probe returned invalid active runs")
    return tuple(
        {
            "run_id": str(item["run_id"]),
            "workload_class": str(item["workload_class"]),
            **(
                {"assigned_cores": int(item["assigned_cores"])}
                if isinstance(item.get("assigned_cores"), int)
                and not isinstance(item["assigned_cores"], bool)
                and int(item["assigned_cores"]) > 0
                else {}
            ),
        }
        for item in raw
    )


def _probe_prepared_server(
    server: dict[str, Any],
    timeout: int,
    *,
    paths: ControllerPaths | None = None,
) -> ProbedServer:
    server = (
        resolve_server_identity(paths, server)
        if paths is not None
        else normalize_server_identity(server)
    )
    probe = probe_server_state(
        str(server["ssh"]),
        str(server["python"]),
        timeout,
    )
    expected_fingerprint = server.get("machine_fingerprint")
    observed_fingerprint = probe.get("machine_fingerprint")
    if expected_fingerprint is not None and observed_fingerprint is None:
        raise RuntimeError(
            f"machine identity probe unavailable for {server['name']!r}"
        )
    if (
        expected_fingerprint is not None
        and observed_fingerprint != expected_fingerprint
    ):
        raise RuntimeError(
            f"machine fingerprint mismatch for machine_id {server['machine_id']!r}"
        )
    if paths is not None and observed_fingerprint is not None:
        server = resolve_server_identity(
            paths, {**server, "machine_fingerprint": observed_fingerprint}
        )
    active = _active_runs(probe)
    standard_count = sum(item["workload_class"] == "standard" for item in active)
    test_count = sum(item["workload_class"] == "test" for item in active)
    assigned = [item.get("assigned_cores") for item in active]
    allocation_unknown = any(value is None for value in assigned)
    assigned_cores = sum(int(value) for value in assigned if value is not None)
    configured_cores = int(server["configured_cores"])
    if assigned_cores > configured_cores:
        raise RuntimeError(
            f"active core allocations exceed configured inventory on {server['name']!r}"
        )
    return ProbedServer(
        server=server,
        capacity=CapacityCandidate(
            name=str(server["name"]),
            configured_cores=configured_cores,
            load5=float(probe["load5"]),
            priority=int(server["priority"]),
            active_run_count=len(active),
            allocated_cores=assigned_cores,
            allocation_unknown=allocation_unknown,
        ),
        active_standard_count=standard_count,
        active_test_count=test_count,
        active_run_ids=tuple(str(item["run_id"]) for item in active),
        active_assigned_cores=assigned_cores,
        allocation_unknown=allocation_unknown,
    )


def _probe_prepared_servers(
    servers: list[dict[str, Any]],
    timeout: int,
    *,
    paths: ControllerPaths | None = None,
) -> tuple[list[ProbedServer], list[str]]:
    def probe(server: dict[str, Any]) -> tuple[ProbedServer | None, str | None]:
        try:
            return _probe_prepared_server(server, timeout, paths=paths), None
        except RuntimeError as exc:
            return None, f"{server['name']}: {exc}"

    if len(servers) <= 1:
        results = [probe(server) for server in servers]
    else:
        workers = min(MAX_CAPACITY_PROBE_WORKERS, len(servers))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(probe, servers))
    reachable = [result for result, _failure in results if result is not None]
    failures = [failure for _result, failure in results if failure is not None]
    return reachable, failures


def _server_snapshot_key(server: dict[str, Any]) -> tuple[object, ...]:
    identity = normalize_server_identity(server)
    return (
        str(identity["machine_id"]),
        str(identity.get("machine_fingerprint")),
        int(identity["configured_cores"]),
    )


def _with_current_capacity(
    paths: ControllerPaths,
    server: dict[str, Any],
    capacities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    identity = resolve_server_identity(paths, server)
    current = capacities.get(str(identity["machine_id"]))
    if current is None:
        return {
            **identity,
            "standard_slots": int(server.get("standard_slots", 1)),
        }
    return {
        **identity,
        "standard_slots": int(current["standard_slots"]),
        "test_slots": int(current["test_slots"]),
    }


def _probe_capacity_snapshot(
    paths: ControllerPaths,
    queued: list[tuple[dict[str, Any], dict[str, Any]]],
    timeout: int,
) -> CapacitySnapshot:
    drained_servers = frozenset(list_drained_servers(paths))
    capacities = list_server_capacities(paths)
    unique_servers: dict[tuple[object, ...], dict[str, Any]] = {}
    for job, _state in queued:
        for prepared_server in eligible_prepared_servers(job):
            server = _with_current_capacity(paths, prepared_server, capacities)
            prepared_server.update(
                {
                    key: server[key]
                    for key in (
                        "machine_id",
                        "machine_id_source",
                        "machine_fingerprint",
                    )
                }
            )
            if str(server["machine_id"]) in drained_servers:
                continue
            unique_servers.setdefault(_server_snapshot_key(server), server)
    reachable, failures = _probe_prepared_servers(
        list(unique_servers.values()), timeout, paths=paths
    )
    return CapacitySnapshot(
        reachable={_server_snapshot_key(item.server): item for item in reachable},
        failures=tuple(failures),
        drained_servers=drained_servers,
    )


def _requested_allocation(job: dict[str, Any], candidate: ProbedServer) -> int:
    requested = job.get("requested_cores")
    if requested is None:
        return candidate.capacity.configured_cores
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        raise ValueError("queued requested_cores must be positive or null")
    return requested


def _has_workload_capacity(job: dict[str, Any], candidate: ProbedServer) -> bool:
    workload_class = str(job["workload_class"])
    if workload_class == "standard":
        lane_available = candidate.active_standard_count < int(
            candidate.server.get("standard_slots", 1)
        )
    else:
        lane_available = candidate.active_test_count < int(
            candidate.server.get("test_slots", 0)
        )
    if not lane_available or candidate.allocation_unknown:
        return False
    return (
        candidate.active_assigned_cores + _requested_allocation(job, candidate)
        <= candidate.capacity.configured_cores
    )


def _capacity_message(job: dict[str, Any], candidates: list[ProbedServer]) -> str:
    workload_class = str(job["workload_class"])
    if workload_class == "standard" and candidates and all(
        candidate.active_standard_count
        >= int(candidate.server.get("standard_slots", 1))
        for candidate in candidates
    ):
        used = max(candidate.active_standard_count for candidate in candidates)
        total = max(
            int(candidate.server.get("standard_slots", 1))
            for candidate in candidates
        )
        return f"standard slots full ({used}/{total})"
    if workload_class == "test" and candidates and all(
        candidate.active_test_count >= int(candidate.server.get("test_slots", 0))
        for candidate in candidates
    ):
        used = max(candidate.active_test_count for candidate in candidates)
        total = max(int(candidate.server.get("test_slots", 0)) for candidate in candidates)
        return f"test slots full ({used}/{total})"
    if any(candidate.allocation_unknown for candidate in candidates):
        return "core allocation unavailable for an active legacy run"
    if candidates and all(
        candidate.active_assigned_cores + _requested_allocation(job, candidate)
        > candidate.capacity.configured_cores
        for candidate in candidates
    ):
        used = max(candidate.active_assigned_cores for candidate in candidates)
        requested = min(_requested_allocation(job, candidate) for candidate in candidates)
        total = max(candidate.capacity.configured_cores for candidate in candidates)
        return f"core allocation full ({used}+{requested}/{total})"
    if workload_class == "standard":
        used = max(
            (candidate.active_standard_count for candidate in candidates), default=0
        )
        total = max(
            (int(candidate.server.get("standard_slots", 1)) for candidate in candidates),
            default=0,
        )
        return f"standard slots full ({used}/{total})"
    used = max((candidate.active_test_count for candidate in candidates), default=0)
    total = max(
        (int(candidate.server.get("test_slots", 0)) for candidate in candidates),
        default=0,
    )
    return f"test slots full ({used}/{total})"


def _rank_for_workload(
    job: dict[str, Any],
    candidates: list[ProbedServer],
) -> list[ProbedServer]:
    eligible = [
        candidate
        for candidate in candidates
        if _has_workload_capacity(job, candidate)
    ]
    if not eligible:
        return eligible
    by_name = {candidate.capacity.name: candidate for candidate in eligible}
    return [
        by_name[item.name]
        for item in rank_candidates([item.capacity for item in eligible])
    ]


def _select_server_for_job(
    paths: ControllerPaths,
    job: dict[str, Any],
    *,
    timeout: int,
    allowed_server_names: set[str] | None = None,
) -> tuple[ProbedServer | None, str]:
    drained_servers = set(list_drained_servers(paths))
    capacities = list_server_capacities(paths)
    eligible_servers = []
    for prepared_server in eligible_prepared_servers(job):
        server = _with_current_capacity(paths, prepared_server, capacities)
        if str(normalize_server_identity(server)["machine_id"]) in drained_servers:
            continue
        if (
            allowed_server_names is not None
            and str(server["name"]) not in allowed_server_names
        ):
            continue
        eligible_servers.append(server)
    reachable, failures = _probe_prepared_servers(
        eligible_servers,
        timeout,
        paths=paths,
    )
    if not reachable:
        eligible_names = {
            str(resolve_server_identity(paths, server)["machine_id"])
            for server in eligible_prepared_servers(job)
        }
        if eligible_names and eligible_names <= drained_servers:
            return None, "all prepared servers are drained"
        return None, "; ".join(failures) or "no reachable prepared server"

    ranked = _rank_for_workload(job, reachable)
    if not ranked:
        return None, _capacity_message(job, reachable)

    ttl = int(job.get("lease_seconds", 120))
    acquired = False
    latest = reachable
    for candidate in ranked:
        ownership = acquire_dispatch_lease(
            paths,
            server=candidate.capacity.name,
            machine_id=str(candidate.server["machine_id"]),
            run_id=str(job["run_id"]),
            ttl_seconds=ttl,
        )
        if ownership is None:
            continue
        acquired = True
        try:
            current_server = _with_current_capacity(
                paths, candidate.server, list_server_capacities(paths)
            )
            current = _probe_prepared_server(current_server, timeout, paths=paths)
        except RuntimeError:
            release_dispatch_lease(
                paths,
                server=candidate.capacity.name,
                machine_id=ownership.machine_id,
                run_id=str(job["run_id"]),
                owner_token=ownership.token,
            )
            continue
        if _has_workload_capacity(job, current):
            return replace(current, lease_ownership=ownership), ""
        latest = [current]
        release_dispatch_lease(
            paths,
            server=candidate.capacity.name,
            machine_id=ownership.machine_id,
            run_id=str(job["run_id"]),
            owner_token=ownership.token,
        )
    if acquired:
        return None, _capacity_message(job, latest)
    return None, "dispatch leases busy"


def _select_backfill_from_lane(
    paths: ControllerPaths,
    lane: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    timeout: int,
) -> tuple[
    ProbedServer | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    head_job, _head_state = lane[0]
    drained_servers = set(list_drained_servers(paths))
    # Blocked jobs reserve every server they could use; backfill only elsewhere.
    protected_machines = {
        str(resolve_server_identity(paths, server)["machine_id"])
        for server in eligible_prepared_servers(head_job)
        if str(resolve_server_identity(paths, server)["machine_id"])
        not in drained_servers
    }

    for candidate_job, candidate_state in lane[1:]:
        eligible_servers = {
            str(server["name"]): str(
                resolve_server_identity(paths, server)["machine_id"]
            )
            for server in eligible_prepared_servers(candidate_job)
            if str(resolve_server_identity(paths, server)["machine_id"])
            not in drained_servers
        }
        safe_servers = {
            name
            for name, machine_id in eligible_servers.items()
            if machine_id not in protected_machines
        }
        if safe_servers:
            selected, _message = _select_server_for_job(
                paths,
                candidate_job,
                timeout=timeout,
                allowed_server_names=safe_servers,
            )
        else:
            selected = None
        if selected is not None:
            return selected, candidate_job, candidate_state
        protected_machines.update(eligible_servers.values())

    return None, None, None


def _select_from_snapshot(
    job: dict[str, Any],
    snapshot: CapacitySnapshot,
    *,
    reserved_servers: set[str],
    allowed_server_names: set[str] | None = None,
) -> tuple[list[ProbedServer], str]:
    prepared = eligible_prepared_servers(job)
    eligible = [
        server
        for server in prepared
        if str(normalize_server_identity(server)["machine_id"])
        not in snapshot.drained_servers
        and str(normalize_server_identity(server)["machine_id"])
        not in reserved_servers
        and (
            allowed_server_names is None or str(server["name"]) in allowed_server_names
        )
    ]
    reachable = []
    for server in eligible:
        probed = snapshot.reachable.get(_server_snapshot_key(server))
        if probed is None:
            continue
        # Capacity is shareable, but launch paths remain frozen per queued job.
        reachable.append(
            ProbedServer(
                server={
                    **server,
                    "standard_slots": int(
                        probed.server.get("standard_slots", 1)
                    ),
                    "test_slots": int(probed.server.get("test_slots", 0)),
                },
                capacity=probed.capacity,
                active_standard_count=probed.active_standard_count,
                active_test_count=probed.active_test_count,
                active_run_ids=probed.active_run_ids,
                active_assigned_cores=probed.active_assigned_cores,
                allocation_unknown=probed.allocation_unknown,
            )
        )
    if not reachable:
        prepared_machines = {
            str(normalize_server_identity(server)["machine_id"])
            for server in prepared
        }
        if prepared_machines and prepared_machines <= snapshot.drained_servers:
            return [], "all prepared servers are drained"
        prepared_names = {str(server["name"]) for server in prepared}
        failures = [
            failure
            for failure in snapshot.failures
            if failure.partition(":")[0] in prepared_names
        ]
        return [], "; ".join(failures) or "no available server in dispatch batch"
    ranked = _rank_for_workload(job, reachable)
    if not ranked:
        return [], _capacity_message(job, reachable)
    return ranked, ""


def _plan_backfill_from_lane(
    lane: list[tuple[dict[str, Any], dict[str, Any]]],
    snapshot: CapacitySnapshot,
    *,
    reserved_servers: set[str],
) -> PlannedDispatch | None:
    head_job, _head_state = lane[0]
    protected_machines = {
        str(normalize_server_identity(server)["machine_id"])
        for server in eligible_prepared_servers(head_job)
        if str(normalize_server_identity(server)["machine_id"])
        not in snapshot.drained_servers
    }
    for candidate_job, candidate_state in lane[1:]:
        eligible_servers = {
            str(server["name"]): str(normalize_server_identity(server)["machine_id"])
            for server in eligible_prepared_servers(candidate_job)
            if str(normalize_server_identity(server)["machine_id"])
            not in snapshot.drained_servers
        }
        safe_servers = {
            name
            for name, machine_id in eligible_servers.items()
            if machine_id not in protected_machines
        }
        if safe_servers:
            candidates, _message = _select_from_snapshot(
                candidate_job,
                snapshot,
                reserved_servers=reserved_servers,
                allowed_server_names=safe_servers,
            )
            if candidates:
                return PlannedDispatch(
                    candidate_job,
                    candidate_state,
                    candidates[0],
                    tuple(candidates[1:]),
                )
        protected_machines.update(eligible_servers.values())
    return None


def _plan_dispatch_batch(
    queued: list[tuple[dict[str, Any], dict[str, Any]]],
    snapshot: CapacitySnapshot,
) -> tuple[list[PlannedDispatch], DispatchOutcome]:
    lanes = [
        [row for row in queued if row[0]["workload_class"] == workload_class]
        for workload_class in ("standard", "test")
    ]
    lanes = [lane for lane in lanes if lane]
    planned: list[PlannedDispatch] = []
    reserved_servers: set[str] = set()
    terminal = DispatchOutcome(action="idle", run_id=None)

    while lanes:
        blocked: list[tuple[str, str, str]] = []
        choice: PlannedDispatch | None = None
        choice_lane: list[tuple[dict[str, Any], dict[str, Any]]] | None = None
        for lane in lanes:
            candidate_job, candidate_state = lane[0]
            candidates, message = _select_from_snapshot(
                candidate_job,
                snapshot,
                reserved_servers=reserved_servers,
            )
            if candidates:
                choice = PlannedDispatch(
                    candidate_job,
                    candidate_state,
                    candidates[0],
                    tuple(candidates[1:]),
                )
                choice_lane = lane
                break
            blocked.append(
                (
                    str(candidate_job["workload_class"]),
                    str(candidate_job["run_id"]),
                    message,
                )
            )
        if choice is None:
            for lane in lanes:
                choice = _plan_backfill_from_lane(
                    lane,
                    snapshot,
                    reserved_servers=reserved_servers,
                )
                if choice is not None:
                    choice_lane = lane
                    break
        if choice is None or choice_lane is None:
            _workload_class, run_id, message = blocked[0]
            if len(blocked) > 1:
                message = "; ".join(
                    f"{kind}: {detail}" for kind, _run_id, detail in blocked
                )
            terminal = DispatchOutcome(action="queued", run_id=run_id, message=message)
            break

        planned.append(choice)
        reserved_servers.add(str(choice.selected.server["machine_id"]))
        choice_lane.remove((choice.job, choice.state))
        lanes = [lane for lane in lanes if lane]

    return planned, terminal


def _ensure_controller_anchor(paths: ControllerPaths) -> None:
    if paths.config_path.is_file():
        return
    paths.project_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.registry_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_yaml(paths.config_path, {"controller_registry": True})


def _register_execution(
    paths: ControllerPaths,
    job: dict[str, Any],
    server: dict[str, Any],
    *,
    workdir: str,
    assigned_cores: int,
    output_root: str | None,
    output_relpath: str | None,
    output_path: str | None,
) -> None:
    _ensure_controller_anchor(paths)
    server = normalize_server_identity(server)
    args = argparse.Namespace(
        project_config=paths.config_path,
        label=job["label"],
        task_id=job["task_id"],
        workload_class=job.get("workload_class", "standard"),
        server=server["name"],
        machine_id=server["machine_id"],
        machine_fingerprint=server.get("machine_fingerprint"),
        ssh=server["ssh"],
        ssh_profile=server["ssh_profile"],
        configured_cores=server["configured_cores"],
        minimum_cores=job["minimum_cores"],
        requested_cores=job.get("requested_cores"),
        assigned_cores=assigned_cores,
        command=job["submitted_command"],
        remote_workdir=workdir,
        project_python=server["python"],
        source_revision=job["revision"],
        prepared_servers=[item["name"] for item in eligible_prepared_servers(job)],
        submitted_command=job["submitted_command"],
        expected_revision=job["revision"],
        require_clean_worktree=True,
        output_root=output_root,
        output_relpath=output_relpath,
        output_path=output_path,
        output_metadata=json.dumps(job.get("output_metadata", {}), sort_keys=True),
        run_id=job["run_id"],
        privacy=job.get("privacy"),
    )
    registration.register(args)


def _resolve_selected_output(
    job: dict[str, Any],
    server: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    output_relpath = job.get("output_relpath")
    if output_relpath is None:
        return None, None, job.get("output_path")
    output_root = server.get("output_root")
    output_path = resolve_output_path(output_root, output_relpath)
    return str(output_root), str(output_relpath), output_path


def _fail_registered_execution(paths: ControllerPaths, run_id: str, error: str) -> None:
    if not paths.config_path.is_file():
        return
    execution_paths = project_paths(paths.config_path)
    if registry_kind(execution_paths, run_id) != "current":
        return
    _manifest, state = load_current_run(execution_paths, run_id)
    if state["status"] != "registered":
        return
    update_current_state(
        execution_paths,
        run_id,
        int(state["revision"]),
        {
            "status": "failed",
            "finished_at": utc_now(),
            "error": error,
        },
        action="controller_dispatch_failed",
    )


def _run_visible_on_server(
    paths: ControllerPaths,
    server: dict[str, Any],
    run_id: str,
    *,
    timeout: int,
) -> bool:
    probed = _probe_prepared_server(server, timeout, paths=paths)
    return run_id in probed.active_run_ids


def _transition_after_launch(
    paths: ControllerPaths,
    run_id: str,
    *,
    expected_revision: int,
    status: str,
    error: str | None,
) -> None:
    try:
        transition_queued_state(
            paths,
            run_id,
            expected_revision=expected_revision,
            status=status,
            error=error,
        )
    except RuntimeError as exc:
        if str(exc) != "queued state revision conflict":
            raise
        _job, current = load_job(paths, run_id)
        if current["status"] != status:
            raise LeaseOwnershipLost(
                f"queued launch commit ownership lost for {run_id}; "
                f"current status={current['status']!r}"
            ) from exc


def _launch_dispatching_job(
    paths: ControllerPaths,
    planned: PlannedDispatch,
    *,
    timeout: int,
) -> DispatchOutcome:
    job = planned.job
    state = planned.state
    selected_server = planned.selected.server
    selected_capacity = planned.selected.capacity
    ownership = planned.selected.lease_ownership
    if ownership is None:
        raise RuntimeError("planned dispatch lacks fenced lease ownership")
    run_id = str(job["run_id"])
    execution_registered = False
    release_lease = True
    ttl = int(job.get("lease_seconds", 120))
    heartbeat = DispatchLeaseHeartbeat(paths, ownership, ttl_seconds=ttl)
    heartbeat.start()
    try:
        heartbeat.assert_owned()
        output_root, output_relpath, output_path = _resolve_selected_output(
            job,
            selected_server,
        )
        worktree = prepare_remote_worktree(
            ssh=str(selected_server["ssh"]),
            python=str(selected_server["python"]),
            bare_repo=str(selected_server["bare_repo"]),
            worktree_root=str(selected_server["worktree_root"]),
            revision=str(job["revision"]),
            timeout=timeout,
        )
        heartbeat.assert_owned()
        _register_execution(
            paths,
            job,
            selected_server,
            workdir=worktree.workdir,
            assigned_cores=_requested_allocation(job, planned.selected),
            output_root=output_root,
            output_relpath=output_relpath,
            output_path=output_path,
        )
        execution_registered = True
        heartbeat.assert_owned()
        execution_paths = project_paths(paths.config_path)
        launch.launch(execution_paths, run_id, timeout)
        heartbeat.assert_owned()
        _transition_after_launch(
            paths,
            run_id,
            expected_revision=int(state["revision"]) + 1,
            status="dispatched",
            error=None,
        )
        return DispatchOutcome(
            action="started", run_id=run_id, server=selected_capacity.name
        )
    except Exception as exc:
        unknown_launch = execution_registered and (
            isinstance(exc, LeaseOwnershipLost)
            or isinstance(exc.__cause__, (launch.BootstrapOutcomeUnknown, OSError))
        )
        if unknown_launch:
            release_lease = False
            try:
                heartbeat.assert_owned()
            except (LeaseOwnershipLost, RuntimeError):
                pass
            try:
                visible = _run_visible_on_server(
                    paths,
                    selected_server,
                    run_id,
                    timeout=timeout,
                )
            except (OSError, RuntimeError, ValueError):
                visible = False
            if visible:
                release_lease = True
            try:
                _transition_after_launch(
                    paths,
                    run_id,
                    expected_revision=int(state["revision"]) + 1,
                    status="dispatched",
                    error=str(exc),
                )
            except LeaseOwnershipLost:
                pass
            return DispatchOutcome(
                action="unknown",
                run_id=run_id,
                server=selected_capacity.name,
                message=str(exc),
            )
        if execution_registered:
            _fail_registered_execution(paths, run_id, str(exc))
        _transition_after_launch(
            paths,
            run_id,
            expected_revision=int(state["revision"]) + 1,
            status="failed",
            error=str(exc),
        )
        return DispatchOutcome(
            action="failed",
            run_id=run_id,
            server=selected_capacity.name,
            message=str(exc),
        )
    finally:
        heartbeat.stop()
        if release_lease:
            current_ownership = heartbeat.ownership
            release_dispatch_lease(
                paths,
                server=selected_capacity.name,
                machine_id=current_ownership.machine_id,
                run_id=run_id,
                owner_token=current_ownership.token,
            )


def _reconcile_dispatching_jobs(paths: ControllerPaths) -> DispatchOutcome | None:
    while True:
        dispatching = list_jobs(paths, statuses={"dispatching"})
        if not dispatching:
            return None
        job, state = dispatching[0]
        run_id = str(job["run_id"])
        if paths.config_path.is_file():
            execution_paths = project_paths(paths.config_path)
            if registry_kind(execution_paths, run_id) == "current":
                transition_queued_state(
                    paths,
                    run_id,
                    expected_revision=int(state["revision"]),
                    status="dispatched",
                )
                continue
        if has_unexpired_dispatch_lease(paths, run_id=run_id):
            return DispatchOutcome(action="busy", run_id=run_id)
        matching = [
            lease
            for lease in list_owned_dispatch_leases(paths)
            if lease["run_id"] == run_id
        ]
        if len(matching) > 1:
            raise RuntimeError(f"run {run_id} owns multiple dispatch leases")
        if matching:
            lease = matching[0]
            fenced = acquire_dispatch_lease(
                paths,
                server=str(lease["server"]),
                machine_id=str(lease["machine_id"]),
                run_id=run_id,
                ttl_seconds=int(job.get("lease_seconds", 120)),
            )
            if fenced is None:
                return DispatchOutcome(action="busy", run_id=run_id)
            release_dispatch_lease(
                paths,
                server=fenced.server,
                machine_id=fenced.machine_id,
                run_id=run_id,
                owner_token=fenced.token,
            )
        recover_dispatching_state(
            paths,
            run_id,
            expected_revision=int(state["revision"]),
        )


def _reconcile_owned_dispatch_leases(paths: ControllerPaths, *, timeout: int) -> None:
    leases = list_owned_dispatch_leases(paths)
    if not leases:
        return
    execution_paths = (
        project_paths(paths.config_path) if paths.config_path.is_file() else None
    )
    for lease in leases:
        run_id = str(lease["run_id"])
        kind = (
            None
            if execution_paths is None
            else registry_kind(execution_paths, run_id)
        )
        if (
            kind is None
            and float(lease["expires_at"]) <= time.time()
            and dispatch_lease_authority_gone(
                paths,
                project_id=str(lease["project_id"]),
                run_id=run_id,
            )
        ):
            release_dispatch_lease(
                paths,
                server=str(lease["server"]),
                machine_id=str(lease["machine_id"]),
                run_id=run_id,
                owner_token=(
                    str(lease["owner_token"])
                    if lease.get("owner_token") is not None
                    else None
                ),
            )
            continue
        if kind != "current":
            continue
        assert execution_paths is not None
        _manifest, state = load_current_run(execution_paths, run_id)
        should_release = state["status"] != "registered"
        if not should_release:
            try:
                job, _queue_state = load_job(paths, run_id)
                selected = next(
                    server
                    for server in eligible_prepared_servers(job)
                    if resolve_server_identity(paths, server)["machine_id"]
                    == lease["machine_id"]
                )
                should_release = _run_visible_on_server(
                    paths,
                    _with_current_capacity(
                        paths, selected, list_server_capacities(paths)
                    ),
                    run_id,
                    timeout=timeout,
                )
            except (FileNotFoundError, OSError, RuntimeError, StopIteration, ValueError):
                should_release = False
        if should_release:
            release_dispatch_lease(
                paths,
                server=str(lease["server"]),
                machine_id=str(lease["machine_id"]),
                run_id=run_id,
                owner_token=(
                    str(lease["owner_token"])
                    if lease.get("owner_token") is not None
                    else None
                ),
            )


def dispatch_once(paths: ControllerPaths, *, timeout: int = 8) -> DispatchOutcome:
    _reconcile_owned_dispatch_leases(paths, timeout=timeout)
    reconciliation = _reconcile_dispatching_jobs(paths)
    if reconciliation is not None:
        return reconciliation

    queued = [row for row in list_queued(paths) if not placement_update_active(row[1])]
    if not queued:
        return DispatchOutcome(action="idle", run_id=None)
    lanes: list[list[tuple[dict[str, Any], dict[str, Any]]]] = []
    for workload_class in ("standard", "test"):
        lane = [row for row in queued if row[0]["workload_class"] == workload_class]
        if lane:
            lanes.append(lane)

    blocked: list[tuple[str, str, str]] = []
    selected: ProbedServer | None = None
    job: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
    for lane in lanes:
        candidate_job, candidate_state = lane[0]
        selected, message = _select_server_for_job(
            paths,
            candidate_job,
            timeout=timeout,
        )
        if selected is not None:
            job = candidate_job
            state = candidate_state
            break
        blocked.append(
            (
                str(candidate_job["workload_class"]),
                str(candidate_job["run_id"]),
                message,
            )
        )
    if selected is None:
        for lane in lanes:
            selected, candidate_job, candidate_state = _select_backfill_from_lane(
                paths,
                lane,
                timeout=timeout,
            )
            if (
                selected is not None
                and candidate_job is not None
                and candidate_state is not None
            ):
                job = candidate_job
                state = candidate_state
                break
    if selected is None or job is None or state is None:
        workload_class, run_id, message = blocked[0]
        if len(blocked) > 1:
            message = "; ".join(f"{kind}: {detail}" for kind, _run, detail in blocked)
        return DispatchOutcome(action="queued", run_id=run_id, message=message)

    run_id = str(job["run_id"])
    selected_capacity = selected.capacity

    try:
        transition_queued_state(
            paths,
            run_id,
            expected_revision=int(state["revision"]),
            status="dispatching",
        )
    except RuntimeError as exc:
        ownership = selected.lease_ownership
        release_dispatch_lease(
            paths,
            server=selected_capacity.name,
            machine_id=str(selected.server["machine_id"]),
            run_id=run_id,
            owner_token=ownership.token if ownership is not None else None,
        )
        if str(exc) == "queued state revision conflict":
            return dispatch_once(paths, timeout=timeout)
        raise
    return _launch_dispatching_job(
        paths,
        PlannedDispatch(job, state, selected),
        timeout=timeout,
    )


def _reserve_planned_dispatch(
    paths: ControllerPaths,
    planned: PlannedDispatch,
    *,
    timeout: int,
    other_planned_servers: frozenset[str],
) -> PlannedDispatch | DispatchOutcome:
    run_id = str(planned.job["run_id"])
    ttl = int(planned.job.get("lease_seconds", 120))
    last_message = "dispatch leases busy"
    for candidate in (planned.selected, *planned.alternatives):
        server_name = candidate.capacity.name
        machine_id = str(candidate.server["machine_id"])
        if machine_id in other_planned_servers:
            continue
        ownership = acquire_dispatch_lease(
            paths,
            server=server_name,
            machine_id=machine_id,
            run_id=run_id,
            ttl_seconds=ttl,
        )
        if ownership is None:
            continue
        try:
            current_server = _with_current_capacity(
                paths, candidate.server, list_server_capacities(paths)
            )
            current = _probe_prepared_server(current_server, timeout, paths=paths)
        except RuntimeError as exc:
            last_message = str(exc)
            release_dispatch_lease(
                paths,
                server=server_name,
                machine_id=machine_id,
                run_id=run_id,
                owner_token=ownership.token,
            )
            continue
        if _has_workload_capacity(planned.job, current):
            return PlannedDispatch(
                planned.job,
                planned.state,
                replace(current, lease_ownership=ownership),
            )
        last_message = _capacity_message(planned.job, [current])
        release_dispatch_lease(
            paths,
            server=server_name,
            machine_id=machine_id,
            run_id=run_id,
            owner_token=ownership.token,
        )
    return DispatchOutcome(
        action="queued",
        run_id=run_id,
        server=planned.selected.capacity.name,
        message=last_message,
    )


def dispatch_batch(
    paths: ControllerPaths,
    *,
    timeout: int = 8,
) -> list[DispatchOutcome]:
    _reconcile_owned_dispatch_leases(paths, timeout=timeout)
    reconciliation = _reconcile_dispatching_jobs(paths)
    if reconciliation is not None:
        return [reconciliation]
    queued = [row for row in list_queued(paths) if not placement_update_active(row[1])]
    if not queued:
        return [DispatchOutcome(action="idle", run_id=None)]

    snapshot = _probe_capacity_snapshot(paths, queued, timeout)
    planned, terminal = _plan_dispatch_batch(queued, snapshot)
    if not planned:
        return [terminal]

    workers = min(MAX_CAPACITY_PROBE_WORKERS, len(planned))
    planned_servers = frozenset(
        str(item.selected.server["machine_id"]) for item in planned
    )

    def reserve(item: PlannedDispatch) -> PlannedDispatch | DispatchOutcome:
        return _reserve_planned_dispatch(
            paths,
            item,
            timeout=timeout,
            other_planned_servers=planned_servers
            - {str(item.selected.server["machine_id"])},
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        reserved = list(executor.map(reserve, planned))

    outcomes = [item for item in reserved if isinstance(item, DispatchOutcome)]
    dispatching: list[PlannedDispatch] = []
    try:
        for item in reserved:
            if isinstance(item, DispatchOutcome):
                continue
            run_id = str(item.job["run_id"])
            try:
                transition_queued_state(
                    paths,
                    run_id,
                    expected_revision=int(item.state["revision"]),
                    status="dispatching",
                )
            except RuntimeError as exc:
                release_dispatch_lease(
                    paths,
                    server=item.selected.capacity.name,
                    machine_id=str(item.selected.server["machine_id"]),
                    run_id=run_id,
                    owner_token=(
                        item.selected.lease_ownership.token
                        if item.selected.lease_ownership is not None
                        else None
                    ),
                )
                if str(exc) == "queued state revision conflict":
                    outcomes.append(
                        DispatchOutcome(
                            action="queued",
                            run_id=run_id,
                            server=item.selected.capacity.name,
                            message=str(exc),
                        )
                    )
                    continue
                raise
            dispatching.append(item)
    except Exception:
        for item in reserved:
            if not isinstance(item, DispatchOutcome) and item not in dispatching:
                release_dispatch_lease(
                    paths,
                    server=item.selected.capacity.name,
                    machine_id=str(item.selected.server["machine_id"]),
                    run_id=str(item.job["run_id"]),
                    owner_token=(
                        item.selected.lease_ownership.token
                        if item.selected.lease_ownership is not None
                        else None
                    ),
                )
        raise

    if not dispatching:
        return outcomes or [terminal]
    _ensure_controller_anchor(paths)
    with ThreadPoolExecutor(max_workers=len(dispatching)) as executor:
        outcomes.extend(
            executor.map(
                lambda item: _launch_dispatching_job(paths, item, timeout=timeout),
                dispatching,
            )
        )
    return outcomes


def dispatch_loop(
    paths: ControllerPaths,
    *,
    timeout: int = 8,
    interval_seconds: int = 60,
) -> int:
    if interval_seconds <= 0:
        raise ValueError("dispatcher interval must be positive")
    while True:
        try:
            active = False
            if paths.config_path.is_file():
                execution_paths = project_paths(paths.config_path)
                rows = [
                    row
                    for row in monitoring.load_registry_rows(execution_paths)
                    if row.get("registry_kind") == "current"
                ]
                for monitored in monitoring.monitor_rows(
                    execution_paths,
                    rows,
                    timeout,
                    no_write=False,
                    isolate_errors=True,
                ):
                    if monitored.get("authoritative_status") not in TERMINAL_STATUSES:
                        active = True
            while True:
                outcomes = dispatch_batch(paths, timeout=timeout)
                started = any(outcome.action == "started" for outcome in outcomes)
                if not started:
                    break
                active = True
            try:
                ensure_output_sync_worker(
                    paths,
                    timeout=timeout,
                    interval=interval_seconds,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                print(
                    f"output-sync worker start failed: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            if any(outcome.action == "idle" for outcome in outcomes) and not active:
                return 0
        except (OSError, RuntimeError, ValueError) as exc:
            print(
                f"[remote-runner dispatcher] cycle failed: {exc}",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dispatch queued remote-runner jobs.")
    parser.add_argument("--controller-root", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    paths = controller_paths(args.controller_root, args.project_id)
    if args.once:
        print(
            json.dumps(
                dispatch_once(paths, timeout=args.timeout).__dict__, sort_keys=True
            )
        )
        return 0
    return dispatch_loop(
        paths,
        timeout=args.timeout,
        interval_seconds=args.interval,
    )


if __name__ == "__main__":
    raise SystemExit(main())
