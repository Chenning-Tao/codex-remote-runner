from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from remote_runner._internal import output_source_gate, output_sync_remote


def test_gate_accepts_the_exact_worker_probe() -> None:
    assert output_source_gate.REMOTE_KIND_PROGRAM == output_sync_remote.REMOTE_KIND_PROGRAM


def test_probe_reports_directory_inside_root(tmp_path: Path) -> None:
    source = tmp_path / "run"
    source.mkdir()
    command = shlex.join(
        ["python3", "-c", output_source_gate.REMOTE_KIND_PROGRAM, str(source)]
    )

    result = output_source_gate.dispatch(
        command,
        root=tmp_path.resolve(),
        rrsync="/usr/bin/rrsync",
    )

    assert result == {"kind": "directory"}


def test_probe_rejects_path_outside_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    restricted = tmp_path / "restricted"
    restricted.mkdir()
    command = shlex.join(
        ["python3", "-c", output_source_gate.REMOTE_KIND_PROGRAM, str(source)]
    )

    with pytest.raises(output_source_gate.GateError, match="outside"):
        output_source_gate.dispatch(
            command,
            root=restricted.resolve(),
            rrsync="/usr/bin/rrsync",
        )


def test_identity_probe_returns_fixed_artifact_evidence(tmp_path: Path) -> None:
    source = tmp_path / "run"
    source.mkdir()
    (source / "COMPLETE").write_text("done\n", encoding="utf-8")
    (source / "summary.json").write_text("{}\n", encoding="utf-8")
    (source / "manifest.json").write_text('{"run_id":"rr-test"}\n', encoding="utf-8")
    command = shlex.join(
        [output_source_gate.IDENTITY_PROBE_COMMAND, str(source), "manifest.json"]
    )

    result = output_source_gate.dispatch(
        command,
        root=tmp_path.resolve(),
        rrsync="/usr/bin/rrsync",
    )

    assert result == {
        "root_exists": True,
        "complete_exists": True,
        "summary_sha256": (
            "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"
        ),
        "identity_sha256": (
            "c3b9e3c0fec5aca258728608a46b47e150b9bd23b8b0e43a39e9bd2a0ba93a64"
        ),
    }


def test_identity_probe_rejects_unapproved_filename(tmp_path: Path) -> None:
    source = tmp_path / "run"
    source.mkdir()
    command = shlex.join(
        [output_source_gate.IDENTITY_PROBE_COMMAND, str(source), "secret.txt"]
    )

    with pytest.raises(output_source_gate.GateError, match="filename"):
        output_source_gate.dispatch(
            command,
            root=tmp_path.resolve(),
            rrsync="/usr/bin/rrsync",
        )


def test_identity_probe_rejects_root_outside_restricted_root(tmp_path: Path) -> None:
    restricted = tmp_path / "restricted"
    restricted.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    command = shlex.join(
        [output_source_gate.IDENTITY_PROBE_COMMAND, str(outside), "manifest.json"]
    )

    with pytest.raises(output_source_gate.GateError, match="outside"):
        output_source_gate.dispatch(
            command,
            root=restricted.resolve(),
            rrsync="/usr/bin/rrsync",
        )


def test_rsync_sender_delegates_to_read_only_rrsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "run"
    source.mkdir()
    rrsync = tmp_path / "rrsync"
    rrsync.write_text("#!/bin/sh\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_execve(
        executable: str, argv: tuple[str, ...], environment: dict[str, str]
    ) -> None:
        captured.update(executable=executable, argv=argv, environment=environment)
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(output_source_gate.os, "execve", fake_execve)
    command = f"rsync --server --sender -logDtpre.iLsfxCIvu . {source}/"

    with pytest.raises(RuntimeError, match="intercepted"):
        output_source_gate.dispatch(
            command,
            root=tmp_path.resolve(),
            rrsync=str(rrsync),
        )

    assert captured["executable"] == str(rrsync)
    assert captured["argv"] == (str(rrsync), "-ro", "/")
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["SSH_ORIGINAL_COMMAND"] == command


def test_rsync_sender_rejects_path_outside_root(tmp_path: Path) -> None:
    restricted = tmp_path / "restricted"
    restricted.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    command = f"rsync --server --sender -logDtpre.iLsfxCIvu . {outside}/"

    with pytest.raises(output_source_gate.GateError, match="outside"):
        output_source_gate.dispatch(
            command,
            root=restricted.resolve(),
            rrsync="/usr/bin/rrsync",
        )


def test_rejects_arbitrary_remote_command(tmp_path: Path) -> None:
    with pytest.raises(output_source_gate.GateError, match="only an rsync sender"):
        output_source_gate.dispatch(
            "bash -lc id",
            root=tmp_path.resolve(),
            rrsync="/usr/bin/rrsync",
        )
