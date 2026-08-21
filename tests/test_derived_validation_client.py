from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any

import pytest

from remote_runner import cli
from remote_runner._internal import derived_result_remote, derived_validation
from remote_runner._internal.derivation import build_relation, derived_run_id
from remote_runner._internal.execution_registry import (
    load_yaml,
    sha256_bytes,
    write_yaml,
)
from remote_runner._internal.source import (
    HistoricalSourceSelection,
    PreparationResult,
    PreparedServer,
)


PROJECT_ID = "example"
SOURCE_RUN_ID = "rr-0123456789abcdef"
REVISION = "a" * 40
ARCHIVE_TARGET = "archive"
ARCHIVE_ROOT = "/srv/archive/scientific-v1"
ARTIFACT_ROOT = f"{ARCHIVE_ROOT}/artifacts/rr-validator"
VALIDATOR_KEY = "portable-smoke/v1"
COMMAND = "experiments/remote_runner/run_synced_validator.sh"
VALIDATOR_RUN_ID = derived_run_id(
    project_id=PROJECT_ID,
    source_run_id=SOURCE_RUN_ID,
    validator_key=VALIDATOR_KEY,
)


def project_config(tmp_path: Path) -> Path:
    path = tmp_path / ".remote-runner.yaml"
    write_yaml(
        path,
        {
            "project_id": PROJECT_ID,
            "controller": {"ssh": "controller_host", "root": "/srv/.remote-runner"},
            "source": {"local_repo": "code"},
            "remote": {
                ARCHIVE_TARGET: {
                    "bare_repo": "/srv/repo.git",
                    "worktree_root": "/srv/worktrees",
                    "python": "/opt/python3",
                    "output_root": "/srv/output",
                }
            },
            "scheduling": {"testing": {"servers": [ARCHIVE_TARGET]}},
        },
    )
    (tmp_path / "code").mkdir(exist_ok=True)
    return path


def pool_entry(*, test_slots: int = 2) -> dict[str, Any]:
    return {
        "name": ARCHIVE_TARGET,
        "ssh": "archive-int",
        "ssh_profile": "intranet",
        "cores": 32,
        "priority": 100,
        "test_slots": test_slots,
        "probe": {"reachable": True},
        "runtime": {
            "bare_repo": "/srv/repo.git",
            "worktree_root": "/srv/worktrees",
            "python": "/opt/python3",
            "output_root": "/srv/output",
        },
    }


def source_payload() -> dict[str, Any]:
    return {
        "source_run_id": SOURCE_RUN_ID,
        "revision": REVISION,
        "server": "compute-a",
        "label": "producer",
        "task_id": "cohort-1",
        "artifact": {
            "target_server": ARCHIVE_TARGET,
            "target_path": f"{ARCHIVE_ROOT}/artifacts/{SOURCE_RUN_ID}",
            "receipt_sha256": "sha256:" + "b" * 64,
        },
    }


def run_view(
    *,
    phase: str = "terminal",
    outcome: str | None = "succeeded",
    sync_status: str = "completed",
    derivation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "run_id": VALIDATOR_RUN_ID,
        "phase": phase,
        "outcome": outcome,
        "queue": {"status": "dispatched"},
        "output_sync": {
            "status": sync_status,
            "receipt": {
                "target_path": ARTIFACT_ROOT,
                "verification": "rsync_checksum_dry_run",
            },
        },
    }
    if derivation is not None:
        view["queue"]["derivation"] = derivation
    return view


