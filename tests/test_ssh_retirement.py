from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from remote_runner._internal.ssh_retirement import (
    known_host_candidates,
    remove_exact_host_block,
    remove_known_hosts,
)


def test_remove_exact_host_block_preserves_adjacent_hosts() -> None:
    config = """Host shared
    HostName shared.example

Host compute-a
    HostName 192.0.2.10
    Port 2222
    IdentityFile ~/.ssh/shared

Host compute-b
    HostName 192.0.2.11
"""

    proposed, plan = remove_exact_host_block(config, "compute-a")

    assert "Host compute-a" not in proposed
    assert "Host shared" in proposed
    assert "Host compute-b" in proposed
    assert plan["hostnames"] == ["192.0.2.10", "compute-a"]
    assert plan["ports"] == ["2222"]
    assert plan["identity_files"] == ["~/.ssh/shared"]


def test_remove_exact_host_block_does_not_remove_shared_pattern() -> None:
    config = "Host compute-a compute-b\n    User ubuntu\n"

    proposed, plan = remove_exact_host_block(config, "compute-a")

    assert proposed == config
    assert plan["action"] == "already_absent"


def test_remove_exact_host_block_rejects_duplicate_exact_blocks() -> None:
    config = "Host compute-a\n    User one\nHost compute-a\n    User two\n"

    with pytest.raises(ValueError, match="duplicate exact Host blocks"):
        remove_exact_host_block(config, "compute-a")


def test_remove_known_hosts_supports_plain_hashed_and_shared_entries() -> None:
    candidate = "192.0.2.10"
    salt = b"01234567890123456789"
    digest = hmac.new(salt, candidate.encode(), hashlib.sha1).digest()
    hashed = "|1|{}|{}".format(
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )
    known_hosts = "".join(
        (
            f"{candidate} ssh-ed25519 KEY1\n",
            f"{hashed} ssh-ed25519 KEY2\n",
            "compute-a,shared ssh-ed25519 KEY3\n",
            "shared ssh-ed25519 KEY4\n",
        )
    )

    proposed, result = remove_known_hosts(
        known_hosts,
        known_host_candidates(["compute-a", candidate], []),
    )

    assert candidate not in proposed
    assert hashed not in proposed
    assert "shared ssh-ed25519 KEY3" in proposed
    assert "shared ssh-ed25519 KEY4" in proposed
    assert result["removed_host_tokens"] == 3
