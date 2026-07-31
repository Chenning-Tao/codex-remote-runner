from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys

from remote_runner._internal.output_sync_retirement import (
    ARCHIVE_CLEANUP_PROGRAM,
    PAYLOAD_ENV,
)


def run_program(
    tmp_path: Path,
    config: Path,
    *,
    apply: bool,
    expected_digest: str | None = None,
) -> dict[str, object]:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "source_ssh_config": str(config),
                "source_host": "project-source-compute-a",
                "apply": apply,
                "expected_digest": expected_digest,
            }
        ).encode()
    ).decode()
    completed = subprocess.run(
        [sys.executable, "-"],
        input=ARCHIVE_CLEANUP_PROGRAM,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "HOME": str(tmp_path), PAYLOAD_ENV: payload},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert isinstance(result, dict)
    return result


def test_archive_cleanup_previews_then_removes_exclusive_source_credentials(
    tmp_path: Path,
) -> None:
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    key = ssh / "project_compute_a_ed25519"
    key.write_text("PRIVATE\n", encoding="utf-8")
    key.with_suffix(".pub").write_text("ssh-ed25519 PUBLIC comment\n", encoding="utf-8")
    config = ssh / "output-sync.conf"
    config.write_text(
        """Host project-source-compute-a
    HostName 192.0.2.10
    IdentityFile {key}

Host project-source-compute-b
    HostName 192.0.2.11
    IdentityFile {other}
""".format(key=key, other=ssh / "other"),
        encoding="utf-8",
    )
    known_hosts = ssh / "known_hosts"
    known_hosts.write_text(
        "192.0.2.10 ssh-ed25519 KEY1\n192.0.2.11 ssh-ed25519 KEY2\n",
        encoding="utf-8",
    )

    preview = run_program(tmp_path, config, apply=False)

    assert preview["host_block"] == "remove"
    assert preview["public_keys"] == ["ssh-ed25519 PUBLIC"]
    assert preview["identities"][0]["exclusive"] is True
    assert key.is_file()
    assert "project-source-compute-a" in config.read_text(encoding="utf-8")

    applied = run_program(
        tmp_path,
        config,
        apply=True,
        expected_digest=str(preview["state_digest"]),
    )

    assert applied["applied"] is True
    assert not key.exists()
    assert not key.with_suffix(".pub").exists()
    assert "project-source-compute-a" not in config.read_text(encoding="utf-8")
    assert "project-source-compute-b" in config.read_text(encoding="utf-8")
    assert "192.0.2.10" not in known_hosts.read_text(encoding="utf-8")
    assert "192.0.2.11" in known_hosts.read_text(encoding="utf-8")


def test_archive_cleanup_preserves_identity_referenced_by_another_host(
    tmp_path: Path,
) -> None:
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    key = ssh / "shared"
    key.write_text("PRIVATE\n", encoding="utf-8")
    key.with_suffix(".pub").write_text("ssh-ed25519 PUBLIC\n", encoding="utf-8")
    config = ssh / "output-sync.conf"
    config.write_text(
        """Host project-source-compute-a
    IdentityFile {key}
Host project-source-compute-b
    IdentityFile {key}
""".format(key=key),
        encoding="utf-8",
    )

    preview = run_program(tmp_path, config, apply=False)
    result = run_program(
        tmp_path,
        config,
        apply=True,
        expected_digest=str(preview["state_digest"]),
    )

    assert result["identities"][0]["exclusive"] is False
    assert result["public_keys"] == []
    assert key.is_file()
    assert key.with_suffix(".pub").is_file()


def test_archive_cleanup_rejects_state_changed_after_preview(tmp_path: Path) -> None:
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    config = ssh / "output-sync.conf"
    config.write_text(
        "Host project-source-compute-a\n    HostName 192.0.2.10\n",
        encoding="utf-8",
    )
    preview = run_program(tmp_path, config, apply=False)
    config.write_text(
        config.read_text(encoding="utf-8") + "    User ubuntu\n",
        encoding="utf-8",
    )
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "source_ssh_config": str(config),
                "source_host": "project-source-compute-a",
                "apply": True,
                "expected_digest": preview["state_digest"],
            }
        ).encode()
    ).decode()

    completed = subprocess.run(
        [sys.executable, "-"],
        input=ARCHIVE_CLEANUP_PROGRAM,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "HOME": str(tmp_path), PAYLOAD_ENV: payload},
        check=False,
    )

    assert completed.returncode != 0
    assert "state changed after inspection" in completed.stderr
    assert "Host project-source-compute-a" in config.read_text(encoding="utf-8")
