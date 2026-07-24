from __future__ import annotations

import base64
import json
import os
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .execution_registry import (
    PROCESS_TITLE_PRIVACY_MODE,
    ProjectPaths,
    current_command_path,
    load_current_run,
    process_title_privacy_mode,
    runtime_path,
    sha256_bytes,
)
from .output_paths import validate_resolved_output
from .remote_shell import remote_python_stdin_command
from .tmux import run_tmux_session


@dataclass(frozen=True)
class LaunchAsset:
    name: str
    content: bytes
    sha256: str
    mode: int

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size": len(self.content),
            "sha256": self.sha256,
            "mode": f"{self.mode:04o}",
        }


@dataclass(frozen=True)
class LaunchPlan:
    run_id: str
    ssh_target: str
    runtime_path: str
    tmux_session: str
    privacy_mode: str | None
    assets: tuple[LaunchAsset, ...]
    bootstrap_ssh_argv: tuple[str, ...]
    bootstrap_stdin: bytes

    def public(self) -> dict[str, Any]:
        value = {
            "run_id": self.run_id,
            "ssh_target": self.ssh_target,
            "runtime_path": self.runtime_path,
            "tmux_session": self.tmux_session,
            "assets": [asset.public() for asset in self.assets],
            "bootstrap_ssh_argv": list(self.bootstrap_ssh_argv),
            "tmux_argv": [
                "tmux",
                "new-session",
                "-d",
                "-s",
                self.tmux_session,
                "bash",
                f"{self.runtime_path}/run.sh",
            ],
        }
        if self.privacy_mode is not None:
            value["privacy_mode"] = self.privacy_mode
        return value


def _sitecustomize_source(run_id: str) -> bytes:
    title = json.dumps(run_id)
    return f'''import os


def _privacy_failure():
    os._exit(86)


if os.environ.get("RR_PROCESS_TITLE_REQUIRED") != "1":
    _privacy_failure()
if os.environ.get("RR_PROCESS_TITLE") != {title}:
    _privacy_failure()
os.environ["SPT_NOENV"] = "1"
try:
    import setproctitle as _setproctitle

    _setproctitle.setproctitle({title})
    if _setproctitle.getproctitle() != {title}:
        _privacy_failure()
except BaseException:
    _privacy_failure()
'''.encode()


SUPERVISOR_SOURCE = r'''import os
import json
import signal
import subprocess
import sys
import time
from pathlib import Path


run_id = sys.argv[1]
runtime_dir = Path(sys.argv[2])
supervisor_pid = os.getpid()
process_group = os.getpgrp()
stopping = False

if process_group != supervisor_pid:
    raise SystemExit("supervisor did not become its own process-group leader")

owner = runtime_dir / "owner.json"
pgid_path = runtime_dir / "pgid"
owner_tmp = runtime_dir / (".owner.tmp." + str(supervisor_pid))
pgid_tmp = runtime_dir / (".pgid.tmp." + str(supervisor_pid))
owner_tmp.write_text(
    json.dumps({"run_id": run_id, "pid": supervisor_pid, "pgid": process_group}) + "\n",
    encoding="utf-8",
)
pgid_tmp.write_text(str(process_group) + "\n", encoding="utf-8")
os.chmod(owner_tmp, 0o600)
os.chmod(pgid_tmp, 0o600)
os.replace(owner_tmp, owner)
os.replace(pgid_tmp, pgid_path)


def request_stop(_signum, _frame):
    global stopping
    stopping = True


def group_has_other_members() -> bool:
    proc_root = Path("/proc")
    if proc_root.is_dir():
        for proc_dir in proc_root.iterdir():
            if not proc_dir.name.isdigit() or int(proc_dir.name) == supervisor_pid:
                continue
            try:
                fields = (proc_dir / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
                if len(fields) >= 3 and int(fields[2]) == process_group:
                    return True
            except (OSError, ValueError, UnicodeDecodeError):
                continue
        return False

    try:
        process = subprocess.Popen(
            ["ps", "-ww", "-axo", "pid=,pgid="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        stdout, _ = process.communicate()
    except OSError:
        return True
    for line in stdout.splitlines():
        fields = line.strip().split()
        try:
            if len(fields) >= 2 and int(fields[0]) != supervisor_pid and fields[1] == str(process_group):
                return True
        except ValueError:
            continue
    return False


signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGHUP, request_stop)
signal.signal(signal.SIGINT, request_stop)
child_pid = os.fork()
if child_pid == 0:
__WORKLOAD_EXEC__

while True:
    try:
        _, child_status = os.waitpid(child_pid, 0)
        break
    except InterruptedError:
        continue

if os.WIFEXITED(child_status):
    child_exit_code = os.WEXITSTATUS(child_status)
elif os.WIFSIGNALED(child_status):
    child_exit_code = 128 + os.WTERMSIG(child_status)
else:
    child_exit_code = 1

while group_has_other_members():
    time.sleep(0.05)

if stopping or (runtime_dir / "stop.request").exists():
    raise SystemExit(143)
raise SystemExit(child_exit_code)
'''


