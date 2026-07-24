from __future__ import annotations

import shlex
from dataclasses import dataclass


QUEUE_PRIORITIES = ("normal", "urgent")
_QUEUE_PRIORITY_RANK = {name: rank for rank, name in enumerate(QUEUE_PRIORITIES)}
WORKLOAD_CLASSES = ("standard", "test")


def normalize_minimum_cores(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("minimum cores must be a positive integer")
    return value


def normalize_queue_priority(value: object) -> str:
    if not isinstance(value, str) or value not in _QUEUE_PRIORITY_RANK:
        choices = ", ".join(QUEUE_PRIORITIES)
        raise ValueError(f"queue priority must be one of: {choices}")
    return value


def queue_priority_rank(value: object) -> int:
    return _QUEUE_PRIORITY_RANK[normalize_queue_priority(value)]


def normalize_workload_class(value: object) -> str:
    if not isinstance(value, str) or value not in WORKLOAD_CLASSES:
        choices = ", ".join(WORKLOAD_CLASSES)
        raise ValueError(f"workload class must be one of: {choices}")
    return value


@dataclass(frozen=True)
class CapacityCandidate:
    name: str
    configured_cores: int
    load5: float
    priority: int = 0
    active_run_count: int = 0

    @property
    def available_cores(self) -> float:
        if self.active_run_count:
            return 0.0
        return max(float(self.configured_cores) - self.load5, 0.0)


def rank_candidates(candidates: list[CapacityCandidate]) -> list[CapacityCandidate]:
    if not candidates:
        raise ValueError("at least one capacity candidate is required")
    for candidate in candidates:
        if candidate.configured_cores <= 0:
            raise ValueError(f"configured cores for {candidate.name!r} must be positive")
        if candidate.load5 < 0:
            raise ValueError(f"load5 for {candidate.name!r} must be non-negative")
        if candidate.active_run_count < 0:
            raise ValueError(f"active run count for {candidate.name!r} must be non-negative")
    return sorted(
        candidates,
        key=lambda item: (
            -item.available_cores,
            -item.configured_cores,
            -item.priority,
            item.name,
        ),
    )


def select_candidate(candidates: list[CapacityCandidate]) -> CapacityCandidate:
    return rank_candidates(candidates)[0]


def should_queue(candidates: list[CapacityCandidate], *, active_runner_work: bool) -> bool:
    if not candidates:
        raise ValueError("at least one capacity candidate is required")
    return active_runner_work and all(candidate.available_cores <= 0 for candidate in candidates)


def _worker_value(tokens: list[str], worker_arg: str) -> int | None:
    prefix = worker_arg + "="
    found: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == worker_arg:
            if index + 1 >= len(tokens):
                raise ValueError(f"worker argument {worker_arg!r} has no value")
            found.append(tokens[index + 1])
            index += 2
            continue
        if token.startswith(prefix):
            found.append(token[len(prefix) :])
        index += 1
    if not found:
        return None
    if len(found) != 1:
        raise ValueError(f"worker argument {worker_arg!r} appears more than once")
    try:
        value = int(found[0])
    except ValueError as exc:
        raise ValueError(f"worker argument {worker_arg!r} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"worker argument {worker_arg!r} must be positive")
    return value


def resolve_worker_command(
    command: str,
    *,
    worker_arg: str,
    configured_cores: int,
) -> tuple[str, int, bool]:
    if not command.strip() or "\x00" in command:
        raise ValueError("command must be non-empty shell text without NUL bytes")
    if not worker_arg.startswith("--") or any(char.isspace() for char in worker_arg):
        raise ValueError("worker argument must be one long option without whitespace")
    if configured_cores <= 0:
        raise ValueError("configured cores must be positive")
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError(f"command cannot be parsed for worker resolution: {exc}") from exc
    explicit = _worker_value(tokens, worker_arg)
    if explicit is not None:
        return command, explicit, False
    if "\n" in command or "\r" in command:
        raise ValueError(
            "automatic worker resolution requires a single-line command; "
            "supply the worker argument explicitly for shell scripts"
        )
    resolved = f"{command.rstrip()} {shlex.quote(worker_arg)} {configured_cores}"
    return resolved, configured_cores, True
