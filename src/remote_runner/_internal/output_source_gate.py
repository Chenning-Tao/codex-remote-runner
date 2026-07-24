from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
from pathlib import Path
from typing import NoReturn


REMOTE_KIND_PROGRAM = r"""
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.is_symlink():
    kind = "symlink"
elif path.is_dir():
    kind = "directory"
elif path.is_file():
    kind = "file"
else:
    kind = "missing"
print(json.dumps({"kind": kind}, separators=(",", ":")))
"""
IDENTITY_PROBE_COMMAND = "remote-runner-output-identity"
IDENTITY_FILENAMES = frozenset({"config.json", "manifest.json"})


class GateError(ValueError):
    pass


def _resolved_root(raw_root: str) -> Path:
    root = Path(raw_root)
    if not root.is_absolute():
        raise GateError("restricted root must be absolute")
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise GateError("restricted root must be a directory")
    return resolved


def _allowed_path(raw_path: str, root: Path) -> Path:
    path = Path(raw_path)
    if not path.is_absolute() or ".." in path.parts:
        raise GateError("source path must be normalized and absolute")
    resolved = path.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise GateError("source path is outside the restricted root")
    return path


def _source_kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "missing"


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _handle_probe(
    argv: list[str], root: Path
) -> dict[str, bool | str | None] | None:
    if len(argv) != 4 or argv[:2] != ["python3", "-c"]:
        return None
    if argv[2] != REMOTE_KIND_PROGRAM:
        raise GateError("unrecognized Python probe")
    path = _allowed_path(argv[3], root)
    return {"kind": _source_kind(path)}


def _handle_identity_probe(
    argv: list[str], root: Path
) -> dict[str, bool | str | None] | None:
    if not argv or argv[0] != IDENTITY_PROBE_COMMAND:
        return None
    if len(argv) != 3:
        raise GateError("identity probe requires a root and identity filename")
    identity_name = argv[2]
    if identity_name not in IDENTITY_FILENAMES:
        raise GateError("identity probe filename is not allowed")
    source_root = _allowed_path(argv[1], root)
    if not source_root.is_dir():
        raise GateError("identity probe root must be a directory")
    complete = _allowed_path(str(source_root / "COMPLETE"), root)
    summary = _allowed_path(str(source_root / "summary.json"), root)
    identity = _allowed_path(str(source_root / identity_name), root)
    return {
        "root_exists": True,
        "complete_exists": complete.is_file(),
        "summary_sha256": _sha256_file(summary),
        "identity_sha256": _sha256_file(identity),
    }


def _validate_rsync_sender(argv: list[str], root: Path) -> None:
    if len(argv) < 6 or argv[:3] != ["rsync", "--server", "--sender"]:
        raise GateError("only an rsync sender command is allowed")
    try:
        separator = argv.index(".", 3)
    except ValueError as exc:
        raise GateError("rsync sender command has no path separator") from exc
    paths = argv[separator + 1 :]
    if len(paths) != 1:
        raise GateError("rsync sender must request exactly one source path")
    _allowed_path(paths[0].rstrip("/") or "/", root)


def _exec_rrsync(rrsync: str, original_command: str) -> NoReturn:
    executable = Path(rrsync)
    if not executable.is_absolute() or not executable.is_file():
        raise GateError("rrsync executable is unavailable")
    environment = dict(os.environ)
    environment["SSH_ORIGINAL_COMMAND"] = original_command
    os.execve(str(executable), (str(executable), "-ro", "/"), environment)


def dispatch(
    original_command: str, *, root: Path, rrsync: str
) -> dict[str, bool | str | None]:
    try:
        argv = shlex.split(original_command, posix=True)
    except ValueError as exc:
        raise GateError("invalid SSH_ORIGINAL_COMMAND quoting") from exc
    probe = _handle_probe(argv, root)
    if probe is not None:
        return probe
    identity_probe = _handle_identity_probe(argv, root)
    if identity_probe is not None:
        return identity_probe
    _validate_rsync_sender(argv, root)
    _exec_rrsync(rrsync, original_command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restrict output-sync source access.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--rrsync", default="/usr/bin/rrsync")
    args = parser.parse_args(argv)
    try:
        root = _resolved_root(args.root)
        original_command = os.environ.get("SSH_ORIGINAL_COMMAND")
        if not original_command:
            raise GateError("SSH_ORIGINAL_COMMAND is required")
        result = dispatch(original_command, root=root, rrsync=args.rrsync)
    except (GateError, OSError) as exc:
        print(f"output source gate denied access: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
