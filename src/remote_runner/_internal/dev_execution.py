from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .config import DevProjectConfig, load_dev_project_config
from .dev_source import DevSourcePlan, build_source_plan
from .execution_registry import load_yaml, resolve_project_config
from .machine_identity import (
    normalize_machine_fingerprint,
    normalize_machine_id,
)
from .pool import DEFAULT_SERVER_REGISTRY, probe_endpoint, resolve_ssh_targets
from .remote_shell import remote_python_stdin_command, ssh_connection_options
from .source import resolve_source_repo


DEV_SESSION_RE = re.compile(r"^dev-[0-9a-f]{16}$")
DEV_ROOT_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
SSH_TARGET_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9._-]*$"
)
INFRASTRUCTURE_EXIT_CODE = 125
REMOTE_RESULT_PREFIX = "RR_DEV_RESULT "
BUILD_ENVIRONMENT = (
    "MAKEFLAGS",
    "CMAKE_BUILD_PARALLEL_LEVEL",
    "CARGO_BUILD_JOBS",
)
_CANCELLATION_STATUSES = {255}


class _LocalSignal(BaseException):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


@dataclass(frozen=True)
class DevServer:
    name: str
    machine_id: str
    ssh: str
    ssh_profile: str
    cores: int
    dev_root: str
    machine_fingerprint: str

    def project_root(self, project_id: str) -> str:
        return str(PurePosixPath(self.dev_root) / project_id)

    def session_root(self, project_id: str, session_id: str) -> str:
        return str(
            PurePosixPath(self.project_root(project_id)) / "tmp" / session_id
        )


@dataclass(frozen=True)
class DevSession:
    project_id: str
    session_id: str
    token: str
    remote_root: str
    cache_root: str

    @property
    def source_root(self) -> str:
        return str(PurePosixPath(self.remote_root) / "source")

    @property
    def runner_path(self) -> str:
        return str(PurePosixPath(self.remote_root) / "runner.py")


@dataclass(frozen=True)
class DevInvocation:
    profile: str | None
    command: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]


def resolve_dev_invocation(
    config: DevProjectConfig,
    *,
    command: object,
    profile: object,
) -> DevInvocation:
    if command is not None and profile is not None:
        raise ValueError("--command and --profile cannot be combined")
    if profile is not None:
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError("--profile must be a non-empty profile name")
        selected = config.profile_for(profile)
        include, exclude = config.source_patterns(selected)
        return DevInvocation(
            profile=selected.name,
            command=selected.command,
            include=include,
            exclude=exclude,
        )
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise ValueError("--command must be non-empty shell text without NUL bytes")
    include, exclude = config.source_patterns(None)
    return DevInvocation(
        profile=None,
        command=command,
        include=include,
        exclude=exclude,
    )


def normalize_dev_root(value: object, field: str = "dev_root") -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"global server {field} must be a non-empty string")
    if DEV_ROOT_RE.fullmatch(value) is None:
        raise ValueError(
            f"global server {field} must be an absolute POSIX path with safe characters"
        )
    components = value.split("/")
    if (
        value == "/"
        or value.endswith("/")
        or components[0] != ""
        or any(component in {"", ".", ".."} for component in components[1:])
    ):
        raise ValueError(f"global server {field} must be a normalized non-root path")
    return value


def _normalize_ssh_target(value: object, *, server_name: str) -> str:
    if not isinstance(value, str) or SSH_TARGET_RE.fullmatch(value) is None:
        raise ValueError(f"SSH target for server {server_name!r} is invalid")
    return value


def _server_mapping(registry_path: Path, name: str) -> dict[str, Any]:
    registry = load_yaml(registry_path.expanduser().resolve(strict=True))
    servers = registry.get("servers")
    if not isinstance(servers, dict):
        raise ValueError("global server registry must contain a 'servers' mapping")
    raw = servers.get(name)
    if not isinstance(raw, dict):
        raise ValueError(f"server {name!r} is not in the global registry")
    if raw.get("enabled", True) is not True:
        raise ValueError(f"server {name!r} is disabled")
    return raw


def resolve_dev_server(
    registry_path: Path,
    name: str,
    *,
    ssh_profile: str,
    timeout: int,
) -> DevServer:
    if isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("development connection timeout must be positive")
    raw = _server_mapping(registry_path, name)
    cores = raw.get("cores")
    if isinstance(cores, bool) or not isinstance(cores, int) or cores <= 0:
        raise ValueError(f"configured cores for {name!r} must be a positive integer")
    machine_id, _source = normalize_machine_id(raw.get("machine_id"), server_name=name)
    expected_fingerprint = normalize_machine_fingerprint(raw.get("machine_fingerprint"))
    dev_root = normalize_dev_root(raw.get("dev_root"))

    attempts: list[str] = []
    targets = resolve_ssh_targets(raw, name, ssh_profile)
    for raw_target, raw_profile in targets:
        ssh_target = _normalize_ssh_target(raw_target, server_name=name)
        if not isinstance(raw_profile, str) or not raw_profile.strip():
            raise ValueError(f"SSH profile for server {name!r} is invalid")
        probe = probe_endpoint(ssh_target, timeout, python="python3")
        if probe.get("reachable") is not True:
            attempts.append(f"{raw_profile}: {probe.get('error', 'unreachable')}")
            continue
        observed = normalize_machine_fingerprint(
            probe.get("machine_fingerprint"), required=True
        )
        if observed is None:  # The required normalizer is typed as optional.
            raise RuntimeError(f"machine fingerprint probe for {name!r} was empty")
        if expected_fingerprint is not None and observed != expected_fingerprint:
            raise RuntimeError(
                f"machine fingerprint mismatch for machine_id {machine_id!r}"
            )
        return DevServer(
            name=name,
            machine_id=machine_id,
            ssh=ssh_target,
            ssh_profile=raw_profile,
            cores=cores,
            dev_root=dev_root,
            machine_fingerprint=observed,
        )
    detail = "; ".join(attempts) if attempts else "no endpoint was configured"
    raise RuntimeError(f"server {name!r} is unreachable ({detail})")