def _supervisor_source(run_id: str, privacy_mode: str | None) -> str:
    if privacy_mode is None:
        workload_exec = (
            '    os.execvp("bash", ("remote-runner:" + run_id + "-workload", "-s"))'
        )
    elif privacy_mode == PROCESS_TITLE_PRIVACY_MODE:
        title = json.dumps(run_id)
        workload_exec = f'''    workload_env = os.environ.copy()
    workload_env["RR_PROCESS_TITLE_REQUIRED"] = "1"
    workload_env["RR_PROCESS_TITLE"] = {title}
    original_pythonpath = workload_env.get("PYTHONPATH")
    workload_env["PYTHONPATH"] = str(runtime_dir) + (
        os.pathsep + original_pythonpath if original_pythonpath else ""
    )
    os.execvpe(
        "bash",
        ("remote-runner:" + run_id + "-workload", "-s"),
        workload_env,
    )'''
    else:
        raise ValueError(f"unsupported privacy mode: {privacy_mode!r}")
    if SUPERVISOR_SOURCE.count("__WORKLOAD_EXEC__") != 1:
        raise RuntimeError("supervisor workload placeholder is invalid")
    return SUPERVISOR_SOURCE.replace("__WORKLOAD_EXEC__", workload_exec)


def _wrapper_source(
    run_id: str,
    label: str,
    workdir: str,
    project_python: str,
    privacy_mode: str | None,
    workload_class: str = "standard",
    *,
    output_root: str | None = None,
    output_path: str | None = None,
) -> str:
    quoted_run_id = shlex.quote(run_id)
    quoted_label_json = shlex.quote(json.dumps(label, ensure_ascii=True))
    quoted_workdir = shlex.quote(workdir)
    quoted_project_python = shlex.quote(project_python)
    quoted_workload_class = shlex.quote(workload_class)
    workload_environment = [f"RR_PROJECT_PYTHON={quoted_project_python}"]
    if output_root is not None:
        workload_environment.append(f"RR_OUTPUT_ROOT={shlex.quote(output_root)}")
    if output_path is not None:
        workload_environment.extend(
            (
                f"RR_OUTPUT_PATH={shlex.quote(output_path)}",
                f"RR_OUTPUT_DIR={shlex.quote(str(PurePosixPath(output_path).parent))}",
            )
        )
    workload_prefix = " ".join(workload_environment)
    supervisor_b64 = base64.b64encode(
        _supervisor_source(run_id, privacy_mode).encode()
    ).decode()
    return f'''#!/usr/bin/env bash
set -u
umask 077
cd -- "${{0%/*}}" || exit 125
runtime_dir=$(pwd -P) || exit 125
run_id={quoted_run_id}
label_json={quoted_label_json}
workload_class={quoted_workload_class}
workdir={quoted_workdir}
status_path="${{runtime_dir}}/status.json"
log_path="${{runtime_dir}}/log"
pgid_path="${{runtime_dir}}/pgid"
stop_path="${{runtime_dir}}/stop.request"
workload_pid=""
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

write_status() {{
  local state=$1
  local exit_code=$2
  local finished_at=$3
  local finished_json=null
  local temporary="${{runtime_dir}}/.status.tmp.$$"
  if [[ "$finished_at" != "null" ]]; then
    finished_json="\\\"${{finished_at}}\\\""
  fi
  printf '{{"schema_version":1,"run_id":"%s","label":%s,"workload_class":"%s","state":"%s","exit_code":%s,"started_at":"%s","finished_at":%s}}\n' \
    "$run_id" "$label_json" "$workload_class" "$state" "$exit_code" "$started_at" "$finished_json" >"$temporary"
  chmod 600 "$temporary"
  mv -f -- "$temporary" "$status_path"
}}

forward_signal() {{
  local signal=$1
  local rc=$2
  if [[ -n "$workload_pid" ]]; then
    kill -"$signal" -- "-$workload_pid" 2>/dev/null || true
    wait "$workload_pid" 2>/dev/null || true
  fi
  exit "$rc"
}}

finalize() {{
  local rc=$?
  local state=failed
  local finished_at
  trap - EXIT HUP INT TERM
  finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if [[ -f "$stop_path" ]]; then
    state=stopped
  elif [[ $rc -eq 0 ]]; then
    state=succeeded
  fi
  write_status "$state" "$rc" "$finished_at" || true
  printf '[REMOTE_RUNNER_END] %s state=%s rc=%d\n' "$finished_at" "$state" "$rc"
  exit "$rc"
}}

trap finalize EXIT
trap 'forward_signal HUP 129' HUP
trap 'forward_signal INT 130' INT
trap 'forward_signal TERM 143' TERM
exec >>"$log_path" 2>&1 || exit 125
printf '[REMOTE_RUNNER_START] %s\n' "$started_at"
write_status running null null || exit 125
cd -- "$workdir" || exit 125

unset RR_OUTPUT_ROOT RR_OUTPUT_PATH RR_OUTPUT_DIR
{workload_prefix} {quoted_project_python} -c 'import base64,os,sys; os.setsid(); os.execv(sys.executable, ("remote-runner:" + sys.argv[1], "-c", base64.b64decode(sys.argv[3]).decode(), sys.argv[1], sys.argv[2]))' "$run_id" "$runtime_dir" {supervisor_b64!r} \
  < "${{runtime_dir}}/command.sh" &
workload_pid=$!
wait "$workload_pid"
workload_rc=$?
while kill -0 -- "-$workload_pid" 2>/dev/null; do
  sleep 0.05
done
exit "$workload_rc"
'''