def args_for(config_path: Path, tmp_path: Path, **overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "project_config": config_path,
        "source_run_id": SOURCE_RUN_ID,
        "validator_key": VALIDATOR_KEY,
        "command": COMMAND,
        "result_relpath": "acceptance.json",
        "requested_cores": 1,
        "source_repo": None,
        "server_registry": tmp_path / "servers.yaml",
        "ssh_profile": "auto",
        "timeout": 8,
        "prepare_timeout": 60,
        "privacy": None,
        "wait": False,
        "max_wait": None,
        "connection_grace": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def install_client_doubles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lookup: dict[str, Any],
    submitted: list[dict[str, Any]],
    calls: list[str],
    test_slots: int = 2,
) -> None:
    def call_controller(
        _config: Any,
        action: str,
        *,
        timeout: int,
        action_args: tuple[str, ...] = (),
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append(action)
        if action == "validation-lookup":
            return lookup
        if action == "submit-validation":
            assert payload is not None
            submitted.append(payload)
            return {
                "outcome": {
                    "action": "submitted",
                    "run_id": payload["run_id"],
                    "submission_disposition": "created",
                    "queue_status": "queued",
                    "derivation": payload["derivation"],
                }
            }
        raise AssertionError(f"unexpected controller action: {action}")

    monkeypatch.setattr(derived_validation, "call_controller", call_controller)
    monkeypatch.setattr(
        derived_validation,
        "probe_project_pool",
        lambda *_args, **_kwargs: [pool_entry(test_slots=test_slots)],
    )
    monkeypatch.setattr(
        derived_validation,
        "select_historical_source_repo",
        lambda repo, _override, **_kwargs: HistoricalSourceSelection(
            source_repo=repo,
            selection="configured",
            clean_head="c" * 40,
            verified_revisions=(REVISION,),
        ),
    )
    monkeypatch.setattr(
        derived_validation,
        "prepare_revision",
        lambda *_args, **_kwargs: PreparationResult(
            revision=REVISION,
            ref=f"refs/remote-runner/{PROJECT_ID}/{REVISION}",
            prepared=(
                PreparedServer(ARCHIVE_TARGET, "archive:/srv/repo.git", "ref", REVISION),
            ),
            failures=(),
        ),
    )


def test_detached_submission_freezes_the_derived_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = project_config(tmp_path)
    submitted: list[dict[str, Any]] = []
    calls: list[str] = []
    install_client_doubles(
        monkeypatch,
        lookup={
            "validator_run_id": VALIDATOR_RUN_ID,
            "validator": None,
            "source": source_payload(),
            "source_error": None,
        },
        submitted=submitted,
        calls=calls,
    )

    result = derived_validation.validate_run(args_for(config_path, tmp_path))

    assert result["status"] == "submitted"
    assert derived_validation.validation_exit_code(result) == 0
    assert result["validator"]["run_id"] == VALIDATOR_RUN_ID
    assert result["validator"]["submission_disposition"] == "created"
    assert result["source"]["revision"] == REVISION
    assert calls == ["validation-lookup", "submit-validation"]
    job = submitted[0]
    assert job["workload_class"] == "test"
    assert job["requested_cores"] == 1
    assert job["minimum_cores"] == 1
    assert job["output_relpath"] == f"validation/{VALIDATOR_RUN_ID}"
    assert job["task_id"] == f"validation/{SOURCE_RUN_ID}"
    assert job["revision"] == REVISION
    assert [server["name"] for server in job["prepared_servers"]] == [ARCHIVE_TARGET]
    assert job["derivation"]["spec_sha256"].startswith("sha256:")
    assert job["submitted_command_sha256"] == sha256_bytes(COMMAND.encode())


def test_wait_returns_the_project_payload_without_interpreting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = project_config(tmp_path)
    install_client_doubles(
        monkeypatch,
        lookup={
            "validator_run_id": VALIDATOR_RUN_ID,
            "validator": None,
            "source": source_payload(),
            "source_error": None,
        },
        submitted=[],
        calls=[],
    )
    payload = {"schema": "decoder-synced-artifact-acceptance/v1", "status": "failed"}
    content = json.dumps(payload).encode()
    monkeypatch.setattr(
        derived_validation.waiting,
        "wait_for_run",
        lambda _args: {"wait_status": "completed", "run_view": run_view()},
    )
    monkeypatch.setattr(
        derived_validation,
        "_read_remote_result",
        lambda **_kwargs: {
            "path": f"{ARTIFACT_ROOT}/acceptance.json",
            "size": len(content),
            "sha256": sha256_bytes(content),
            "content_base64": base64.b64encode(content).decode(),
        },
    )

    result = derived_validation.validate_run(
        args_for(config_path, tmp_path, wait=True)
    )

    assert result["status"] == "validated"
    assert derived_validation.validation_exit_code(result) == 0
    assert result["result"]["payload"] == payload
    assert result["result"]["sha256"] == sha256_bytes(content)
    assert result["result"]["relpath"] == "acceptance.json"


@pytest.mark.parametrize(
    ("waited", "status", "code"),
    [
        ({"wait_status": "timed_out", "run_view": run_view(phase="running", outcome=None)}, "validation_pending", 3),
        ({"wait_status": "completed", "run_view": run_view(outcome="failed")}, "validator_failed", 4),
        ({"wait_status": "completed", "run_view": run_view(outcome="stopped")}, "validator_failed", 4),
        (
            {"wait_status": "attention_required", "run_view": run_view(phase="attention_required", outcome=None)},
            "attention_required",
            4,
        ),
    ],
)
def test_wait_reports_lifecycle_outcomes_without_reading_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    waited: dict[str, Any],
    status: str,
    code: int,
) -> None:
    config_path = project_config(tmp_path)
    install_client_doubles(
        monkeypatch,
        lookup={
            "validator_run_id": VALIDATOR_RUN_ID,
            "validator": None,
            "source": source_payload(),
            "source_error": None,
        },
        submitted=[],
        calls=[],
    )
    monkeypatch.setattr(
        derived_validation.waiting, "wait_for_run", lambda _args: waited
    )

    def refuse(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a non-succeeded validator must not be read")

    monkeypatch.setattr(derived_validation, "_read_remote_result", refuse)

    result = derived_validation.validate_run(
        args_for(config_path, tmp_path, wait=True)
    )

    assert result["status"] == status
    assert derived_validation.validation_exit_code(result) == code
    assert result["result"] is None
    assert result["validator"]["run_id"] == VALIDATOR_RUN_ID


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({"sha256": "sha256:" + "0" * 64}, "digest does not match"),
        ({"content": b"not json"}, "not valid UTF-8 JSON"),
        ({"content": b"[1, 2, 3]"}, "must contain one JSON object"),
    ],
)
def test_retrieval_guards_reject_untrustworthy_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: dict[str, Any],
    message: str,
) -> None:
    config_path = project_config(tmp_path)
    install_client_doubles(
        monkeypatch,
        lookup={
            "validator_run_id": VALIDATOR_RUN_ID,
            "validator": None,
            "source": source_payload(),
            "source_error": None,
        },
        submitted=[],
        calls=[],
    )
    content = record.get("content", json.dumps({"status": "passed"}).encode())
    monkeypatch.setattr(
        derived_validation.waiting,
        "wait_for_run",
        lambda _args: {"wait_status": "completed", "run_view": run_view()},
    )
    monkeypatch.setattr(
        derived_validation,
        "_read_remote_result",
        lambda **_kwargs: {
            "path": f"{ARTIFACT_ROOT}/acceptance.json",
            "size": len(content),
            "sha256": record.get("sha256", sha256_bytes(content)),
            "content_base64": base64.b64encode(content).decode(),
        },
    )

    result = derived_validation.validate_run(
        args_for(config_path, tmp_path, wait=True)
    )

    assert result["status"] == "result_unavailable"
    assert derived_validation.validation_exit_code(result) == 1
    assert message in result["error"]


