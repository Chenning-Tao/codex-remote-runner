from __future__ import annotations

import base64
import json
import shlex
import subprocess
from typing import Any

from .output_sync import OutputSyncConfig


PAYLOAD_ENV = "REMOTE_RUNNER_RETIREMENT_PAYLOAD"
ARCHIVE_CLEANUP_PROGRAM = r"""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import tempfile

payload = json.loads(base64.urlsafe_b64decode(os.environ["REMOTE_RUNNER_RETIREMENT_PAYLOAD"]))
config_text = payload.get("source_ssh_config")
alias = payload.get("source_host")
apply = payload.get("apply")
expected_digest = payload.get("expected_digest")
if not isinstance(config_text, str) or not PurePosixPath(config_text).is_absolute():
    raise ValueError("source_ssh_config must be an absolute path")
if not isinstance(alias, str) or re.fullmatch(r"[A-Za-z0-9._-]+", alias) is None:
    raise ValueError("source_host is invalid")
if not isinstance(apply, bool):
    raise ValueError("apply must be boolean")
if expected_digest is not None and (not isinstance(expected_digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest) is None):
    raise ValueError("expected_digest is invalid")

config_path = Path(config_text)
if config_path.is_symlink():
    raise ValueError("archive source SSH config is a symlink")
text = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
lines = text.splitlines(keepends=True)
sections = []
for index, line in enumerate(lines):
    try:
        tokens = shlex.split(line, comments=True)
    except ValueError as exc:
        raise ValueError("invalid archive SSH config") from exc
    if tokens and tokens[0].lower() in {"host", "match"}:
        end = index + 1
        while end < len(lines):
            next_tokens = shlex.split(lines[end], comments=True)
            if next_tokens and next_tokens[0].lower() in {"host", "match"}:
                break
            end += 1
        sections.append((index, end, tokens, lines[index:end]))
matches = [section for section in sections if section[2][0].lower() == "host" and section[2][1:] == [alias]]
if len(matches) > 1:
    raise ValueError("duplicate exact archive source Host blocks")

hostnames = [alias]
ports = []
identity_values = []
known_hosts_values = []
if matches:
    start, end, _tokens, block = matches[0]
    for line in block[1:]:
        tokens = shlex.split(line, comments=True)
        if len(tokens) < 2:
            continue
        directive = tokens[0].lower()
        if directive == "hostname":
            hostnames.append(tokens[1])
        elif directive == "port":
            ports.append(tokens[1])
        elif directive == "identityfile":
            identity_values.extend(tokens[1:])
        elif directive == "userknownhostsfile":
            known_hosts_values.extend(tokens[1:])
    proposed = "".join(lines[:start] + lines[end:])
    proposed = re.sub(r"\n{3,}", "\n\n", proposed)
else:
    proposed = text

identity_references = {}
for line in lines:
    tokens = shlex.split(line, comments=True)
    if len(tokens) >= 2 and tokens[0].lower() == "identityfile":
        for value in tokens[1:]:
            identity_references[value] = identity_references.get(value, 0) + 1
identities = []
public_keys = []
for value in identity_values:
    path = Path(value).expanduser()
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError("archive IdentityFile must resolve to an absolute path")
    exclusive = identity_references.get(value, 0) == 1
    public_path = Path(str(path) + ".pub")
    public_key = public_path.read_text(encoding="utf-8").strip() if public_path.is_file() else None
    if public_key:
        fields = public_key.split()
        if len(fields) < 2:
            raise ValueError("archive public key is invalid")
        if exclusive:
            public_keys.append(" ".join(fields[:2]))
    identities.append({
        "path": str(path),
        "exclusive": exclusive,
        "exists": path.is_file(),
        "public_key_path": str(public_path),
        "public_key_exists": public_path.is_file(),
    })

if known_hosts_values:
    known_paths = []
    for value in known_hosts_values:
        if value.lower() == "none":
            continue
        path = Path(value).expanduser()
        if not path.is_absolute() or "%" in value:
            raise ValueError("archive UserKnownHostsFile is not a fixed absolute path")
        known_paths.append(path)
else:
    known_paths = [Path.home() / ".ssh" / "known_hosts"]
candidates = set(hostnames)
for hostname in hostnames:
    for port in ports:
        candidates.add("[" + hostname + "]:" + port)

def hashed_match(token, values):
    if not token.startswith("|1|"):
        return False
    try:
        _empty, version, salt_text, digest_text = token.split("|", 3)
        if version != "1":
            return False
        salt = base64.b64decode(salt_text, validate=True)
        expected = base64.b64decode(digest_text, validate=True)
    except Exception:
        return False
    return any(hmac.compare_digest(hmac.new(salt, value.encode(), hashlib.sha1).digest(), expected) for value in values)

def filtered_known_hosts(path):
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    kept = []
    removed = 0
    for line in original.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        fields = stripped.split()
        host_index = 1 if fields[0].startswith("@") else 0
        tokens = fields[host_index].split(",")
        retained = [token for token in tokens if token not in candidates and not hashed_match(token, candidates)]
        if len(retained) == len(tokens):
            kept.append(line)
        else:
            removed += len(tokens) - len(retained)
            if retained:
                fields[host_index] = ",".join(retained)
                kept.append(" ".join(fields) + ("\n" if line.endswith("\n") else ""))
    return original, "".join(kept), removed

known_results = []
known_updates = []
for path in known_paths:
    if path.is_symlink():
        raise ValueError("archive known_hosts path is a symlink")
    original, filtered, removed = filtered_known_hosts(path)
    known_results.append({"path": str(path), "removed_host_tokens": removed})
    known_updates.append((path, original, filtered))

state_digest = "sha256:" + hashlib.sha256(json.dumps({
    "config": text,
    "identities": identities,
    "public_keys": public_keys,
    "known_hosts": [{"path": str(path), "content": original} for path, original, _filtered in known_updates],
}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
if apply and expected_digest != state_digest:
    raise ValueError("archive retirement state changed after inspection")

def replace(path, content):
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_text = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(temporary_text)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()

if apply:
    if matches:
        replace(config_path, proposed)
    for identity in identities:
        if not identity["exclusive"]:
            continue
        for path_text in (identity["path"], identity["public_key_path"]):
            path = Path(path_text)
            if path.is_file():
                path.unlink()
            elif path.exists():
                raise ValueError("archive identity path is not a regular file")
    for path, original, filtered in known_updates:
        if original != filtered:
            replace(path, filtered)

print(json.dumps({
    "ok": True,
    "applied": apply,
    "source_host": alias,
    "host_block": "remove" if matches else "already_absent",
    "hostnames": sorted(set(hostnames)),
    "identities": identities,
    "public_keys": public_keys,
    "known_hosts": known_results,
    "state_digest": state_digest,
}, sort_keys=True))
"""


def inspect_or_apply_archive_source(
    config: OutputSyncConfig,
    source_server: str,
    *,
    apply: bool,
    timeout: int,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    source_host = config.source_hosts.get(source_server)
    if source_host is None:
        return {
            "applied": apply,
            "source_server": source_server,
            "status": "not_configured",
            "public_keys": [],
        }
    payload = {
        "source_ssh_config": config.source_ssh_config,
        "source_host": source_host,
        "apply": apply,
        "expected_digest": expected_digest,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    remote_command = " ".join(
        (
            f"{PAYLOAD_ENV}={shlex.quote(encoded)}",
            shlex.quote(config.target_python),
            "-",
        )
    )
    try:
        completed = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={timeout}",
                config.target_ssh,
                remote_command,
            ],
            input=ARCHIVE_CLEANUP_PROGRAM,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout + 30,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"archive retirement inspection timed out after {exc.timeout}s"
        ) from exc
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "archive retirement inspection returned invalid JSON"
        ) from exc
    if (
        completed.returncode != 0
        or not isinstance(result, dict)
        or result.get("ok") is not True
    ):
        raise RuntimeError(
            completed.stderr.strip() or "archive retirement inspection failed"
        )
    return {"source_server": source_server, "status": "inspected", **result}
