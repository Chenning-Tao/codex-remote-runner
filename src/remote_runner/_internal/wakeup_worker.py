from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .codex_app_server import commit_wakeup_turn
from .wakeup import (
    AMBIGUOUS_START_SECONDS,
    CONTROLLER_WAIT_SECONDS,
    WakeupPaths,
    _sync_pending_marker_locked,
    archive_history_commit,
    build_wake_prompt,
    claim_delivery,
    list_subscriptions,
    mark_turn_start_attempt,
    monitoring_batches,
    poll_batch,
    record_error,
    retry_delay,
    state_lock,
    worker_lock,
)


def _delivery_due(subscription: dict[str, Any], now: float) -> bool:
    return subscription["status"] in {"ready", "delivering"} and float(
        subscription["delivery_not_before"]
    ) <= now


def run_worker(
    paths: WakeupPaths,
    *,
    once: bool = False,
    supervised: bool = False,
) -> dict[str, Any]:
    with worker_lock(paths, wait=supervised) as acquired:
        if not acquired:
            return {"status": "already_running", "processed": 0}

        processed = 0
        batch_cursor = 0
        while True:
            subscriptions = list_subscriptions(paths)
            if not subscriptions:
                with state_lock(paths):
                    _sync_pending_marker_locked(paths)
                return {"status": "idle", "processed": processed}

            now = time.time()
            due = next(
                (
                    subscription
                    for subscription in subscriptions
                    if _delivery_due(subscription, now)
                ),
                None,
            )
            if due is not None:
                wake_id = str(due["wake_id"])
                claimed = claim_delivery(paths, wake_id)
                if claimed is None:
                    continue
                payload = claimed.get("ready_payload")
                if not isinstance(payload, dict):
                    raise ValueError("ready wakeup subscription has no payload")
                attempted_at = claimed.get("turn_start_attempted_at")
                allow_start = attempted_at is None or now >= (
                    float(attempted_at) + AMBIGUOUS_START_SECONDS
                )
                if allow_start:
                    refreshed = mark_turn_start_attempt(
                        paths,
                        wake_id,
                        attempted_at=now,
                    )
                    if refreshed is None:
                        continue
                    claimed = refreshed
                try:
                    delivery = commit_wakeup_turn(
                        Path(str(claimed["codex_executable"])),
                        str(claimed["thread_id"]),
                        wake_id,
                        build_wake_prompt(
                            payload,
                            project_config=Path(str(claimed["project_config"])),
                        ),
                        start_if_missing=allow_start,
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    record_error(
                        paths,
                        [wake_id],
                        kind="delivery",
                        error=exc,
                    )
                    if once:
                        return {
                            "status": "delivery_retryable",
                            "processed": processed,
                            "wake_id": wake_id,
                        }
                    latest = next(
                        item
                        for item in list_subscriptions(paths)
                        if item["wake_id"] == wake_id
                    )
                    time.sleep(retry_delay(int(latest["delivery_attempts"])))
                    continue
                archive_history_commit(paths, wake_id, delivery)
                processed += 1
                if once:
                    return {
                        "status": "history_committed",
                        "processed": processed,
                        "wake_id": wake_id,
                    }
                continue

            batches = monitoring_batches(subscriptions)
            if batches:
                batch = batches[batch_cursor % len(batches)]
                batch_cursor += 1
                wake_ids = [str(subscription["wake_id"]) for subscription in batch]
                try:
                    observation = poll_batch(paths, batch)
                except (OSError, RuntimeError, ValueError) as exc:
                    record_error(
                        paths,
                        wake_ids,
                        kind="controller",
                        error=exc,
                    )
                    if once:
                        return {
                            "status": "controller_retryable",
                            "processed": processed,
                            "wake_ids": wake_ids,
                        }
                    latest = {
                        str(item["wake_id"]): item
                        for item in list_subscriptions(paths)
                    }
                    attempt_values = [
                        int(latest[wake_id]["controller_attempts"])
                        for wake_id in wake_ids
                        if wake_id in latest
                    ]
                    if attempt_values:
                        time.sleep(retry_delay(max(attempt_values)))
                else:
                    if (
                        not once
                        and observation["ready"] == 0
                        and observation["controller_ready"] is True
                    ):
                        time.sleep(CONTROLLER_WAIT_SECONDS)
                if once:
                    return {"status": "observed", "processed": processed}
                continue

            next_delivery = min(
                float(subscription["delivery_not_before"])
                for subscription in subscriptions
                if subscription["status"] in {"ready", "delivering"}
            )
            if once:
                return {"status": "waiting_to_deliver", "processed": processed}
            time.sleep(min(CONTROLLER_WAIT_SECONDS, max(0.05, next_delivery - now)))