DEV_RUNNER_SOURCE = r'''from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path


RESULT_PREFIX = "RR_DEV_RESULT "
BUILD_ENVIRONMENT = (
    "MAKEFLAGS",
    "CMAKE_BUILD_PARALLEL_LEVEL",
    "CARGO_BUILD_JOBS",
)


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
                values.get(key, 0) for key in ("MemFree", "Buffers", "Cached")
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
            completed = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                text=True,
                timeout=2,
            )
            if completed.returncode == 0:
                total = int(completed.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    if total is None or total <= 0:
        total = None
        available = None
    elif available is not None and not 0 <= available <= total:
        available = None
    return total, available


def resource_snapshot(config):
    total, available = memory_snapshot()
    return {
        "schema": "remote-runner-dev-resources/v1",
        "server": config["server_name"],
        "machine_id": config["machine_id"],
        "profile": config["profile"],
        "configured_cores": config["cores"],
        "assigned_cores": config["cores"],
        "observed_logical_cpus": os.cpu_count(),
        "memory_total_bytes": total,
        "memory_available_bytes": available,
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
        },
    }


def process_start(pid):
    try:
        raw = Path("/proc") / str(pid) / "stat"
        text = raw.read_text(encoding="utf-8")
        fields = text[text.rfind(")") + 2 :].split()
        if len(fields) >= 20:
            return "proc:" + fields[19]
    except OSError:
        pass
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return None
    value = " ".join(completed.stdout.split())
    return "ps:" + value if value else None


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def write_private(path, value):
    data = (str(value) + "\n").encode()
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError("write returned zero bytes")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def clear_process_records(session):
    for name in ("pgid", "process-start"):
        try:
            (session / name).unlink()
        except FileNotFoundError:
            pass


def cleanup_session(session, config):
    payload = {
        "action": "cleanup",
        "dev_root": config["dev_root"],
        "project_id": config["project_id"],
        "session_id": config["session_id"],
        "token": config["token"],
    }
    try:
        completed = subprocess.run(
            [sys.executable, str(session / "cleanup.py")],
            input=json.dumps(payload, separators=(",", ":")),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        if not os.path.lexists(session):
            return True, ""
        return False, str(exc)
    result = None
    for line in reversed(completed.stdout.splitlines()):
        if not line.startswith(RESULT_PREFIX):
            continue
        try:
            result = json.loads(line[len(RESULT_PREFIX) :])
        except json.JSONDecodeError:
            result = None
        break
    if completed.returncode == 0 and isinstance(result, dict) and result.get("ok") is True:
        return True, ""
    if not os.path.lexists(session):
        return True, ""
    detail = completed.stderr.strip()
    if isinstance(result, dict) and result.get("message"):
        detail = str(result["message"])
    elif not detail:
        detail = completed.stdout.strip()
    return False, detail or "guarded cleanup did not return a valid result"


def stop_child(child, pgid):
    if child.poll() is not None and not group_alive(pgid):
        return True
    deadline = time.monotonic() + 5
    term_sent = False
    while group_alive(pgid) and time.monotonic() < deadline:
        if not term_sent:
            try:
                os.killpg(pgid, signal.SIGTERM)
                term_sent = True
            except ProcessLookupError:
                break
            except PermissionError:
                pass
        try:
            child.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            pass
    if group_alive(pgid):
        deadline = time.monotonic() + 2
        kill_sent = False
        while group_alive(pgid) and time.monotonic() < deadline:
            if not kill_sent:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                    kill_sent = True
                except ProcessLookupError:
                    break
                except PermissionError:
                    pass
            try:
                child.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
    if child.poll() is None:
        try:
            child.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            return False
    return not group_alive(pgid)


session = Path(__file__).resolve().parent
config = json.loads((session / "session.json").read_text(encoding="utf-8"))
expected_keys = {
    "schema_version",
    "dev_root",
    "project_id",
    "session_id",
    "token",
    "cores",
    "cache_root",
    "build_environment",
    "project_python",
    "server_name",
    "machine_id",
    "profile",
}
if not isinstance(config, dict) or set(config) != expected_keys or config.get("schema_version") != 2:
    print("[remote-runner dev] runner configuration is invalid", file=sys.stderr)
    raise SystemExit(125)

environment = os.environ.copy()
cores = str(config["cores"])
environment["RR_SERVER_CORES"] = cores
environment["RR_ASSIGNED_CORES"] = cores
environment["RR_DEV_CACHE_DIR"] = config["cache_root"]
environment["RR_RESOURCE_JSON"] = json.dumps(
    resource_snapshot(config), sort_keys=True, separators=(",", ":")
)
environment.pop("RR_DEV_PROFILE", None)
if config["profile"] is not None:
    environment["RR_DEV_PROFILE"] = config["profile"]
project_python = config["project_python"]
if project_python is not None:
    if (
        not isinstance(project_python, str)
        or not project_python.startswith("/")
        or "\\x00" in project_python
        or "\\n" in project_python
        or "\\r" in project_python
    ):
        print("[remote-runner dev] runner project Python is invalid", file=sys.stderr)
        raise SystemExit(125)
    environment["RR_PROJECT_PYTHON"] = project_python
tool_bins = [
    os.path.join(os.path.expanduser("~"), ".local", "bin"),
    os.path.join(os.path.expanduser("~"), ".cargo", "bin"),
]
environment["PATH"] = os.pathsep.join(
    [*tool_bins, environment.get("PATH", os.defpath)]
)
defaults = {
    "MAKEFLAGS": "-j" + cores,
    "CMAKE_BUILD_PARALLEL_LEVEL": cores,
    "CARGO_BUILD_JOBS": cores,
}
for name, default in defaults.items():
    environment[name] = config["build_environment"].get(name, default)

child = None
pgid = None
forwarded = None
status = 125
identity_complete = False


def forward(signum, _frame):
    global forwarded
    forwarded = signum
    if pgid is not None:
        try:
            os.killpg(pgid, signum)
        except ProcessLookupError:
            pass


for candidate in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(candidate, forward)

try:
    if forwarded is None:
        child = subprocess.Popen(
            ["bash", str(session / "command.sh")],
            cwd=session / "source",
            env=environment,
            start_new_session=True,
        )
        pgid = child.pid
        write_private(session / "pgid", pgid)
        start_identity = process_start(child.pid)
        if start_identity is None:
            if forwarded is None:
                raise RuntimeError("cannot establish workload process identity")
            status = 128 + forwarded
        else:
            write_private(session / "process-start", start_identity)
            identity_complete = True
            if forwarded is not None:
                forward(forwarded, None)
            returncode = child.wait()
            if returncode < 0:
                status = 128 + (-returncode)
            elif forwarded is not None:
                status = 128 + forwarded
            else:
                status = returncode
    else:
        status = 128 + forwarded
except KeyboardInterrupt:
    forwarded = signal.SIGINT
    status = 128 + signal.SIGINT
except BaseException as exc:
    if forwarded is not None:
        status = 128 + forwarded
    else:
        print("[remote-runner dev] workload wrapper failed: " + str(exc), file=sys.stderr)
        status = 125
finally:
    group_stopped = True
    if child is not None and pgid is not None:
        if child.poll() is None or group_alive(pgid):
            group_stopped = stop_child(child, pgid)
        if group_stopped and not identity_complete:
            clear_process_records(session)
    if group_stopped:
        cleaned, detail = cleanup_session(session, config)
        if not cleaned:
            print("[remote-runner dev] cleanup failed: " + detail, file=sys.stderr)
            status = 125
    else:
        print(
            "[remote-runner dev] workload group could not be stopped; session retained",
            file=sys.stderr,
        )
        status = 125

raise SystemExit(status)
'''