REMOTE_BOOTSTRAP = r'''import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

RUN_ID_RE = re.compile(r"^rr-[0-9a-f]{16}$")
ALLOWED_ASSETS = {"run.sh": 0o700, "command.sh": 0o600}

def exact_tmux_target(session_name):
    return "=" + session_name

def emit(ok, phase, message=None, tmux_started=False, status=None):
    print("RR_BOOTSTRAP_RESULT " + json.dumps({
        "ok": ok,
        "phase": phase,
        "message": message,
        "tmux_started": tmux_started,
        "status": status,
    }, sort_keys=True), flush=True)

def fail(message, phase="preflight"):
    emit(False, phase, message, False, None)
    raise SystemExit(1)

def read_status(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None

def write_private(path, content, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
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

def remove_runtime(runtime):
    if not runtime.exists():
        return
    for path in sorted(runtime.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    runtime.rmdir()

run_id = payload.get("run_id")
if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
    fail("invalid run id", "validation")
if payload.get("runtime_path") != "~/.rr/" + run_id:
    fail("runtime path is not derived from run id", "validation")
if payload.get("tmux_session") != run_id:
    fail("tmux session is not derived from run id", "validation")

workdir_value = payload.get("remote_workdir")
python_value = payload.get("project_python")
if not isinstance(workdir_value, str) or not PurePosixPath(workdir_value).is_absolute():
    fail("configured workdir must be an absolute path", "validation")
if not isinstance(python_value, str) or not PurePosixPath(python_value).is_absolute():
    fail("configured project Python must be an absolute path", "validation")
workdir = Path(workdir_value)
project_python = Path(python_value)
if shutil.which("tmux") is None:
    fail("tmux is not installed")
if not workdir.is_dir():
    fail("configured workdir does not exist: " + str(workdir))
if not project_python.exists() or not os.access(project_python, os.X_OK):
    fail("configured project Python is not executable: " + str(project_python))
try:
    python_check = subprocess.run(
        [str(project_python), "-c", "import sys; print(sys.executable)"],
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )
except (OSError, subprocess.TimeoutExpired) as exc:
    fail("configured project Python check failed: " + str(exc))
if python_check.returncode != 0:
    fail("configured project Python exited non-zero: " + (python_check.stderr.strip() or str(python_check.returncode)))

expected_revision = payload.get("expected_revision")
require_clean = payload.get("require_clean_worktree") is True
if expected_revision is not None or require_clean:
    head = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )
    if head.returncode != 0:
        fail("configured workdir is not a readable Git worktree: " + (head.stderr.strip() or str(head.returncode)))
    actual_revision = head.stdout.strip()
    if expected_revision is not None and actual_revision != expected_revision:
        fail("remote Git revision mismatch: expected " + str(expected_revision) + ", found " + actual_revision)
if require_clean:
    clean = subprocess.run(
        ["git", "-C", str(workdir), "status", "--porcelain"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )
    if clean.returncode != 0 or clean.stdout:
        fail("remote Git worktree is not clean")

output_root = payload.get("output_root")
output_relpath = payload.get("output_relpath")
output_path = payload.get("output_path")
if output_relpath is not None:
    if not isinstance(output_root, str) or not PurePosixPath(output_root).is_absolute():
        fail("output root must be an absolute path", "validation")
    if not isinstance(output_relpath, str) or not output_relpath:
        fail("output relpath must be a non-empty string", "validation")
    relpath = PurePosixPath(output_relpath)
    if relpath.is_absolute() or str(relpath) != output_relpath:
        fail("output relpath must be a normalized relative path", "validation")
    if any(part in {".", ".."} for part in relpath.parts):
        fail("output relpath contains traversal", "validation")
    if "$" in output_relpath or "`" in output_relpath or output_relpath.startswith("~"):
        fail("output relpath cannot use shell expansion", "validation")
    expected_output = str(PurePosixPath(output_root) / relpath)
    if output_path != expected_output:
        fail("resolved output path identity mismatch", "validation")
elif output_root is not None:
    fail("output root requires output relpath", "validation")
if output_path is not None:
    if not isinstance(output_path, str) or not PurePosixPath(output_path).is_absolute():
        fail("output path must be an absolute path", "validation")
    if Path(output_path).exists():
        fail("output path already exists: " + output_path)

root = Path.home() / ".rr"
runtime = root / run_id
tmux_check = subprocess.run(
    ["tmux", "has-session", "-t", exact_tmux_target(run_id)],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
if tmux_check.returncode == 0:
    fail("tmux session already exists: " + run_id)
if runtime.exists():
    fail("remote runtime already exists: " + str(runtime))

assets = payload.get("assets")
if not isinstance(assets, list) or len(assets) != len(ALLOWED_ASSETS):
    fail("invalid launch asset set", "validation")
decoded = {}
for item in assets:
    if not isinstance(item, dict):
        fail("invalid launch asset metadata", "validation")
    name = item.get("name")
    if name not in ALLOWED_ASSETS or name in decoded:
        fail("invalid or duplicate launch asset", "validation")
    if item.get("mode") != ALLOWED_ASSETS[name]:
        fail("launch asset mode mismatch: " + str(name), "validation")
    try:
        content = base64.b64decode(item.get("data", ""), validate=True)
    except Exception:
        fail("invalid base64 launch asset: " + str(name), "validation")
    actual = "sha256:" + hashlib.sha256(content).hexdigest()
    if actual != item.get("sha256"):
        fail("launch asset digest mismatch: " + str(name), "validation")
    decoded[name] = content

root.mkdir(mode=0o700, exist_ok=True)
os.chmod(root, 0o700)
tmux_started = False
try:
    runtime.mkdir(mode=0o700)
    os.chmod(runtime, 0o700)
    for name, mode in ALLOWED_ASSETS.items():
        write_private(runtime / name, decoded[name], mode)
    write_private(runtime / "log", b"", 0o600)
    started = subprocess.run(
        ["tmux", "new-session", "-d", "-s", run_id, "bash", str(runtime / "run.sh")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        raise RuntimeError(started.stderr.strip() or "tmux new-session failed")
    tmux_started = True
except Exception as exc:
    if not tmux_started:
        remove_runtime(runtime)
        fail("remote runtime install failed: " + str(exc), "install")
    emit(False, "start", "tmux start outcome is ambiguous: " + str(exc), True, None)
    raise SystemExit(2)

deadline = time.monotonic() + 5.0
status_path = runtime / "status.json"
owner_path = runtime / "owner.json"
pgid_path = runtime / "pgid"
status = None
while time.monotonic() < deadline:
    status = read_status(status_path)
    if status is not None and owner_path.is_file() and pgid_path.is_file():
        break
    time.sleep(0.05)
if status is None or not owner_path.is_file() or not pgid_path.is_file():
    alive = subprocess.run(
        ["tmux", "has-session", "-t", exact_tmux_target(run_id)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    if not alive:
        emit(False, "start", "tmux exited before producing complete runtime state", True, status)
        raise SystemExit(2)
    emit(False, "start", "tmux did not produce complete runtime state", True, status)
    raise SystemExit(2)
emit(True, "started", None, True, status)
'''


