from __future__ import annotations

from threading import Barrier

from remote_runner._internal import pool


def candidate(name: str) -> dict[str, object]:
    return {
        "name": name,
        "server": {"ssh": name},
        "runtime": {},
        "cores": 8,
        "priority": 0,
        "test_slots": 0,
    }


def test_pool_probes_candidates_concurrently_and_preserves_order(monkeypatch) -> None:
    started = Barrier(2)

    def probe_endpoint(ssh: str, _timeout: int) -> dict[str, object]:
        started.wait(timeout=1)
        return {"reachable": True, "ssh": ssh}

    monkeypatch.setattr(pool, "probe_endpoint", probe_endpoint)

    result = pool._probe_candidates(
        [candidate("compute-b"), candidate("compute-a")],
        ssh_profile="auto",
        timeout=8,
    )

    assert [item["name"] for item in result] == ["compute-b", "compute-a"]
    assert [item["ssh"] for item in result] == ["compute-b", "compute-a"]


def test_candidate_endpoint_fallback_remains_ordered(monkeypatch) -> None:
    attempts: list[str] = []

    def probe_endpoint(ssh: str, _timeout: int) -> dict[str, object]:
        attempts.append(ssh)
        return {"reachable": ssh == "compute-a-ts"}

    monkeypatch.setattr(pool, "probe_endpoint", probe_endpoint)
    item = candidate("compute-a")
    item["server"] = {
        "ssh": "compute-a",
        "endpoints": {"intranet": "compute-a-int", "tailscale": "compute-a-ts"},
        "endpoint_order": ["intranet", "tailscale"],
    }

    result = pool._probe_candidates([item], ssh_profile="auto", timeout=8)

    assert attempts == ["compute-a-int", "compute-a-ts"]
    assert result[0]["ssh"] == "compute-a-ts"
    assert result[0]["ssh_profile"] == "tailscale"