REMOTE_HELPER_SOURCE = r'''import fcntl
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path


SESSION_RE = re.compile(r"^dev-[0-9a-f]{16}$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ROOT_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
CANCELLATION_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_CANCELLATION_INTENTS = 4096

if "payload" not in globals():
    payload = json.load(sys.stdin)


def emit(ok, action, message=None, **values):
    print(
        "RR_DEV_RESULT "
        + json.dumps(
            {"ok": ok, "action": action, "message": message, **values},
            sort_keys=True,
        ),
        flush=True,
    )


def fail(action, message, code=1):
    emit(False, action, message)
    raise SystemExit(code)


def validate_dev_root(value):
    if not isinstance(value, str) or ROOT_RE.fullmatch(value) is None:
        raise ValueError("development root must be an absolute path with safe characters")
    components = value.split("/")
    if (
        value == "/"
        or value.endswith("/")
        or components[0] != ""
        or any(component in {"", ".", ".."} for component in components[1:])
    ):
        raise ValueError("development root must be a normalized non-root path")
    return value


def validate_component(value, pattern, label):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError("invalid " + label)
    return value


def process_start(pid):
    try:
        raw = Path("/proc") / str(pid) / "stat"
        text = raw.read_text(encoding="utf-8")
        fields = text[text.rfind(")") + 2 :].split()
        if len(fields) >= 20:
            return "proc:" + fields[19]
    except OSError:
        pass
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return None
    value = " ".join(completed.stdout.split())
    return "ps:" + value if value else None


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def open_dir_at(parent_fd, name):
    return os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )


def require_private_directory(fd, label):
    current = os.fstat(fd)
    if not stat.S_ISDIR(current.st_mode) or current.st_uid != os.geteuid():
        raise PermissionError(label + " is not an owned directory")
    if stat.S_IMODE(current.st_mode) != PRIVATE_DIR_MODE:
        raise PermissionError(label + " must have mode 0700")


def open_absolute_dir(path, create=False):
    validate_dev_root(path)
    fd = os.open("/", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        parts = path.split("/")[1:]
        for index, part in enumerate(parts):
            created = False
            try:
                child = open_dir_at(fd, part)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, PRIVATE_DIR_MODE, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
                child = open_dir_at(fd, part)
            os.close(fd)
            fd = child
            if created or index == len(parts) - 1:
                require_private_directory(fd, "development root")
        return fd
    except BaseException:
        os.close(fd)
        raise


def ensure_child_dir(parent_fd, name, create=True):
    if not isinstance(name, str) or not name or "/" in name or name in {".", ".."}:
        raise ValueError("unsafe directory component")
    created = False
    try:
        fd = open_dir_at(parent_fd, name)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, PRIVATE_DIR_MODE, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        fd = open_dir_at(parent_fd, name)
    try:
        require_private_directory(fd, "development directory")
    except BaseException:
        os.close(fd)
        raise
    return fd, created


def write_bytes_at(parent_fd, name, content, mode):
    fd = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(fd, mode)
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError("write returned zero bytes")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_at(parent_fd, name, value):
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    write_bytes_at(parent_fd, name, data, PRIVATE_FILE_MODE)


def read_private_at(parent_fd, name, maximum=65536):
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        current = os.fstat(fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.geteuid()
            or stat.S_IMODE(current.st_mode) != PRIVATE_FILE_MODE
            or current.st_size > maximum
        ):
            raise ValueError("private control file is invalid")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0 and os.read(fd, 1):
            raise ValueError("private control file is too large")
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_json_at(parent_fd, name):
    return json.loads(read_private_at(parent_fd, name))


def read_text_at(parent_fd, name):
    return read_private_at(parent_fd, name, 4096).decode().strip()


def remove_tree_at(parent_fd, name, expected_dev):
    if not isinstance(name, str) or not name or "/" in name or name in {".", ".."}:
        raise ValueError("unsafe removal component")
    child_fd = open_dir_at(parent_fd, name)
    try:
        current = os.fstat(child_fd)
        if current.st_uid != os.geteuid() or current.st_dev != expected_dev:
            raise ValueError("refusing a foreign or mounted session directory")
        for entry in os.listdir(child_fd):
            entry_stat = os.stat(entry, dir_fd=child_fd, follow_symlinks=False)
            if stat.S_ISDIR(entry_stat.st_mode):
                remove_tree_at(child_fd, entry, expected_dev)
            elif stat.S_ISLNK(entry_stat.st_mode):
                os.unlink(entry, dir_fd=child_fd)
            else:
                if (
                    entry_stat.st_uid != os.geteuid()
                    or entry_stat.st_dev != expected_dev
                ):
                    raise ValueError("refusing a foreign or mounted session entry")
                os.unlink(entry, dir_fd=child_fd)
        current_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current_path.st_dev, current_path.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("session directory changed during guarded cleanup")
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(child_fd)


def validate_identity(marker, project_id, session_id, expected_token=None):
    expected_keys = {"schema_version", "project_id", "session_id", "token", "created_at"}
    if not isinstance(marker, dict) or set(marker) != expected_keys:
        raise ValueError("session ownership marker mismatch")
    if marker.get("schema_version") != 1:
        raise ValueError("session ownership marker mismatch")
    if marker.get("project_id") != project_id or marker.get("session_id") != session_id:
        raise ValueError("session ownership marker mismatch")
    token = marker.get("token")
    if not isinstance(token, str) or TOKEN_RE.fullmatch(token) is None:
        raise ValueError("session ownership marker has an invalid token")
    if expected_token is not None and token != expected_token:
        raise ValueError("session ownership marker mismatch")
    created_at = marker.get("created_at")
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or not math.isfinite(created_at)
        or created_at <= 0
    ):
        raise ValueError("session ownership marker has invalid creation time")
    return float(created_at)


def entry_exists(parent_fd, name):
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def session_process(session_fd):
    has_pgid = entry_exists(session_fd, "pgid")
    has_start = entry_exists(session_fd, "process-start")
    if not has_pgid and not has_start:
        return None
    if has_pgid != has_start:
        raise ValueError("session process identity is incomplete")
    pgid_text = read_text_at(session_fd, "pgid")
    start = read_text_at(session_fd, "process-start")
    try:
        pgid = int(pgid_text)
    except ValueError as exc:
        raise ValueError("session process identity is malformed") from exc
    if pgid <= 1 or not start or not start.startswith(("proc:", "ps:")):
        raise ValueError("session process identity is malformed")
    return pgid, start


def stale_session_process(session_fd):
    has_pgid = entry_exists(session_fd, "pgid")
    has_start = entry_exists(session_fd, "process-start")
    if not has_pgid and not has_start:
        return None
    if has_start and not has_pgid:
        raise ValueError("session process identity is incomplete")
    if has_pgid and not has_start:
        pgid_text = read_text_at(session_fd, "pgid")
        try:
            pgid = int(pgid_text)
        except ValueError as exc:
            raise ValueError("session process identity is malformed") from exc
        if pgid <= 1:
            raise ValueError("session process identity is malformed")
        if group_alive(pgid):
            raise ValueError(
                "session has a live process group with incomplete identity"
            )
        return None
    return session_process(session_fd)


def project_marker(project_id):
    return {"schema_version": 1, "project_id": project_id}


def cancellation_marker(project_id, session_id, token, created_at):
    return {
        "schema_version": 1,
        "project_id": project_id,
        "session_id": session_id,
        "token": token,
        "created_at": created_at,
    }


def validate_cancellation(marker, project_id, session_id, expected_token=None):
    created_at = validate_identity(
        marker, project_id, session_id, expected_token=expected_token
    )
    return created_at


def open_cancellations(project_fd, create):
    try:
        cancellations_fd, _created = ensure_child_dir(
            project_fd, "cancellations", create=create
        )
    except FileNotFoundError:
        return None
    return cancellations_fd


def prune_cancellations(cancellations_fd, project_id):
    now = time.time()
    active = 0
    for name in sorted(os.listdir(cancellations_fd)):
        if SESSION_RE.fullmatch(name) is None:
            raise ValueError("cancellation directory contains an invalid entry")
        marker = read_json_at(cancellations_fd, name)
        created_at = validate_cancellation(marker, project_id, name)
        if now - created_at > CANCELLATION_TTL_SECONDS:
            os.unlink(name, dir_fd=cancellations_fd)
        else:
            active += 1
    return active


def record_cancellation(project_fd, project_id, session_id, token):
    cancellations_fd = open_cancellations(project_fd, create=True)
    if cancellations_fd is None:
        raise RuntimeError("could not create cancellation directory")
    try:
        active = prune_cancellations(cancellations_fd, project_id)
        try:
            existing = read_json_at(cancellations_fd, session_id)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            validate_cancellation(
                existing, project_id, session_id, expected_token=token
            )
            return
        if active >= MAX_CANCELLATION_INTENTS:
            raise RuntimeError("too many active development cancellation intents")
        created_at = time.time()
        write_json_at(
            cancellations_fd,
            session_id,
            cancellation_marker(project_id, session_id, token, created_at),
        )
        os.utime(
            session_id,
            (created_at, created_at),
            dir_fd=cancellations_fd,
            follow_symlinks=False,
        )
    finally:
        os.close(cancellations_fd)


def has_cancellation(project_fd, project_id, session_id, token):
    cancellations_fd = open_cancellations(project_fd, create=False)
    if cancellations_fd is None:
        return False
    try:
        try:
            marker = read_json_at(cancellations_fd, session_id)
        except FileNotFoundError:
            return False
        validate_cancellation(marker, project_id, session_id, expected_token=token)
        return True
    finally:
        os.close(cancellations_fd)


def open_control_lock(project_fd, create):
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    fd = os.open("control.lock", flags, PRIVATE_FILE_MODE, dir_fd=project_fd)
    try:
        current = os.fstat(fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.geteuid()
            or stat.S_IMODE(current.st_mode) != PRIVATE_FILE_MODE
        ):
            raise ValueError("project control lock is not a private owned file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = os.fstat(fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.geteuid()
            or stat.S_IMODE(current.st_mode) != PRIVATE_FILE_MODE
        ):
            raise ValueError("project control lock changed while acquiring it")
        return fd
    except BaseException:
        os.close(fd)
        raise


def close_locked_project(project_fd, lock_fd):
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
        os.close(project_fd)


def open_locked_project(dev_root, project_id, create=False):
    try:
        root_fd = open_absolute_dir(dev_root, create=create)
    except FileNotFoundError:
        if create:
            raise
        return None
    project_fd = None
    lock_fd = None
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX)
        try:
            project_fd, project_created = ensure_child_dir(
                root_fd, project_id, create=create
            )
        except FileNotFoundError:
            if create:
                raise
            return None
        if project_created:
            write_json_at(project_fd, "owner.json", project_marker(project_id))
        owner = read_json_at(project_fd, "owner.json")
        if owner != project_marker(project_id):
            raise ValueError("project ownership marker mismatch")
        lock_fd = open_control_lock(project_fd, create=create)
        return project_fd, lock_fd
    except BaseException:
        if lock_fd is not None:
            os.close(lock_fd)
        if project_fd is not None:
            os.close(project_fd)
        raise
    finally:
        fcntl.flock(root_fd, fcntl.LOCK_UN)
        os.close(root_fd)


def cleanup_stale(tmp_fd, project_id, stale_after):
    removed = []
    retained = []
    now = time.time()
    expected_dev = os.fstat(tmp_fd).st_dev
    for name in sorted(os.listdir(tmp_fd)):
        if SESSION_RE.fullmatch(name) is None:
            continue
        reason = None
        try:
            session_stat = os.stat(name, dir_fd=tmp_fd, follow_symlinks=False)
            if not stat.S_ISDIR(session_stat.st_mode) or session_stat.st_uid != os.geteuid():
                raise ValueError("session entry is not an owned directory")
            session_fd = open_dir_at(tmp_fd, name)
            try:
                require_private_directory(session_fd, "session directory")
                marker = read_json_at(session_fd, "owner.json")
                created = validate_identity(marker, project_id, name)
                marker_stat = os.stat(
                    "owner.json", dir_fd=session_fd, follow_symlinks=False
                )
                if abs(marker_stat.st_mtime - created) > 2:
                    raise ValueError("session creation timestamp does not match its marker")
                age = now - created
                if age <= stale_after:
                    continue
                process = stale_session_process(session_fd)
                if process is not None:
                    pgid, expected_start = process
                    if group_alive(pgid):
                        observed = process_start(pgid)
                        reason = (
                            "live workload"
                            if observed == expected_start
                            else "live or reused process group"
                        )
            finally:
                os.close(session_fd)
            if reason is not None:
                retained.append({"session_id": name, "reason": reason})
                continue
            remove_tree_at(tmp_fd, name, expected_dev)
            removed.append(name)
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            retained.append({"session_id": name, "reason": str(exc)})
    return removed, retained


def validate_runner_config(config, dev_root, project_id, session_id, token):
    expected_keys = {
        "schema_version",
        "dev_root",
        "project_id",
        "session_id",
        "token",
        "cores",
        "cache_root",
        "build_environment",
        "project_python",
        "server_name",
        "machine_id",
        "profile",
    }
    if not isinstance(config, dict) or set(config) != expected_keys:
        raise ValueError("runner configuration is invalid")
    expected_cache = dev_root + "/" + project_id + "/cache"
    if config.get("schema_version") != 2:
        raise ValueError("runner configuration is invalid")
    if config.get("dev_root") != dev_root or config.get("project_id") != project_id:
        raise ValueError("runner configuration path ownership is invalid")
    if config.get("session_id") != session_id or config.get("token") != token:
        raise ValueError("runner configuration session ownership is invalid")
    cores = config.get("cores")
    if isinstance(cores, bool) or not isinstance(cores, int) or cores <= 0:
        raise ValueError("runner configuration cores are invalid")
    if config.get("cache_root") != expected_cache:
        raise ValueError("runner configuration cache path is invalid")
    for field in ("server_name", "machine_id"):
        value = config.get(field)
        if not isinstance(value, str) or PROJECT_RE.fullmatch(value) is None:
            raise ValueError(f"runner configuration {field} is invalid")
    profile = config.get("profile")
    if profile is not None and (
        not isinstance(profile, str) or PROJECT_RE.fullmatch(profile) is None
    ):
        raise ValueError("runner configuration profile is invalid")
    build_environment = config.get("build_environment")
    if not isinstance(build_environment, dict):
        raise ValueError("runner build environment is invalid")
    allowed = {"MAKEFLAGS", "CMAKE_BUILD_PARALLEL_LEVEL", "CARGO_BUILD_JOBS"}
    if set(build_environment) - allowed or any(
        not isinstance(value, str) for value in build_environment.values()
    ):
        raise ValueError("runner build environment is invalid")
    project_python = config.get("project_python")
    if project_python is not None and (
        not isinstance(project_python, str)
        or not project_python.startswith("/")
        or "\x00" in project_python
        or "\n" in project_python
        or "\r" in project_python
    ):
        raise ValueError("runner project Python is invalid")


def action_create():
    locked = open_locked_project(
        payload["dev_root"], payload["project_id"], create=True
    )
    if locked is None:
        raise RuntimeError("could not create development project root")
    project_fd, lock_fd = locked
    try:
        if has_cancellation(
            project_fd,
            payload["project_id"],
            payload["session_id"],
            payload["token"],
        ):
            fail(
                "cancelled",
                "session creation was cancelled before materialization",
                2,
            )
        if (
            subprocess.run(
                ["rsync", "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            != 0
        ):
            fail("preflight", "rsync is unavailable on the remote server")
        cache_fd, _cache_created = ensure_child_dir(project_fd, "cache")
        os.close(cache_fd)
        tmp_fd, _tmp_created = ensure_child_dir(project_fd, "tmp")
        try:
            stale_removed, stale_retained = cleanup_stale(
                tmp_fd, payload["project_id"], payload["stale_after_seconds"]
            )
            os.mkdir(payload["session_id"], PRIVATE_DIR_MODE, dir_fd=tmp_fd)
            session_created = True
            try:
                session_fd = open_dir_at(tmp_fd, payload["session_id"])
                try:
                    require_private_directory(session_fd, "session directory")
                    created_at = time.time()
                    marker = {
                        "schema_version": 1,
                        "project_id": payload["project_id"],
                        "session_id": payload["session_id"],
                        "token": payload["token"],
                        "created_at": created_at,
                    }
                    write_json_at(session_fd, "owner.json", marker)
                    os.utime(
                        "owner.json",
                        (created_at, created_at),
                        dir_fd=session_fd,
                        follow_symlinks=False,
                    )
                    os.mkdir("source", PRIVATE_DIR_MODE, dir_fd=session_fd)
                    write_json_at(session_fd, "session.json", payload["runner_config"])
                    write_bytes_at(
                        session_fd, "command.sh", payload["command"].encode(), 0o600
                    )
                    write_bytes_at(
                        session_fd, "runner.py", payload["runner_source"].encode(), 0o700
                    )
                    write_bytes_at(
                        session_fd,
                        "cleanup.py",
                        payload["cleanup_source"].encode(),
                        0o600,
                    )
                finally:
                    os.close(session_fd)
            except BaseException:
                if session_created:
                    remove_tree_at(
                        tmp_fd, payload["session_id"], os.fstat(tmp_fd).st_dev
                    )
                raise
        finally:
            os.close(tmp_fd)
    finally:
        close_locked_project(project_fd, lock_fd)
    emit(
        True,
        "created",
        stale_removed=stale_removed,
        stale_retained=stale_retained,
    )


def action_cleanup(cancel=False):
    compensate = payload.get("record_if_absent", False)
    locked = open_locked_project(
        payload["dev_root"], payload["project_id"], create=compensate
    )
    if locked is None:
        emit(True, "already_absent")
        return
    project_fd, lock_fd = locked
    tmp_fd = None
    session_fd = None
    result_action = "cancelled" if cancel else "removed"
    try:
        try:
            tmp_fd = open_dir_at(project_fd, "tmp")
            require_private_directory(tmp_fd, "development tmp directory")
        except FileNotFoundError:
            tmp_fd = None
        if tmp_fd is not None:
            try:
                session_fd = open_dir_at(tmp_fd, payload["session_id"])
            except FileNotFoundError:
                session_fd = None
        if session_fd is None:
            if compensate:
                record_cancellation(
                    project_fd,
                    payload["project_id"],
                    payload["session_id"],
                    payload["token"],
                )
                result_action = "cancellation_recorded"
            else:
                result_action = "already_absent"
        else:
            try:
                require_private_directory(session_fd, "session directory")
                marker = read_json_at(session_fd, "owner.json")
                validate_identity(
                    marker,
                    payload["project_id"],
                    payload["session_id"],
                    payload["token"],
                )
                process = session_process(session_fd)
                if process is not None:
                    pgid, expected_start = process
                    alive = group_alive(pgid)
                    observed_start = process_start(pgid) if alive else None
                    if alive and observed_start != expected_start:
                        fail(
                            "identity",
                            "process group identity does not match this session",
                            2,
                        )
                    if alive and not cancel:
                        fail("active", "session workload is still alive", 2)
                    if alive:
                        try:
                            os.killpg(pgid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        deadline = time.monotonic() + float(
                            payload.get("cancel_timeout", 5)
                        )
                        while group_alive(pgid) and time.monotonic() < deadline:
                            time.sleep(0.05)
                        if group_alive(pgid):
                            observed_start = process_start(pgid)
                            if observed_start != expected_start:
                                fail(
                                    "identity",
                                    "process group changed before forced cancellation",
                                    2,
                                )
                            try:
                                os.killpg(pgid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            deadline = time.monotonic() + 2
                            while group_alive(pgid) and time.monotonic() < deadline:
                                time.sleep(0.05)
                        if group_alive(pgid):
                            fail("active", "session workload did not stop", 2)
            finally:
                os.close(session_fd)
                session_fd = None
            remove_tree_at(
                tmp_fd, payload["session_id"], os.fstat(tmp_fd).st_dev
            )
    finally:
        if session_fd is not None:
            os.close(session_fd)
        if tmp_fd is not None:
            os.close(tmp_fd)
        close_locked_project(project_fd, lock_fd)
    emit(True, result_action)


try:
    os.umask(0o077)
    if not isinstance(payload, dict):
        fail("validation", "payload must be an object")
    action = payload.get("action")
    if action not in {"create", "cleanup", "cancel"}:
        fail("validation", "unsupported action")
    payload["dev_root"] = validate_dev_root(payload.get("dev_root"))
    payload["project_id"] = validate_component(
        payload.get("project_id"), PROJECT_RE, "project id"
    )
    payload["session_id"] = validate_component(
        payload.get("session_id"), SESSION_RE, "session id"
    )
    payload["token"] = validate_component(
        payload.get("token"), TOKEN_RE, "session token"
    )
    cancel_timeout = payload.get("cancel_timeout", 5)
    if (
        isinstance(cancel_timeout, bool)
        or not isinstance(cancel_timeout, (int, float))
        or not math.isfinite(cancel_timeout)
        or cancel_timeout <= 0
        or cancel_timeout > 300
    ):
        fail("validation", "invalid cancellation timeout")
    record_if_absent = payload.get("record_if_absent", False)
    if not isinstance(record_if_absent, bool):
        fail("validation", "invalid absent-session compensation flag")
    if action == "create" and record_if_absent:
        fail("validation", "create cannot record absent-session compensation")
    if action == "create":
        stale_after = payload.get("stale_after_seconds")
        if isinstance(stale_after, bool) or not isinstance(stale_after, int) or stale_after <= 0:
            fail("validation", "invalid stale threshold")
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            fail("validation", "invalid workload command")
        runner_source = payload.get("runner_source")
        cleanup_source = payload.get("cleanup_source")
        if not isinstance(runner_source, str) or not runner_source:
            fail("validation", "invalid runner helper")
        if not isinstance(cleanup_source, str) or not cleanup_source:
            fail("validation", "invalid cleanup helper")
        validate_runner_config(
            payload.get("runner_config"),
            payload["dev_root"],
            payload["project_id"],
            payload["session_id"],
            payload["token"],
        )
        action_create()
    elif action == "cleanup":
        action_cleanup(cancel=False)
    else:
        action_cleanup(cancel=True)
except SystemExit:
    raise
except ValueError as exc:
    fail("validation", str(exc))
except BaseException as exc:
    fail("error", str(exc))
'''


