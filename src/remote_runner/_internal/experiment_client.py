from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import resolve_project_config
from .experiment_contracts import MAX_CONTRACT_BYTES


def _read_document(path: Path, noun: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if path.expanduser().is_symlink() or not resolved.is_file():
        raise ValueError(f"{noun} must be a regular JSON file: {path}")
    if resolved.stat().st_size > MAX_CONTRACT_BYTES:
        raise ValueError(f"{noun} exceeds the {MAX_CONTRACT_BYTES}-byte limit")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{noun} must contain one UTF-8 JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{noun} must contain one JSON object")
    return value


def _call(
    args: argparse.Namespace,
    action: str,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    return call_controller(
        config,
        action,
        timeout=args.timeout,
        payload=payload,
    )


def request_query(
    args: argparse.Namespace,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return _call(args, "experiment-query", payload=payload)


def execute(args: argparse.Namespace) -> dict[str, Any]:
    command = args.experiment_command
    if command == "query":
        return request_query(
            args,
            _read_document(args.file, "experiment query"),
        )
    if command == "plan-preview":
        return _call(
            args,
            "experiment-plan-preview",
            payload=_read_document(args.file, "experiment plan"),
        )
    if command == "plan-publish":
        return _call(
            args,
            "experiment-plan-publish",
            payload={
                "plan": _read_document(args.file, "experiment plan"),
                "request_id": args.request_id,
                "expected_impact_digest": args.impact_digest,
            },
        )
    if command == "binding-ingest":
        return _call(
            args,
            "experiment-binding-ingest",
            payload=_read_document(args.file, "run binding"),
        )
    if command == "result-ingest":
        return _call(
            args,
            "experiment-result-ingest",
            payload=_read_document(args.file, "experiment result"),
        )
    if command == "acceptance-record":
        return _call(
            args,
            "experiment-acceptance",
            payload=_read_document(args.file, "acceptance request"),
        )
    if command == "registry-rebuild":
        return _call(args, "experiment-registry-rebuild")
    raise AssertionError(f"unhandled experiment command: {command}")