SITE_CUSTOMIZE_PROBE_SOURCE = r'''import importlib.abc
import importlib.util
import json
import site
import sys


class _NoOpLoader(importlib.abc.Loader):
    def create_module(self, _spec):
        return None

    def exec_module(self, _module):
        return None


class _StartupHookBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, _path, _target=None):
        if fullname in {"sitecustomize", "usercustomize"}:
            return importlib.util.spec_from_loader(fullname, _NoOpLoader())
        return None


blocker = _StartupHookBlocker()
sys.meta_path.insert(0, blocker)
try:
    site.main()
finally:
    sys.meta_path.remove(blocker)
    sys.modules.pop("sitecustomize", None)
    sys.modules.pop("usercustomize", None)
try:
    spec = importlib.util.find_spec("sitecustomize")
except (ImportError, ValueError):
    spec = None
origin = None if spec is None else (getattr(spec, "origin", None) or "<discoverable>")
print("RR_SITE_CUSTOMIZE_PROBE " + json.dumps(origin), flush=True)
'''


SET_PROCESS_TITLE_PROBE_SOURCE = r'''import os

os.environ["SPT_NOENV"] = "1"
import setproctitle

setproctitle.setproctitle("rr-privacy-preflight")
if setproctitle.getproctitle() != "rr-privacy-preflight":
    raise SystemExit("setproctitle did not change the process title")
'''