def _remote_payload_stdin(payload: dict[str, Any]) -> bytes:
    encoded = base64.b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode()
    return (
        f"import base64,json\npayload=json.loads(base64.b64decode({encoded!r}))\n"
        + REMOTE_HELPER_SOURCE
    ).encode()


def _remote_result(stdout: bytes) -> dict[str, Any] | None:
    for line in reversed(stdout.decode(errors="replace").splitlines()):
        if not line.startswith(REMOTE_RESULT_PREFIX):
            continue
        try:
            value = json.loads(line[len(REMOTE_RESULT_PREFIX) :])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


def _ssh_control(
    server: DevServer,
    payload: dict[str, Any],
    *,
    timeout: int,
) -> dict[str, Any]:
    argv = [
        "ssh",
        *ssh_connection_options(timeout),
        server.ssh,
        remote_python_stdin_command("python3"),
    ]
    try:
        completed = subprocess.run(
            argv,
            input=_remote_payload_stdin(payload),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 15,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"remote development control timed out after {exc.timeout}s"
        ) from exc
    result = _remote_result(completed.stdout)
    if completed.returncode == 0 and result is not None and result.get("ok") is True:
        return result
    detail = completed.stderr.decode(errors="replace").strip()
    if result is not None and result.get("message"):
        detail = str(result["message"])
    elif not detail:
        detail = completed.stdout.decode(errors="replace").strip()
    raise RuntimeError(detail or f"remote development control exited {completed.returncode}")


