from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from remote_runner._internal import output_prune, output_sync, registration
from remote_runner._internal.controller import output_prune as controller_output_prune
from remote_runner._internal.controller import output_sync_worker
from remote_runner._internal.controller.registry import (
    ControllerPaths,
    controller_paths,
)
from remote_runner._internal.execution_registry import (
    ProjectPaths,
    load_current_run,
    project_paths,
    update_current_state,
    write_yaml,
)


RUN_ID = "rr-0123456789abcdef"
REVISION = "a" * 40
OUTPUT_PATH = "/srv/project/output/run"
OUTPUT_ROOT = "/srv/project/output"


def make_synchronized_run(tmp_path: Path) -> tuple[ControllerPaths, ProjectPaths]:
    paths = controller_paths(tmp_path / "controller", "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    registration.register(
        argparse.Namespace(
            project_config=paths.config_path,
            label="prune-test",
            task_id="task-1",
            workload_class="standard",
            server="TENCENT",
            ssh="tencent",
            ssh_profile="intranet",
            configured_cores=8,
            minimum_cores=1,
            assigned_cores=8,
            command="true\n",
            remote_workdir=str(worktree),
            project_python=sys.executable,
            expected_revision=REVISION,
            source_revision=REVISION,
            prepared_servers=["TENCENT"],
            submitted_command="true",
            require_clean_worktree=True,
            output_root=OUTPUT_ROOT,
            output_relpath="run",
            output_path=OUTPUT_PATH,
            output_metadata=json.dumps({"kind": "result"}),
            privacy=None,
            run_id=RUN_ID,
        )
    )
    execution_paths = project_paths(paths.config_path)
    output_sync.store_config(
        execution_paths.registry_root,
        {
            "schema_version": 1,
            "target_server": "archive",
            "target_ssh": "archive",
            "target_root": "/srv/archive",
            "target_python": "/opt/python3",
            "source_ssh_config": "/home/user/.ssh/output-sync.conf",
            "source_hosts": {"TENCENT": "tencent-int"},
            "retry_seconds": 60,
        },
    )
    _manifest, state = load_current_run(execution_paths, RUN_ID)
    update_current_state(
        execution_paths,
        RUN_ID,
        int(state["revision"]),
        {
            "status": "succeeded",
            "finished_at": "2026-07-24T00:00:00Z",
            "exit_code": 0,
        },
    )
    intent = output_sync.list_pending(execution_paths.registry_root)[0]
    output_sync._complete_intent(
        execution_paths.registry_root,
        intent,
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "source_server": "TENCENT",
            "source_path": OUTPUT_PATH,
            "source_kind": "directory",
            "target_path": f"/srv/archive/artifacts/{RUN_ID}",
            "revision": REVISION,
            "task_id": "task-1",
            "authoritative_status": "succeeded",
            "terminal_at": intent["terminal_at"],
            "archived_at": "2026-07-24T00:01:00Z",
            "verification": "rsync_checksum_dry_run",
            "disposition": "copied_and_verified",
            "source_deletion_performed": False,
        },
    )
    return paths, execution_paths


def prune_args(paths: ControllerPaths, *, apply: bool) -> argparse.Namespace:
    return argparse.Namespace(
        controller_root=paths.root,
        project_id="example",
        run_id=RUN_ID,
        server=None,
        apply=apply,
        timeout=8,
    )


def test_prune_outputs_is_dry_run_then_records_verified_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, execution_paths = make_synchronized_run(tmp_path)

    preview = controller_output_prune.prune_outputs(prune_args(paths, apply=False))

    assert preview["candidate_count"] == 1
    assert preview["candidates"][0]["source_path"] == OUTPUT_PATH
    calls: list[str] = []
    monkeypatch.setattr(
        controller_output_prune,
        "prune_remote_output",
        lambda **kwargs: (
            calls.append(str(kwargs["output_path"]))
            or {"ok": True, "action": "removed_directory", "message": None}
        ),
    )

    result = controller_output_prune.prune_outputs(prune_args(paths, apply=True))

    assert result["pruned_count"] == 1
    assert result["failed_count"] == 0
    assert calls == [OUTPUT_PATH]
    completed = output_sync.list_completed_syncs(execution_paths.registry_root)
    assert completed[0]["receipt"]["source_deletion_performed"] is True
    assert (
        completed[0]["receipt"]["source_deletion_result"]["action"]
        == "removed_directory"
    )
    repeated = controller_output_prune.prune_outputs(prune_args(paths, apply=True))
    assert repeated["candidate_count"] == 0
    assert repeated["already_pruned"] == [RUN_ID]


def test_unknown_remote_prune_outcome_does_not_mark_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, execution_paths = make_synchronized_run(tmp_path)
    monkeypatch.setattr(
        controller_output_prune,
        "prune_remote_output",
        lambda **_kwargs: (_ for _ in ()).throw(
            output_prune.OutputPruneOutcomeUnknown("transport outcome unknown")
        ),
    )

    result = controller_output_prune.prune_outputs(prune_args(paths, apply=True))

    assert result["pruned_count"] == 0
    assert result["failed_count"] == 1
    completed = output_sync.list_completed_syncs(execution_paths.registry_root)
    assert completed[0]["receipt"]["source_deletion_performed"] is False