PROCESS_TITLE_HELPER_PROBE_SOURCE = r'''from pathlib import Path
import sitecustomize

print("RR_PROCESS_TITLE_HELPER " + str(Path(sitecustomize.__file__).resolve()), flush=True)
'''


def _privacy_bootstrap_source() -> str:
    allowed_assets = 'ALLOWED_ASSETS = {"run.sh": 0o700, "command.sh": 0o600}'
    source = REMOTE_BOOTSTRAP
    if source.count(allowed_assets) != 1:
        raise RuntimeError("remote bootstrap asset declaration is invalid")
    source = source.replace(
        allowed_assets,
        'ALLOWED_ASSETS = {"run.sh": 0o700, "command.sh": 0o600, '
        '"sitecustomize.py": 0o600}',
    )

    python_check = '[str(project_python), "-c", "import sys; print(sys.executable)"]'
    if source.count(python_check) != 1:
        raise RuntimeError("remote bootstrap Python check is invalid")
    source = source.replace(
        python_check,
        '[str(project_python), "-S", "-c", "import sys; print(sys.executable)"]',
    )

    insertion_point = "root.mkdir(mode=0o700, exist_ok=True)"
    if source.count(insertion_point) != 1:
        raise RuntimeError("remote bootstrap privacy insertion point is invalid")
    preflight = f'''if payload.get("process_title_privacy") != {{"mode": "required"}}:
    fail("invalid process-title privacy request", "validation")

privacy_environment = os.environ.copy()
privacy_environment.pop("RR_PROCESS_TITLE_REQUIRED", None)
privacy_environment.pop("RR_PROCESS_TITLE", None)

def run_privacy_probe(source, environment, description, python_flags=()):
    try:
        return subprocess.run(
            [str(project_python), *python_flags, "-c", source],
            cwd=workdir,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(description + ": " + str(exc))

site_probe = run_privacy_probe(
    {SITE_CUSTOMIZE_PROBE_SOURCE!r},
    privacy_environment,
    "could not inspect the configured Python for an existing sitecustomize",
    ("-S",),
)
site_lines = [
    line[len("RR_SITE_CUSTOMIZE_PROBE "):]
    for line in site_probe.stdout.splitlines()
    if line.startswith("RR_SITE_CUSTOMIZE_PROBE ")
]
if site_probe.returncode != 0 or not site_lines:
    fail("could not inspect the configured Python for an existing sitecustomize")
try:
    existing_sitecustomize = json.loads(site_lines[-1])
except json.JSONDecodeError:
    fail("configured Python returned an invalid sitecustomize probe result")
if existing_sitecustomize is not None:
    fail("process-title privacy conflicts with existing sitecustomize: " + str(existing_sitecustomize))

title_probe = run_privacy_probe(
    {SET_PROCESS_TITLE_PROBE_SOURCE!r},
    privacy_environment,
    "could not test setproctitle in the configured project Python",
)
if title_probe.returncode != 0:
    fail(
        "process-title privacy requires importable setproctitle in the configured "
        "project Python; install it in that environment before retrying"
    )

import tempfile
try:
    with tempfile.TemporaryDirectory(prefix="remote-runner-privacy-") as privacy_temp:
        privacy_dir = Path(privacy_temp)
        helper_path = privacy_dir / "sitecustomize.py"
        write_private(helper_path, decoded["sitecustomize.py"], 0o600)
        helper_environment = privacy_environment.copy()
        helper_environment["RR_PROCESS_TITLE_REQUIRED"] = "1"
        helper_environment["RR_PROCESS_TITLE"] = run_id
        original_pythonpath = helper_environment.get("PYTHONPATH")
        helper_environment["PYTHONPATH"] = str(privacy_dir) + (
            os.pathsep + original_pythonpath if original_pythonpath else ""
        )
        helper_probe = run_privacy_probe(
            {PROCESS_TITLE_HELPER_PROBE_SOURCE!r},
            helper_environment,
            "could not test the generated process-title privacy helper",
        )
        helper_lines = [
            line[len("RR_PROCESS_TITLE_HELPER "):]
            for line in helper_probe.stdout.splitlines()
            if line.startswith("RR_PROCESS_TITLE_HELPER ")
        ]
        if (
            helper_probe.returncode != 0
            or not helper_lines
            or helper_lines[-1] != str(helper_path.resolve())
        ):
            fail("generated process-title privacy helper failed its configured Python preflight")
except OSError as exc:
    fail("could not stage the generated process-title privacy helper: " + str(exc))

'''
    return source.replace(insertion_point, preflight + insertion_point)


