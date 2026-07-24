from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


def _path_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{field} contains invalid control characters")
    if "$" in value or "`" in value or value.startswith("~"):
        raise ValueError(f"{field} must be literal and cannot use shell expansion")
    return value


def normalize_absolute_output_path(value: Any, field: str = "output_path") -> str:
    text = _path_text(value, field)
    path = PurePosixPath(text)
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute POSIX path")
    if str(path) != text:
        raise ValueError(f"{field} must be a normalized POSIX path")
    return text


def normalize_output_root(value: Any, field: str = "output_root") -> str | None:
    if value is None:
        return None
    return normalize_absolute_output_path(value, field)


def normalize_output_relpath(value: Any, field: str = "--output-relpath") -> str:
    text = _path_text(value, field)
    path = PurePosixPath(text)
    if path.is_absolute():
        raise ValueError(f"{field} must be a relative POSIX path")
    if str(path) == "." or any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"{field} cannot contain dot or parent traversal components")
    if str(path) != text:
        raise ValueError(f"{field} must be a normalized POSIX path")
    return text


def resolve_output_path(output_root: Any, output_relpath: Any) -> str:
    root_text = normalize_output_root(output_root)
    if root_text is None:
        raise ValueError("selected server has no configured output_root")
    relpath_text = normalize_output_relpath(output_relpath)
    root = PurePosixPath(root_text)
    resolved = root / PurePosixPath(relpath_text)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("resolved output path escapes output_root") from exc
    return str(resolved)


def validate_resolved_output(
    *,
    output_root: Any,
    output_relpath: Any,
    output_path: Any,
) -> tuple[str | None, str | None, str | None]:
    if output_relpath is None:
        if output_root is not None:
            raise ValueError("output_root requires output_relpath")
        normalized_path = (
            None if output_path is None else normalize_absolute_output_path(output_path)
        )
        return None, None, normalized_path

    normalized_root = normalize_output_root(output_root)
    normalized_relpath = normalize_output_relpath(output_relpath)
    resolved_path = resolve_output_path(normalized_root, normalized_relpath)
    if output_path != resolved_path:
        raise ValueError(
            "resolved output_path does not match output_root/output_relpath"
        )
    return normalized_root, normalized_relpath, resolved_path