def test_unsynchronized_validator_artifact_is_not_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = project_config(tmp_path)
    install_client_doubles(
        monkeypatch,
        lookup={
            "validator_run_id": VALIDATOR_RUN_ID,
            "validator": None,
            "source": source_payload(),
            "source_error": None,
        },
        submitted=[],
        calls=[],
    )
    monkeypatch.setattr(
        derived_validation.waiting,
        "wait_for_run",
        lambda _args: {
            "wait_status": "completed",
            "run_view": run_view(sync_status="pending"),
        },
    )

    result = derived_validation.validate_run(
        args_for(config_path, tmp_path, wait=True)
    )

    assert result["status"] == "result_unavailable"
    assert "not synchronized" in result["error"]


def test_existing_validator_resumes_without_resubmitting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = project_config(tmp_path)
    submitted: list[dict[str, Any]] = []
    calls: list[str] = []
    frozen = _frozen_relation()
    install_client_doubles(
        monkeypatch,
        lookup={
            "validator_run_id": VALIDATOR_RUN_ID,
            "validator": run_view(phase="queued", outcome=None, derivation=frozen),
            "source": None,
            "source_error": "source run rr-0123456789abcdef has no controller queue record",
        },
        submitted=submitted,
        calls=calls,
    )

    result = derived_validation.validate_run(args_for(config_path, tmp_path))

    assert result["status"] == "submitted"
    assert result["validator"]["submission_disposition"] == "reused"
    assert submitted == []
    assert calls == ["validation-lookup"]


