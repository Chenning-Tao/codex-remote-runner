from __future__ import annotations

import pytest

from remote_runner._internal.result_metadata import (
    normalize_result_intent,
    normalize_result_tags,
    parse_result_tags,
)


def test_result_metadata_accepts_fixed_intent_and_open_tags() -> None:
    assert normalize_result_intent("candidate") == "candidate"
    assert parse_result_tags(
        ["purpose=canary", "campaign=historical-backfill", "phase=smoke test"]
    ) == {
        "campaign": "historical-backfill",
        "phase": "smoke test",
        "purpose": "canary",
    }


@pytest.mark.parametrize("value", ["formal", "", None])
def test_result_metadata_rejects_unsupported_intent(value: object) -> None:
    with pytest.raises(ValueError, match="candidate, supporting, excluded"):
        normalize_result_intent(value)


def test_result_metadata_rejects_duplicate_or_unsafe_tags() -> None:
    with pytest.raises(ValueError, match="duplicate --tag key"):
        parse_result_tags(["purpose=canary", "purpose=benchmark"])
    with pytest.raises(ValueError, match="keys must start"):
        normalize_result_tags({"bad key": "value"})
    with pytest.raises(ValueError, match="single-line"):
        normalize_result_tags({"purpose": "bad\nvalue"})
