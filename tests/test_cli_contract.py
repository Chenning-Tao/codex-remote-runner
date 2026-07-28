from __future__ import annotations

import builtins
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from remote_runner import cli


ROOT = Path(__file__).resolve().parents[1]


def help_text(*args: str) -> str:
    completed = subprocess.run(
        [sys.executable, "-m", "remote_runner.cli", *args, "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_public_cli_has_exact_lifecycle_subcommands() -> None:
    top_level = help_text()
    for subcommand in (
        "prepare",
        "run",
        "monitor",
        "wait",
        "wakeup",
        "stop",
        "cleanup",
        "purge-run",
        "purge-task",
        "sync-outputs",
        "prune-outputs",
        "sync-pool",
        "add-server",
        "drain-server",
        "resume-server",
        "tui",
        "web",
    ):
        assert subcommand in top_level

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {"remote-runner": "remote_runner.cli:main"}


def test_public_subcommands_preserve_lifecycle_arguments() -> None:
    prepare_help = help_text("prepare")
    assert "--out" in prepare_help
    assert "--candidate-server" in prepare_help
    run_help = help_text("run")
    for option in (
        "--source-repo",
        "--prepared-manifest",
        "--queue-priority",
        "--workload-class",
        "--worker-policy",
        "--result-intent",
        "--tag",
        "--min-cores",
        "--candidate-server",
        "--output-relpath",
        "--wait",
    ):
        assert option in run_help
    assert "--output-path" not in run_help
    monitor_help = help_text("monitor")
    assert "--task-id" in monitor_help
    assert "--result-intent" in monitor_help
    assert "--all" not in monitor_help
    wait_help = help_text("wait")
    assert "--run-id" in wait_help
    assert "--until" in wait_help
    assert "reportable" in wait_help
    assert "--max-wait" in wait_help
    wakeup_register_help = help_text("wakeup", "register")
    assert "--run-id" in wakeup_register_help
    assert "--codex-thread-id" in wakeup_register_help
    assert "--codex-executable" in wakeup_register_help
    assert "--wake-id" in help_text("wakeup", "cancel")
    wakeup_help = help_text("wakeup")
    assert "install" in wakeup_help
    assert "uninstall" in wakeup_help
    assert "worker" not in wakeup_help
    assert "--run-id" in help_text("stop")
    assert "--apply" in help_text("cleanup")
    run_purge_help = help_text("purge-run")
    assert "--run-id" in run_purge_help
    assert "--replacement-run-id" in run_purge_help
    assert "--no-replacement" in run_purge_help
    assert "--apply" in run_purge_help
    purge_help = help_text("purge-task")
    assert "--task-id" in purge_help
    assert "--apply" in purge_help
    assert "--project-config" in help_text("sync-outputs")
    prune_outputs_help = help_text("prune-outputs")
    assert "--run-id" in prune_outputs_help
    assert "--server" in prune_outputs_help
    assert "--apply" in prune_outputs_help
    assert "--server-registry" in help_text("sync-pool")
    add_server_help = help_text("add-server")
    assert "--run-id" in add_server_help
    assert "--server" in add_server_help
    tui_help = help_text("tui")
    assert "--server-registry" in tui_help
    assert "--stop-timeout" in tui_help
    web_help = help_text("web")
    assert "--server-registry" in web_help
    assert "--port" in web_help
    assert "--no-open" in web_help
    assert "--server" in help_text("drain-server")
    assert "--server" in help_text("resume-server")


def test_tui_reports_missing_optional_dependencies_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = "textual"
    original_import = builtins.__import__

    def fail_tui_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tui" and level == 1:
            raise ModuleNotFoundError(f"No module named '{missing}'", name=missing)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fail_tui_import)

    with pytest.raises(SystemExit) as raised:
        cli.main(["tui", "--project-config", "/tmp/project.yaml"])

    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert "the TUI optional dependency is not installed" in stderr
    assert "Traceback" not in stderr


def test_web_reports_missing_optional_dependencies_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = "starlette"
    original_import = builtins.__import__

    def fail_web_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "web_app" and level == 1:
            raise ModuleNotFoundError(f"No module named '{missing}'", name=missing)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fail_web_import)

    with pytest.raises(SystemExit) as raised:
        cli.main(["web", "--project-config", "/tmp/project.yaml"])

    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert "the web dashboard optional dependency is not installed" in stderr
    assert "Traceback" not in stderr


def test_skill_and_agent_metadata_match_normal_flow() -> None:
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    metadata = yaml.safe_load(
        (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    )
    for command in (
        "remote-runner prepare",
        "remote-runner run",
        "remote-runner monitor",
        "remote-runner wait",
        "remote-runner wakeup",
        "remote-runner stop",
        "remote-runner cleanup",
        "remote-runner purge-run",
        "remote-runner purge-task",
        "remote-runner sync-outputs",
        "remote-runner prune-outputs",
    ):
        assert command in skill
    assert "remote-runner tui" not in skill
    assert "Treat the local Git repository as the only source authority" in skill
    assert (
        "Run and follow durable remote workloads"
        == metadata["interface"]["short_description"]
    )
    assert (
        "foreground wait for a live Codex App report"
        in metadata["interface"]["default_prompt"]
    )
    assert "output synchronization completes" in metadata["interface"]["default_prompt"]
    assert "history-only follow-up" in metadata["interface"]["default_prompt"]


def test_user_facing_skill_and_help_hide_internal_schemas() -> None:
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8").lower()
    assert "schema-v" not in skill
    for command in (
        "run",
        "monitor",
        "wait",
        "stop",
        "cleanup",
        "purge-run",
        "purge-task",
        "prune-outputs",
    ):
        assert "schema-v" not in help_text(command).lower()
