from __future__ import annotations

import pytest

from remote_runner._internal.controller.run_view import derive_run_view


RUN_ID = "rr-0123456789abcdef"


def queue(status: str, revision: int = 1) -> dict[str, object]:
    return {
        "status": status,
        "revision": revision,
        "updated_at": "2026-07-24T00:00:00+00:00",
        "error": None,
    }


def execution(status: str, revision: int = 1) -> dict[str, object]:
    return {
        "registry_kind": "current",
        "authoritative_status": status,
        "revision": revision,
        "updated_at": "2026-07-24T00:00:00+00:00",
    }


@pytest.mark.parametrize(
    ("queued", "executed", "phase", "outcome", "source"),
    [
        (queue("queued"), None, "queued", None, None),
        (queue("dispatching"), None, "dispatching", None, None),
        (queue("dispatched"), None, "attention_required", None, None),
        (queue("failed"), None, "terminal", "failed", "queue"),
        (queue("stopped"), None, "terminal", "stopped", "queue"),
        (queue("dispatching"), execution("registered"), "registered", None, None),
        (queue("dispatched"), execution("running"), "running", None, None),
        (
            queue("dispatched"),
            execution("succeeded"),
            "terminal",
            "succeeded",
            "execution",
        ),
        (
            queue("dispatched"),
            execution("failed"),
            "terminal",
            "failed",
            "execution",
        ),
        (
            queue("dispatched"),
            execution("stopped"),
            "terminal",
            "stopped",
            "execution",
        ),
        (queue("failed"), execution("running"), "attention_required", None, None),
        (
            queue("failed"),
            execution("succeeded"),
            "attention_required",
            None,
            None,
        ),
    ],
)
def test_derive_run_view_state_matrix(
    queued: dict[str, object] | None,
    executed: dict[str, object] | None,
    phase: str,
    outcome: str | None,
    source: str | None,
) -> None:
    view = derive_run_view(
        project_id="example",
        run_id=RUN_ID,
        queue=queued,
        execution=executed,
        output_sync={"status": "not_enqueued"},
    )

    assert view["phase"] == phase
    assert view["outcome"] == outcome
    assert view["terminal_source"] == source
    assert view["etag"].startswith("sha256:")


def test_noncurrent_execution_fails_closed() -> None:
    view = derive_run_view(
        project_id="example",
        run_id=RUN_ID,
        queue=queue("dispatched"),
        execution={
            "registry_kind": "legacy",
            "authoritative_status": None,
            "stored_status": "done",
        },
        output_sync={"status": "not_enqueued"},
    )

    assert view["phase"] == "attention_required"
    assert "not a supported current" in view["attention_reason"]


def test_missing_and_purged_runs_are_distinct() -> None:
    missing = derive_run_view(
        project_id="example",
        run_id=RUN_ID,
        queue=None,
        execution=None,
        output_sync={"status": "not_enqueued"},
    )
    purged = derive_run_view(
        project_id="example",
        run_id=RUN_ID,
        queue=None,
        execution=None,
        output_sync={"status": "not_enqueued"},
        purge={"status": "purged"},
    )

    assert missing["phase"] == "missing"
    assert purged["phase"] == "purged"


def test_etag_is_stable_and_changes_with_authoritative_state() -> None:
    first = derive_run_view(
        project_id="example",
        run_id=RUN_ID,
        queue=queue("queued", 0),
        execution=None,
        output_sync={"status": "not_enqueued"},
    )
    same = derive_run_view(
        project_id="example",
        run_id=RUN_ID,
        queue=queue("queued", 0),
        execution=None,
        output_sync={"status": "not_enqueued"},
    )
    changed = derive_run_view(
        project_id="example",
        run_id=RUN_ID,
        queue=queue("dispatching", 1),
        execution=None,
        output_sync={"status": "not_enqueued"},
    )

    assert first["etag"] == same["etag"]
    assert changed["etag"] != first["etag"]