def _session_payload(
    action: str,
    *,
    config: DevProjectConfig,
    server: DevServer,
    session: DevSession,
    command: str | None = None,
    profile: str | None = None,
    cancel_timeout: int | None = None,
    record_if_absent: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "dev_root": server.dev_root,
        "project_id": config.project_id,
        "session_id": session.session_id,
        "token": session.token,
    }
    if action == "create":
        build_environment = {
            name: os.environ[name] for name in BUILD_ENVIRONMENT if name in os.environ
        }
        payload.update(
            {
                "stale_after_seconds": config.stale_after_seconds,
                "command": command,
                "runner_source": DEV_RUNNER_SOURCE,
                "cleanup_source": REMOTE_HELPER_SOURCE,
                "runner_config": {
                    "schema_version": 2,
                    "dev_root": server.dev_root,
                    "project_id": config.project_id,
                    "session_id": session.session_id,
                    "token": session.token,
                    "cores": server.cores,
                    "cache_root": session.cache_root,
                    "build_environment": build_environment,
                    "project_python": config.project_python_for(server.name),
                    "server_name": server.name,
                    "machine_id": server.machine_id,
                    "profile": profile,
                },
            }
        )
    if cancel_timeout is not None:
        payload["cancel_timeout"] = cancel_timeout
    if record_if_absent:
        payload["record_if_absent"] = True
    return payload


