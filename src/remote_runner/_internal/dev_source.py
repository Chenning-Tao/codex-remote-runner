from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol


STRUCTURAL_COMPONENTS = {
    ".git",
    ".hg",
    ".svn",
    ".jj",
    ".remote-runner",
    ".trellis",
    ".agents",
    ".claude",
    ".codex",
    ".worktree",
    ".worktrees",
}
REPOSITORY_MARKERS = (".git", ".hg", ".svn", ".jj")

DEFAULT_EXCLUDES = (
    ".venv/",
    "venv/",
    ".tox/",
    ".nox/",
    "node_modules/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".cache/",
    ".ppt-build/",
    "build/",
    "dist/",
    "target/",
    "out/",
    "outputs/",
    "results/",
    ".env",
    ".env.*",
    ".ssh/",
    ".aws/",
    ".gnupg/",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "service-account*.json",
    ".DS_Store",
    "*.pyc",
    "*.pyo",
)


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path,
    ) -> subprocess.CompletedProcess[bytes]: ...


@dataclass(frozen=True)
class DevSourcePlan:
    source_root: Path
    mode: str
    files: tuple[str, ...]
    total_bytes: int

    def files_from_bytes(self) -> bytes:
        return b"".join(os.fsencode(path) + b"\0" for path in self.files)