def test_prune_outputs_filters_by_source_server(tmp_path: Path) -> None:
    paths, _execution_paths = make_synchronized_run(tmp_path)
    args = prune_args(paths, apply=False)

    args.server = ["OTHER"]
    excluded = controller_output_prune.prune_outputs(args)
    args.server = ["TENCENT"]
    included = controller_output_prune.prune_outputs(args)

    assert excluded["servers"] == ["OTHER"]
    assert excluded["candidate_count"] == 0
    assert included["servers"] == ["TENCENT"]
    assert included["candidate_count"] == 1


def test_output_sync_worker_prunes_configured_source_after_archival(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, execution_paths = make_synchronized_run(tmp_path)
    config = output_sync.load_config(execution_paths.registry_root)
    assert config is not None
    output_sync.store_config(
        execution_paths.registry_root,
        {
            **config.to_payload(),
            "prune_after_sync": {"servers": ["TENCENT"]},
        },
    )
    calls: list[str] = []
    monkeypatch.setattr(
        controller_output_prune,
        "prune_remote_output",
        lambda **kwargs: (
            calls.append(str(kwargs["output_path"]))
            or {"ok": True, "action": "removed_directory", "message": None}
        ),
    )
    result = output_sync_worker.run_worker(
        paths,
        timeout=8,
        interval=60,
        once=True,
    )

    assert result == 0
    assert calls == [OUTPUT_PATH]
    assert not output_sync.has_unpruned_completed_syncs(
        execution_paths.registry_root,
        ("TENCENT",),
    )
    completed = output_sync.list_completed_syncs(execution_paths.registry_root)
    assert completed[0]["receipt"]["source_deletion_performed"] is True


def test_controller_starts_worker_for_post_sync_pruning_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, execution_paths = make_synchronized_run(tmp_path)
    config = output_sync.load_config(execution_paths.registry_root)
    assert config is not None
    output_sync.store_config(
        execution_paths.registry_root,
        {
            **config.to_payload(),
            "prune_after_sync": {"servers": ["TENCENT"]},
        },
    )
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append([str(item) for item in argv])
        return type(
            "Result",
            (),
            {"returncode": 1 if len(calls) == 1 else 0, "stderr": ""},
        )()

    monkeypatch.setattr(output_sync_worker, "resolve_tmux_executable", lambda: "tmux")
    monkeypatch.setattr(output_sync_worker.subprocess, "run", fake_run)

    started = output_sync_worker.ensure_output_sync_worker(
        paths,
        timeout=8,
        interval=60,
    )

    assert started is True
    assert calls[0][0:3] == ["tmux", "has-session", "-t"]
    assert calls[1][0:4] == ["tmux", "new-session", "-d", "-s"]


def test_prune_outputs_refuses_manifest_without_output_root(tmp_path: Path) -> None:
    paths, execution_paths = make_synchronized_run(tmp_path)
    manifest, _state = load_current_run(execution_paths, RUN_ID)
    manifest["output_root"] = None
    manifest["output_relpath"] = None
    write_yaml(execution_paths.runs_dir / RUN_ID / "manifest.yaml", manifest)

    with pytest.raises(ValueError, match="no configured output_root"):
        controller_output_prune.prune_outputs(prune_args(paths, apply=False))


def test_remote_output_prune_removes_only_declared_output(tmp_path: Path) -> None:
    output_root = tmp_path / "server" / "outputs"
    output = output_root / "run"
    output.mkdir(parents=True)
    (output / "result.bin").write_bytes(b"result")
    worktree = tmp_path / "server" / "worktree"
    worktree.mkdir()
    (worktree / "source.py").write_text("pass\n", encoding="utf-8")
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")

    completed = subprocess.run(
        [sys.executable, "-"],
        input=output_prune.build_prune_stdin(
            output_path=str(output),
            output_root=str(output_root),
            remote_workdir=str(worktree),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode()
    assert not output.exists()
    assert (worktree / "source.py").is_file()
    result = output_prune._prune_result(completed.stdout)
    assert result is not None and result["action"] == "removed_directory"


def test_remote_output_prune_refuses_symlink(tmp_path: Path) -> None:
    output_root = tmp_path / "server" / "outputs"
    output_root.mkdir(parents=True)
    retained = tmp_path / "retained"
    retained.mkdir()
    output = output_root / "run"
    output.symlink_to(retained, target_is_directory=True)
    worktree = tmp_path / "server" / "worktree"
    worktree.mkdir()

    completed = subprocess.run(
        [sys.executable, "-"],
        input=output_prune.build_prune_stdin(
            output_path=str(output),
            output_root=str(output_root),
            remote_workdir=str(worktree),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode != 0
    assert retained.is_dir()
    result = output_prune._prune_result(completed.stdout)
    assert result is not None and result["action"] == "validation"
