from __future__ import annotations

import base64
import hashlib
import hmac
from pathlib import Path
import re
import shlex
from typing import Any


_SECTION_RE = re.compile(r"^\s*(host|match)\s+", re.IGNORECASE)


def _directive(line: str) -> list[str]:
    try:
        return shlex.split(line, comments=True)
    except ValueError as exc:
        raise ValueError(f"invalid SSH config line: {line.rstrip()}") from exc


def remove_exact_host_block(text: str, alias: str) -> tuple[str, dict[str, Any]]:
    lines = text.splitlines(keepends=True)
    matches: list[tuple[int, int, list[str]]] = []
    for index, line in enumerate(lines):
        tokens = _directive(line)
        if not tokens or tokens[0].lower() != "host" or tokens[1:] != [alias]:
            continue
        end = index + 1
        while end < len(lines) and _SECTION_RE.match(lines[end]) is None:
            end += 1
        matches.append((index, end, lines[index:end]))
    if len(matches) > 1:
        raise ValueError(
            f"SSH config contains duplicate exact Host blocks for {alias!r}"
        )
    if not matches:
        return text, {
            "alias": alias,
            "action": "already_absent",
            "hostnames": [alias],
            "ports": [],
            "identity_files": [],
        }

    start, end, block = matches[0]
    hostnames = [alias]
    ports: list[str] = []
    identity_files: list[str] = []
    for line in block[1:]:
        tokens = _directive(line)
        if len(tokens) < 2:
            continue
        directive = tokens[0].lower()
        if directive == "hostname":
            hostnames.append(tokens[1])
        elif directive == "port":
            ports.append(tokens[1])
        elif directive == "identityfile":
            identity_files.append(tokens[1])
    proposed = "".join(lines[:start] + lines[end:])
    proposed = re.sub(r"\n{3,}", "\n\n", proposed)
    return proposed, {
        "alias": alias,
        "action": "remove",
        "hostnames": sorted(set(hostnames)),
        "ports": sorted(set(ports)),
        "identity_files": identity_files,
    }


def known_host_candidates(hostnames: list[str], ports: list[str]) -> set[str]:
    candidates = set(hostnames)
    for hostname in hostnames:
        for port in ports:
            candidates.add(f"[{hostname}]:{port}")
    return candidates


def _hashed_host_matches(token: str, candidates: set[str]) -> bool:
    if not token.startswith("|1|"):
        return False
    try:
        _empty, version, salt_text, digest_text = token.split("|", 3)
        if version != "1":
            return False
        salt = base64.b64decode(salt_text, validate=True)
        expected = base64.b64decode(digest_text, validate=True)
    except (ValueError, TypeError):
        return False
    return any(
        hmac.compare_digest(
            hmac.new(salt, candidate.encode(), hashlib.sha1).digest(), expected
        )
        for candidate in candidates
    )


def remove_known_hosts(
    text: str,
    candidates: set[str],
) -> tuple[str, dict[str, Any]]:
    kept: list[str] = []
    removed = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        fields = stripped.split()
        host_index = 1 if fields[0].startswith("@") else 0
        if len(fields) <= host_index:
            kept.append(line)
            continue
        host_field = fields[host_index]
        tokens = host_field.split(",")
        retained_tokens = [
            token
            for token in tokens
            if token not in candidates and not _hashed_host_matches(token, candidates)
        ]
        if len(retained_tokens) == len(tokens):
            kept.append(line)
            continue
        removed += len(tokens) - len(retained_tokens)
        if retained_tokens:
            fields[host_index] = ",".join(retained_tokens)
            newline = "\n" if line.endswith("\n") else ""
            kept.append(" ".join(fields) + newline)
    return "".join(kept), {
        "action": "remove" if removed else "already_absent",
        "removed_host_tokens": removed,
        "candidates": sorted(candidates),
    }


def inspect_local_ssh_cleanup(
    ssh_config: Path,
    known_hosts: Path,
    alias: str,
) -> dict[str, Any]:
    config_text = ssh_config.read_text(encoding="utf-8") if ssh_config.is_file() else ""
    proposed_config, block = remove_exact_host_block(config_text, alias)
    candidates = known_host_candidates(block["hostnames"], block["ports"])
    known_text = (
        known_hosts.read_text(encoding="utf-8") if known_hosts.is_file() else ""
    )
    proposed_known, known = remove_known_hosts(known_text, candidates)
    return {
        "ssh_config": str(ssh_config),
        "known_hosts": str(known_hosts),
        "block": block,
        "known_host_records": known,
        "original_config": config_text,
        "proposed_config": proposed_config,
        "original_known_hosts": known_text,
        "proposed_known_hosts": proposed_known,
    }