def _rsync_source(
    plan: DevSourcePlan,
    server: DevServer,
    session: DevSession,
    *,
    timeout: int,
) -> None:
    ssh_command = shlex.join(["ssh", *ssh_connection_options(timeout)])
    destination = f"{server.ssh}:{session.source_root}/"
    argv = [
        "rsync",
        "-a",
        "--compress",
        "--from0",
        "--files-from=-",
        "--safe-links",
        "-e",
        ssh_command,
        str(plan.source_root) + "/",
        destination,
    ]
    completed = subprocess.run(
        argv,
        input=plan.files_from_bytes(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()
        raise RuntimeError(detail or f"rsync exited {completed.returncode}")


def _run_foreground(server: DevServer, session: DevSession, timeout: int) -> int:
    remote_command = shlex.join(["python3", session.runner_path])
    argv = ["ssh", *ssh_connection_options(timeout), server.ssh, remote_command]
    completed = subprocess.run(argv, check=False)
    if completed.returncode < 0:
        return 128 + (-completed.returncode)
    return completed.returncode


def resolve_source_root(config: DevProjectConfig, override: Path | None) -> Path:
    if override is None:
        return resolve_source_repo(config.source_root, None)
    if not override.is_absolute():
        raise ValueError("--source-root must be an absolute path")
    resolved = override.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"development source root does not exist: {resolved}")
    return resolved


def run_dev(args: argparse.Namespace) -> int:
    config_path = resolve_project_config(args.project_config)
    config = load_dev_project_config(config_path)
    invocation = resolve_dev_invocation(
        config,
        command=getattr(args, "command", None),
        profile=getattr(args, "profile", None),
    )
    source_root = resolve_source_root(config, args.source_root)
    plan = build_source_plan(
        source_root,
        include=invocation.include,
        exclude=invocation.exclude,
    )
    registry_path = args.server_registry.expanduser().resolve(strict=True)
    server = resolve_dev_server(
        registry_path,
        args.server,
        ssh_profile=args.ssh_profile,
        timeout=args.timeout,
    )
    session_id = "dev-" + secrets.token_hex(8)
    token = secrets.token_hex(16)
    remote_root = server.session_root(config.project_id, session_id)
    session = DevSession(
        project_id=config.project_id,
        session_id=session_id,
        token=token,
        remote_root=remote_root,
        cache_root=str(PurePosixPath(server.project_root(config.project_id)) / "cache"),
    )

    create_attempted = False
    create_confirmed = False
    foreground_started = False
    foreground_completed = False
    returncode = INFRASTRUCTURE_EXIT_CODE
    previous_handlers: dict[int, Any] = {}
    cleanup_in_progress = False
    deferred_signals: list[int] = []

    def handle_local_signal(signum: int, _frame: object) -> None:
        if cleanup_in_progress:
            deferred_signals.append(signum)
            return
        raise _LocalSignal(signum)

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, handle_local_signal)
    try:
        create_attempted = True
        _ssh_control(
            server,
            _session_payload(
                "create",
                config=config,
                server=server,
                session=session,
                command=invocation.command,
                profile=invocation.profile,
            ),
            timeout=args.timeout,
        )
        create_confirmed = True
        _rsync_source(plan, server, session, timeout=args.timeout)
        foreground_started = True
        returncode = _run_foreground(server, session, args.timeout)
        foreground_completed = True
    except KeyboardInterrupt:
        returncode = 128 + signal.SIGINT
    except _LocalSignal as exc:
        returncode = 128 + exc.signum
    finally:
        cleanup_in_progress = True
        cleanup_verified = True
        try:
            if create_attempted:
                cancel = foreground_started and (
                    not foreground_completed or returncode in _CANCELLATION_STATUSES
                )
                try:
                    _ssh_control(
                        server,
                        _session_payload(
                            "cancel" if cancel else "cleanup",
                            config=config,
                            server=server,
                            session=session,
                            cancel_timeout=min(max(args.timeout, 5), 30),
                            record_if_absent=not create_confirmed,
                        ),
                        timeout=args.timeout,
                    )
                except (OSError, RuntimeError) as exc:
                    cleanup_verified = False
                    print(
                        f"[remote-runner dev] cleanup could not be verified: {exc}",
                        file=sys.stderr,
                    )
                    returncode = INFRASTRUCTURE_EXIT_CODE
            if deferred_signals and cleanup_verified:
                returncode = 128 + deferred_signals[-1]
        finally:
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)
    return returncode


def default_server_registry() -> Path:
    return DEFAULT_SERVER_REGISTRY