def test_unreachable_archive_target_is_a_retrieval_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = project_config(tmp_path)
    install_client_doubles(
        monkeypatch,
        lookup={
            "validator_run_id": VALIDATOR_RUN_ID,
            "validator": run_view(derivation=_frozen_relation()),
            "source": source_payload(),
            "source_error": None,
        },
        submitted=[],
        calls=[],
    )
    monkeypatch.setattr(
        derived_validation.waiting,
        "wait_for_run",
        lambda _args: {"wait_status": "completed", "run_view": run_view()},
    )

    def unreachable(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("ssh probe failed")

    monkeypatch.setattr(derived_validation, "probe_project_pool", unreachable)

    result = derived_validation.validate_run(
        args_for(config_path, tmp_path, wait=True)
    )

    assert result["status"] == "result_unavailable"
    assert derived_validation.validation_exit_code(result) == 1
    assert "unavailable for retrieval" in result["error"]


def test_changed_command_conflicts_with_the_frozen_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = project_config(tmp_path)
    submitted: list[dict[str, Any]] = []
    install_client_doubles(
        monkeypatch,
        lookup={
            "validator_run_id": VALIDATOR_RUN_ID,
            "validator": run_view(phase="terminal", outcome="failed", derivation=_frozen_relation()),
            "source": source_payload(),
            "source_error": None,
        },
        submitted=submitted,
        calls=[],
    )

    result = derived_validation.validate_run(
        args_for(config_path, tmp_path, command="python other.py")
    )

    assert result["status"] == "invalid_request"
    assert derived_validation.validation_exit_code(result) == 2
    assert "under a new key" in result["error"]
    assert submitted == []


def test_unusable_source_without_an_existing_validator_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = project_config(tmp_path)
    install_client_doubles(
        monkeypatch,
        lookup={
            "validator_run_id": VALIDATOR_RUN_ID,
            "validator": None,
            "source": None,
            "source_error": "source run rr-0123456789abcdef is not reportable yet",
        },
        submitted=[],
        calls=[],
    )

    result = derived_validation.validate_run(args_for(config_path, tmp_path))

    assert result["status"] == "invalid_request"
    assert "not reportable yet" in result["error"]


def test_archive_target_must_be_a_configured_testing_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = project_config(tmp_path)
    document = load_yaml(config_path)
    document.pop("scheduling")
    write_yaml(config_path, document)
    install_client_doubles(
        monkeypatch,
        lookup={
            "validator_run_id": VALIDATOR_RUN_ID,
            "validator": None,
            "source": source_payload(),
            "source_error": None,
        },
        submitted=[],
        calls=[],
    )

    result = derived_validation.validate_run(args_for(config_path, tmp_path))

    assert result["status"] == "invalid_request"
    assert "scheduling.testing.servers" in result["error"]


def test_archive_target_without_testing_slots_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = project_config(tmp_path)
    install_client_doubles(
        monkeypatch,
        lookup={
            "validator_run_id": VALIDATOR_RUN_ID,
            "validator": None,
            "source": source_payload(),
            "source_error": None,
        },
        submitted=[],
        calls=[],
        test_slots=0,
    )

    result = derived_validation.validate_run(args_for(config_path, tmp_path))

    assert result["status"] == "invalid_request"
    assert "testing.slots" in result["error"]


def test_archive_target_without_an_output_root_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = project_config(tmp_path)
    entry = pool_entry()
    entry["runtime"] = {**entry["runtime"], "output_root": None}
    install_client_doubles(
        monkeypatch,
        lookup={
            "validator_run_id": VALIDATOR_RUN_ID,
            "validator": None,
            "source": source_payload(),
            "source_error": None,
        },
        submitted=[],
        calls=[],
    )
    monkeypatch.setattr(
        derived_validation, "probe_project_pool", lambda *_args, **_kwargs: [entry]
    )

    result = derived_validation.validate_run(args_for(config_path, tmp_path))

    assert result["status"] == "invalid_request"
    assert "output_root" in result["error"]


def test_old_controller_reports_a_runtime_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = project_config(tmp_path)

    def call_controller(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(
            "usage: controller [-h] ...\\ncontroller: error: argument action: "
            "invalid choice: 'validation-lookup'"
        )

    monkeypatch.setattr(derived_validation, "call_controller", call_controller)

    result = derived_validation.validate_run(args_for(config_path, tmp_path))

    assert result["status"] == "invalid_request"
    assert "does not support the 'validation-lookup' action" in result["error"]


def _frozen_relation() -> dict[str, Any]:
    facts = source_payload()
    relation = build_relation(
        source_run_id=facts["source_run_id"],
        source_revision=facts["revision"],
        source_server=facts["server"],
        target_server=facts["artifact"]["target_server"],
        target_path=facts["artifact"]["target_path"],
        receipt_sha256=facts["artifact"]["receipt_sha256"],
        validator_key=VALIDATOR_KEY,
        result_relpath="acceptance.json",
    )
    relation["spec_sha256"] = derived_validation.validator_spec_sha256(
        relation,
        validator_run_id=VALIDATOR_RUN_ID,
        command=COMMAND,
        requested_cores=1,
        privacy=None,
    )
    return relation


def test_cli_prints_the_result_schema_and_maps_the_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failure = {
        "schema_version": derived_validation.RESULT_SCHEMA,
        "status": "validator_failed",
        "source": None,
        "validator": {"run_id": VALIDATOR_RUN_ID},
        "result": None,
        "wait": None,
        "error": "validator failed",
    }
    monkeypatch.setattr(cli.derived_validation, "validate_run", lambda _args: failure)

    code = cli.main(
        [
            "validate-run",
            "--project-config",
            str(project_config(tmp_path)),
            "--source-run-id",
            SOURCE_RUN_ID,
            "--validator-key",
            VALIDATOR_KEY,
            "--command",
            COMMAND,
            "--result-relpath",
            "acceptance.json",
        ]
    )

    assert code == 4
    assert json.loads(capsys.readouterr().out) == failure


def test_exit_codes_cover_every_reported_status() -> None:
    statuses = {
        "validated": 0,
        "submitted": 0,
        "result_unavailable": 1,
        "invalid_request": 2,
        "validation_pending": 3,
        "validator_failed": 4,
        "attention_required": 4,
    }

    for status, code in statuses.items():
        assert derived_validation.validation_exit_code({"status": status}) == code
    with pytest.raises(ValueError):
        derived_validation.validation_exit_code({"status": "unknown"})


def result_payload(root: Path, relpath: str = "acceptance.json") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_root": str(root),
        "relpath": relpath,
        "max_bytes": derived_result_remote.MAX_RESULT_BYTES,
    }


def test_remote_reader_returns_bytes_and_digest(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    root.mkdir()
    content = json.dumps({"status": "passed"}).encode()
    (root / "acceptance.json").write_bytes(content)

    record = derived_result_remote.read_result(result_payload(root))

    assert record["size"] == len(content)
    assert record["sha256"] == sha256_bytes(content)
    assert record["path"] == str(root / "acceptance.json")


@pytest.mark.parametrize("relpath", ["/acceptance.json", "../acceptance.json", "./a.json", ""])
def test_remote_reader_payload_rejects_unsafe_paths(tmp_path: Path, relpath: str) -> None:
    with pytest.raises(ValueError):
        derived_result_remote.validate_payload(result_payload(tmp_path, relpath))


def test_remote_reader_payload_bounds_the_size_limit(tmp_path: Path) -> None:
    payload = result_payload(tmp_path)

    with pytest.raises(ValueError):
        derived_result_remote.validate_payload({**payload, "max_bytes": 0})
    with pytest.raises(ValueError):
        derived_result_remote.validate_payload(
            {**payload, "max_bytes": derived_result_remote.MAX_RESULT_BYTES + 1}
        )


def test_remote_reader_refuses_a_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "acceptance.json").write_text("{}", encoding="utf-8")
    link = tmp_path / "artifact"
    link.symlink_to(real)

    with pytest.raises(ValueError, match="root must not be a symlink"):
        derived_result_remote.read_result(result_payload(link))


def test_remote_reader_refuses_a_symlinked_result(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    root.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text("{}", encoding="utf-8")
    (root / "acceptance.json").symlink_to(secret)

    with pytest.raises(ValueError, match="symlink"):
        derived_result_remote.read_result(result_payload(root))


def test_remote_reader_refuses_a_symlinked_directory_component(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "acceptance.json").write_text("{}", encoding="utf-8")
    (root / "reports").symlink_to(elsewhere)

    with pytest.raises(ValueError, match="symlink"):
        derived_result_remote.read_result(
            result_payload(root, "reports/acceptance.json")
        )


def test_remote_reader_refuses_a_directory_and_a_missing_file(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    (root / "acceptance.json").mkdir(parents=True)

    with pytest.raises(ValueError, match="not a regular file"):
        derived_result_remote.read_result(result_payload(root))
    with pytest.raises(ValueError, match="does not exist"):
        derived_result_remote.read_result(result_payload(root, "missing.json"))


def test_remote_reader_refuses_an_oversized_result(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "acceptance.json").write_bytes(b"x" * 2048)

    with pytest.raises(ValueError, match="above the 1024 limit"):
        derived_result_remote.read_result(
            {**result_payload(root), "max_bytes": 1024}
        )


def test_remote_reader_main_reports_refusals_as_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = result_payload(tmp_path / "missing-root")
    monkeypatch.setenv(
        derived_result_remote.PAYLOAD_ENV,
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode("ascii"),
    )

    code = derived_result_remote.main()

    assert code == 1
    reported = json.loads(capsys.readouterr().out)
    assert reported["ok"] is False
    assert "artifact root" in reported["error"]
