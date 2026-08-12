from __future__ import annotations

import re
from typing import Any


MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MACHINE_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MACHINE_ID_SOURCES = {"explicit", "legacy-name"}


MACHINE_IDENTITY_PROBE_PROGRAM = r'''import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


def identity_material():
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return "machine-id:" + value

    if sys.platform == "darwin":
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
                return "ioplatformuuid:" + match.group(1)

    raise SystemExit("stable machine identity is unavailable")


material = identity_material().encode("utf-8")
print(json.dumps({"machine_fingerprint": "sha256:" + hashlib.sha256(material).hexdigest()}))
'''


def normalize_machine_id(value: object, *, server_name: str) -> tuple[str, str]:
    if not isinstance(server_name, str) or MACHINE_ID_RE.fullmatch(server_name) is None:
        raise ValueError(f"invalid server name for machine identity: {server_name!r}")
    if value is None:
        return server_name, "legacy-name"
    if not isinstance(value, str) or MACHINE_ID_RE.fullmatch(value) is None:
        raise ValueError(
            f"machine_id for server {server_name!r} must start with an alphanumeric "
            "character and contain only letters, digits, dots, underscores, or hyphens"
        )
    return value, "explicit"


def normalize_machine_fingerprint(
    value: object,
    *,
    required: bool = False,
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or MACHINE_FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError("machine fingerprint must be a sha256 value")
    return value


def normalize_server_identity(server: dict[str, Any]) -> dict[str, Any]:
    name = server.get("name")
    if not isinstance(name, str):
        raise ValueError("server identity requires a server name")
    machine_id, inferred_source = normalize_machine_id(
        server.get("machine_id"),
        server_name=name,
    )
    source = server.get("machine_id_source", inferred_source)
    if source not in MACHINE_ID_SOURCES:
        raise ValueError(f"machine identity source for {name!r} is invalid")
    if source == "legacy-name" and machine_id != name:
        raise ValueError(
            f"legacy machine identity for {name!r} must equal the server name"
        )
    if source == "explicit" and server.get("machine_id") is None:
        raise ValueError(f"explicit machine identity for {name!r} is missing machine_id")
    fingerprint = normalize_machine_fingerprint(server.get("machine_fingerprint"))
    return {
        **server,
        "machine_id": machine_id,
        "machine_id_source": source,
        "machine_fingerprint": fingerprint,
    }
