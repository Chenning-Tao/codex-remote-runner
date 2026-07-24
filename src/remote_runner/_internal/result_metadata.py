from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


RESULT_INTENTS = ("candidate", "supporting", "excluded")
LEGACY_RESULT_INTENT = "unclassified"
MONITOR_RESULT_INTENTS = (*RESULT_INTENTS, LEGACY_RESULT_INTENT)
_TAG_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_TAGS = 32
_MAX_TAG_VALUE_LENGTH = 256


def normalize_result_intent(
    value: object,
    *,
    allow_unclassified: bool = False,
    field: str = "result_intent",
) -> str:
    allowed = MONITOR_RESULT_INTENTS if allow_unclassified else RESULT_INTENTS
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(allowed)
        raise ValueError(f"{field} must be one of: {choices}")
    return value


def normalize_result_tags(
    value: object,
    *,
    field: str = "result_tags",
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    if len(value) > _MAX_TAGS:
        raise ValueError(f"{field} must contain at most {_MAX_TAGS} tags")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or _TAG_KEY_RE.fullmatch(raw_key) is None:
            raise ValueError(
                f"{field} keys must start with an alphanumeric character and contain "
                "only letters, digits, dots, underscores, or hyphens (max 64 chars)"
            )
        if (
            not isinstance(raw_value, str)
            or not raw_value
            or "\x00" in raw_value
            or "\n" in raw_value
            or "\r" in raw_value
            or len(raw_value) > _MAX_TAG_VALUE_LENGTH
        ):
            raise ValueError(
                f"{field}[{raw_key!r}] must be a non-empty single-line string "
                f"of at most {_MAX_TAG_VALUE_LENGTH} characters"
            )
        normalized[raw_key] = raw_value
    return dict(sorted(normalized.items()))


def parse_result_tags(values: Sequence[str] | None) -> dict[str, str]:
    tags: dict[str, str] = {}
    for item in values or ():
        if not isinstance(item, str) or "=" not in item:
            raise ValueError("--tag must use KEY=VALUE")
        key, value = item.split("=", 1)
        if key in tags:
            raise ValueError(f"duplicate --tag key: {key!r}")
        tags[key] = value
    return normalize_result_tags(tags, field="--tag")


def legacy_result_metadata() -> tuple[str, dict[str, str]]:
    return LEGACY_RESULT_INTENT, {}


def stored_result_metadata(raw: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    intent = raw.get("result_intent", LEGACY_RESULT_INTENT)
    tags = raw.get("result_tags", {})
    return (
        normalize_result_intent(
            intent,
            allow_unclassified=True,
            field="stored result_intent",
        ),
        normalize_result_tags(tags, field="stored result_tags"),
    )