def _run(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _normalized_relative_path(raw: bytes, *, source_root: Path) -> str:
    value = os.fsdecode(raw)
    if not value or "\x00" in value:
        raise ValueError("development source contains an invalid empty path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"development source returned an unsafe path: {value!r}")
    candidate = source_root.joinpath(*path.parts)
    try:
        candidate.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(f"development source path escapes its root: {value!r}") from exc
    return path.as_posix()


def _matches(path: str, pattern: str) -> bool:
    directory_pattern = pattern.endswith("/")
    normalized = pattern.rstrip("/")
    parts = PurePosixPath(path).parts
    if "/" not in normalized:
        if directory_pattern:
            return normalized in parts[:-1]
        return any(fnmatch.fnmatchcase(part, normalized) for part in parts)
    if directory_pattern:
        return path == normalized or path.startswith(normalized + "/")
    return PurePosixPath("/" + path).match("/" + normalized)


def _structurally_excluded(path: str) -> bool:
    return any(part in STRUCTURAL_COMPONENTS for part in PurePosixPath(path).parts)


def _inside_nested_repository(path: Path, *, source_root: Path) -> bool:
    current = path.parent
    while current != source_root:
        if any((current / marker).exists() for marker in REPOSITORY_MARKERS):
            return True
        if source_root not in current.parents:
            return True
        current = current.parent
    return False


def _safe_symlink(path: Path, *, source_root: Path) -> bool:
    try:
        resolved_target = path.resolve(strict=True)
        resolved_target.relative_to(source_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return False
    return True


def _resolved_inside_source(path: Path, *, source_root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(source_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return False
    return True


def _has_git_marker(source_root: Path) -> bool:
    current = source_root
    while True:
        try:
            (current / ".git").lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(
                f"could not inspect Git marker above development source: {exc}"
            ) from exc
        else:
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _selected(path: str, *, include: tuple[str, ...], exclude: tuple[str, ...]) -> bool:
    if _structurally_excluded(path):
        return False
    if any(_matches(path, pattern) for pattern in exclude):
        return False
    if any(_matches(path, pattern) for pattern in include):
        return True
    return not any(_matches(path, pattern) for pattern in DEFAULT_EXCLUDES)


def _git_files(
    source_root: Path,
    *,
    runner: CommandRunner,
) -> tuple[str, ...] | None:
    probe = runner(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=source_root,
    )
    if probe.returncode != 0:
        if _has_git_marker(source_root):
            detail = probe.stderr.decode(errors="replace").strip()
            raise RuntimeError(detail or "could not inspect Git development source")
        return None
    if probe.stdout.strip() != b"true":
        return None

    listed = runner(
        ["git", "ls-files", "-co", "--exclude-standard", "-z", "--", "."],
        cwd=source_root,
    )
    if listed.returncode != 0:
        detail = listed.stderr.decode(errors="replace").strip()
        raise RuntimeError(detail or "could not enumerate Git development source")
    values: list[str] = []
    for raw in listed.stdout.split(b"\0"):
        if not raw:
            continue
        relative = _normalized_relative_path(raw, source_root=source_root)
        values.append(relative)
    return tuple(dict.fromkeys(values))


def _could_include_descendant(directory: str, include: tuple[str, ...]) -> bool:
    directory_parts = PurePosixPath(directory).parts
    for pattern in include:
        normalized = pattern.rstrip("/")
        pattern_parts = PurePosixPath(normalized).parts
        static_parts: list[str] = []
        for part in pattern_parts:
            if any(character in part for character in "*?["):
                break
            static_parts.append(part)
        if not static_parts:
            return True
        common = min(len(directory_parts), len(static_parts))
        if directory_parts[:common] == tuple(static_parts[:common]):
            return True
    return False


def _matches_directory_or_descendant(directory: str, pattern: str) -> bool:
    return _matches(directory + "/.remote-runner-dev-descendant", pattern)


def _walk_files(
    source_root: Path,
    *,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
) -> tuple[str, ...]:
    values: list[str] = []
    for directory, dirnames, filenames in os.walk(source_root, followlinks=False):
        current = Path(directory)
        relative_dir = current.relative_to(source_root)
        retained: list[str] = []
        for name in sorted(dirnames):
            path = current / name
            relative = (relative_dir / name).as_posix().removeprefix("./")
            if _structurally_excluded(relative):
                continue
            if path.is_symlink():
                if _safe_symlink(path, source_root=source_root):
                    values.append(relative)
                continue
            if any((path / marker).exists() for marker in REPOSITORY_MARKERS):
                continue
            if any(
                _matches_directory_or_descendant(relative, pattern)
                for pattern in exclude
            ):
                continue
            ordinarily_excluded = any(
                _matches_directory_or_descendant(relative, pattern)
                for pattern in DEFAULT_EXCLUDES
            )
            if ordinarily_excluded and not _could_include_descendant(relative, include):
                continue
            retained.append(name)
        dirnames[:] = retained
        for name in sorted(filenames):
            path = current / name
            if not path.is_symlink() and not path.is_file():
                continue
            relative = (relative_dir / name).as_posix()
            values.append(relative.removeprefix("./"))
    return tuple(values)


def _expand_explicit_includes(source_root: Path, patterns: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for pattern in patterns:
        normalized = pattern.rstrip("/")
        exact_file = not any(character in normalized for character in "*?[")
        matches = (
            source_root.rglob(normalized)
            if "/" not in normalized
            else source_root.glob(normalized)
        )
        for path in matches:
            if path.is_symlink():
                candidates = (path,)
            else:
                candidates = path.rglob("*") if path.is_dir() else (path,)
            for candidate in candidates:
                if not candidate.is_symlink() and not candidate.is_file():
                    continue
                if candidate.is_symlink() and not _safe_symlink(
                    candidate, source_root=source_root
                ):
                    continue
                if not candidate.is_symlink() and not _resolved_inside_source(
                    candidate, source_root=source_root
                ):
                    continue
                relative = candidate.relative_to(source_root).as_posix()
                exact_relative_file = exact_file and relative == normalized
                if (
                    not exact_relative_file
                    and _inside_nested_repository(candidate, source_root=source_root)
                ):
                    continue
                if not _structurally_excluded(relative):
                    values.append(relative)
    return tuple(values)


def build_source_plan(
    source_root: Path,
    *,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    runner: CommandRunner = _run,
) -> DevSourcePlan:
    root = source_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"development source root does not exist: {root}")
    git_files = _git_files(root, runner=runner)
    mode = "git-aware" if git_files is not None else "filtered-walk"
    candidates = list(
        git_files
        if git_files is not None
        else _walk_files(root, include=include, exclude=exclude)
    )
    candidates.extend(_expand_explicit_includes(root, include))

    selected: list[str] = []
    total_bytes = 0
    for relative in sorted(dict.fromkeys(candidates)):
        if not _selected(relative, include=include, exclude=exclude):
            continue
        path = root.joinpath(*PurePosixPath(relative).parts)
        if not path.is_symlink() and not path.is_file():
            continue
        if path.is_symlink() and not _safe_symlink(path, source_root=root):
            continue
        if not path.is_symlink() and not _resolved_inside_source(
            path, source_root=root
        ):
            continue
        selected.append(relative)
        total_bytes += path.lstat().st_size
    if not selected:
        raise ValueError("development source selection is empty")
    return DevSourcePlan(
        source_root=root,
        mode=mode,
        files=tuple(selected),
        total_bytes=total_bytes,
    )
