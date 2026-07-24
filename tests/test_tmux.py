from __future__ import annotations

from pathlib import Path

from remote_runner._internal import tmux
from remote_runner._internal.tmux import (
    dispatcher_tmux_session,
    exact_tmux_target,
    resolve_tmux_executable,
    run_tmux_session,
)


def test_tmux_resolution_prefers_ssh_path(monkeypatch) -> None:
    monkeypatch.setattr(tmux.shutil, "which", lambda _command: "/custom/bin/tmux")

    assert resolve_tmux_executable() == "/custom/bin/tmux"


def test_tmux_resolution_falls_back_for_non_login_ssh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fallback = tmp_path / "tmux"
    fallback.write_text("tmux", encoding="utf-8")
    monkeypatch.setattr(tmux.shutil, "which", lambda _command: None)
    monkeypatch.setattr(tmux, "TMUX_FALLBACKS", (fallback,))

    assert resolve_tmux_executable() == str(fallback)


def test_run_tmux_session_preserves_run_identity() -> None:
    run_id = "rr-0123456789abcdef"

    assert run_tmux_session(run_id) == run_id


def test_dispatcher_tmux_session_is_project_scoped() -> None:
    assert dispatcher_tmux_session("example_project") == (
        "rr-dispatch-example_project"
    )


def test_exact_tmux_target_disables_prefix_matching() -> None:
    assert exact_tmux_target("rr-dispatch-example_project") == (
        "=rr-dispatch-example_project"
    )
