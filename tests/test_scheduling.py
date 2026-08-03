from __future__ import annotations

import pytest

from remote_runner._internal.scheduling import (
    CapacityCandidate,
    normalize_minimum_cores,
    rank_candidates,
    select_candidate,
    should_queue,
)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "256"])
def test_minimum_cores_must_be_a_positive_integer(value: object) -> None:
    with pytest.raises(ValueError, match="minimum cores must be a positive integer"):
        normalize_minimum_cores(value)


def test_minimum_cores_preserves_valid_requirement() -> None:
    assert normalize_minimum_cores(256) == 256


def test_absolute_headroom_prefers_partly_loaded_large_server() -> None:
    candidates = [
        CapacityCandidate("compute-b", configured_cores=256, load5=128),
        CapacityCandidate("archive", configured_cores=32, load5=0),
    ]

    assert select_candidate(candidates).name == "compute-b"
    assert [item.name for item in rank_candidates(candidates)] == ["compute-b", "archive"]


def test_active_runner_work_reserves_candidate_before_load5_rises() -> None:
    candidates = [
        CapacityCandidate("compute-b", configured_cores=256, load5=0, active_run_count=1),
        CapacityCandidate("archive", configured_cores=32, load5=0),
    ]

    assert candidates[0].available_cores == 0
    assert select_candidate(candidates).name == "archive"


def test_ties_prefer_total_cores_then_priority_then_name() -> None:
    candidates = [
        CapacityCandidate("small", configured_cores=32, load5=0, priority=100),
        CapacityCandidate("z-large", configured_cores=256, load5=224, priority=100),
        CapacityCandidate("a-large", configured_cores=256, load5=224, priority=101),
    ]

    assert [item.name for item in rank_candidates(candidates)] == [
        "a-large",
        "z-large",
        "small",
    ]


def test_queue_requires_saturation_and_runner_owned_work() -> None:
    saturated = [CapacityCandidate("compute-a", 256, 300)]
    available = [CapacityCandidate("compute-a", 256, 255)]

    assert should_queue(saturated, active_runner_work=True)
    assert not should_queue(saturated, active_runner_work=False)
    assert not should_queue(available, active_runner_work=True)


def test_active_run_count_must_be_non_negative() -> None:
    with pytest.raises(ValueError, match="active run count"):
        rank_candidates([CapacityCandidate("compute-a", 256, 0, active_run_count=-1)])
