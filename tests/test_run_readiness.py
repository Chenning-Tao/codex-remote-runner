from __future__ import annotations

from typing import Any

import pytest

from remote_runner._internal.run_readiness import (
    cohort_report_readiness,
    report_readiness,
)


def view(
    phase: str,
    *,
    outcome: str | None = None,
    output_sync_status: str = "not_enqueued",
) -> dict[str, Any]:
    return {
        "phase": phase,
        "outcome": outcome,
        "output_sync": {"status": output_sync_status},
    }


@pytest.mark.parametrize(
    ("output_sync_status", "expected"),
    [
        ("not_enqueued", "ready"),
        ("completed", "ready"),
        ("pending", "waiting"),
        ("retryable", "waiting"),
        ("waiting_for_succeeded_state", "waiting"),
        ("cancelled", "attention"),
        ("unknown", "attention"),
    ],
)
def test_succeeded_run_readiness_follows_output_sync(
    output_sync_status: str,
    expected: str,
) -> None:
    assert (
        report_readiness(
            view(
                "terminal",
                outcome="succeeded",
                output_sync_status=output_sync_status,
            )
        )
        == expected
    )


@pytest.mark.parametrize("outcome", ["failed", "stopped"])
def test_unsuccessful_terminal_run_is_ready_without_output_sync(outcome: str) -> None:
    assert (
        report_readiness(
            view("terminal", outcome=outcome, output_sync_status="pending")
        )
        == "ready"
    )


def test_cohort_attention_preempts_other_members() -> None:
    assert (
        cohort_report_readiness(
            [
                view("running"),
                view(
                    "terminal",
                    outcome="succeeded",
                    output_sync_status="cancelled",
                ),
            ]
        )
        == "attention"
    )
