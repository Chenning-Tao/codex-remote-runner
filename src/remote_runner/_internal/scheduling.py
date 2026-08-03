from __future__ import annotations

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
