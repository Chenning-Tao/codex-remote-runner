from __future__ import annotations

import pytest

from remote_runner._internal.output_paths import (
    normalize_absolute_output_path,
    normalize_output_relpath,
    resolve_output_path,
    validate_resolved_output,
)


@pytest.mark.parametrize(
    "value",
    (
        "",
        ".",
        "../result.json",
        "validation/../result.json",
        "/validation/result.json",
        "validation//result.json",
        "validation/./result.json",
        "$HOME/result.json",
        "~/result.json",
        "validation/`hostname`.json",
        "validation/result.json\n",
    ),
)
def test_output_relpath_rejects_ambiguous_or_expanded_values(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_output_relpath(value)


def test_output_relpath_preserves_literal_safe_metacharacters() -> None:
    value = "validation/run with spaces/result;final.json"

    assert normalize_output_relpath(value) == value


def test_output_path_requires_literal_normalized_absolute_posix_path() -> None:
    assert normalize_absolute_output_path("/srv/project/result.json") == (
        "/srv/project/result.json"
    )
    for value in ("result.json", "$HOME/result.json", "/srv//result.json"):
        with pytest.raises(ValueError):
            normalize_absolute_output_path(value)


def test_relative_identity_resolves_under_distinct_server_roots() -> None:
    relpath = "validation/c1/evidence.json"

    assert resolve_output_path("/home/user-a/project", relpath) == (
        "/home/user-a/project/validation/c1/evidence.json"
    )
    assert resolve_output_path("/home/user-b/project", relpath) == (
        "/home/user-b/project/validation/c1/evidence.json"
    )


def test_resolved_output_requires_exact_root_relative_path_binding() -> None:
    with pytest.raises(ValueError, match="does not match"):
        validate_resolved_output(
            output_root="/srv/project",
            output_relpath="validation/result.json",
            output_path="/srv/other/result.json",
        )