def _read_frozen_command(path: Path, expected_hash: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"frozen command is not a regular file: {path}")
        if before.st_uid != os.getuid():
            raise ValueError(f"frozen command is not owned by current user: {path}")
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise ValueError(f"frozen command mode mismatch: {path}")
        if before.st_nlink != 1:
            raise ValueError(f"frozen command must have one link: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("frozen command changed while reading")
        content = b"".join(chunks)
    finally:
        os.close(fd)
    actual = sha256_bytes(content)
    if actual != expected_hash:
        raise ValueError(f"frozen command digest mismatch: {actual} != {expected_hash}")
    return content


def _bootstrap_stdin(payload: dict[str, Any], privacy_mode: str | None) -> bytes:
    encoded = base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    bootstrap_source = (
        REMOTE_BOOTSTRAP
        if privacy_mode is None
        else _privacy_bootstrap_source()
        if privacy_mode == PROCESS_TITLE_PRIVACY_MODE
        else None
    )
    if bootstrap_source is None:
        raise ValueError(f"unsupported privacy mode: {privacy_mode!r}")
    return (
        f"import base64, json\npayload=json.loads(base64.b64decode({encoded!r}))\n"
        + bootstrap_source
    ).encode()


def build_launch_plan(paths: ProjectPaths, run_id: str) -> LaunchPlan:
    manifest, _state = load_current_run(paths, run_id)
    privacy_mode = process_title_privacy_mode(manifest)
    if Path(str(manifest["project_config"])) != paths.config_path:
        raise ValueError("registered project config does not match the active project")
    if Path(str(manifest["registry_root"])) != paths.registry_root:
        raise ValueError("registered registry root does not match the active project")

    command_path = current_command_path(paths, run_id)
    command_bytes = _read_frozen_command(command_path, str(manifest["command_sha256"]))
    if command_bytes.decode("utf-8") != manifest["command"]:
        raise ValueError("frozen command text does not match the manifest")
    project_python = str(manifest["project_python"])
    output_root, output_relpath, output_path = validate_resolved_output(
        output_root=manifest.get("output_root"),
        output_relpath=manifest.get("output_relpath"),
        output_path=manifest.get("output_path"),
    )
    wrapper_bytes = _wrapper_source(
        run_id,
        str(manifest["label"]),
        str(manifest["remote_workdir"]),
        project_python,
        privacy_mode,
        str(manifest.get("workload_class", "standard")),
        output_root=output_root,
        output_path=output_path,
    ).encode()
    asset_list = [
        LaunchAsset("run.sh", wrapper_bytes, sha256_bytes(wrapper_bytes), 0o700),
        LaunchAsset("command.sh", command_bytes, sha256_bytes(command_bytes), 0o600),
    ]
    if privacy_mode == PROCESS_TITLE_PRIVACY_MODE:
        helper_bytes = _sitecustomize_source(run_id)
        asset_list.append(
            LaunchAsset(
                "sitecustomize.py",
                helper_bytes,
                sha256_bytes(helper_bytes),
                0o600,
            )
        )
    assets = tuple(asset_list)
    remote_runtime = runtime_path(run_id)
    tmux_session = run_tmux_session(run_id)
    payload = {
        "payload_schema_version": 1,
        "run_id": run_id,
        "runtime_path": remote_runtime,
        "tmux_session": tmux_session,
        "remote_workdir": manifest["remote_workdir"],
        "project_python": manifest["project_python"],
        "expected_revision": manifest["expected_revision"],
        "require_clean_worktree": manifest["require_clean_worktree"],
        "output_root": output_root,
        "output_relpath": output_relpath,
        "output_path": output_path,
        "assets": [
            {
                "name": asset.name,
                "mode": asset.mode,
                "sha256": asset.sha256,
                "data": base64.b64encode(asset.content).decode(),
            }
            for asset in assets
        ],
    }
    if privacy_mode == PROCESS_TITLE_PRIVACY_MODE:
        payload["process_title_privacy"] = {"mode": "required"}
    return LaunchPlan(
        run_id=run_id,
        ssh_target=str(manifest["ssh"]),
        runtime_path=remote_runtime,
        tmux_session=tmux_session,
        privacy_mode=privacy_mode,
        assets=assets,
        bootstrap_ssh_argv=(
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=8",
            str(manifest["ssh"]),
            remote_python_stdin_command(
                project_python,
                no_site=privacy_mode == PROCESS_TITLE_PRIVACY_MODE,
            ),
        ),
        bootstrap_stdin=_bootstrap_stdin(payload, privacy_mode),
    )
