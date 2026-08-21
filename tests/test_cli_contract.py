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
        "dev",
        "prepare",
        "run",
        "monitor",
        "wait",
        "wait-cohort",
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
        "retire-server",
        "web",
    ):
        assert subcommand in top_level
    assert "tui" not in top_level
    assert "experiment" not in top_level
    assert "wakeup" not in top_level

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {"remote-runner": "remote_runner.cli:main"}


def test_public_subcommands_preserve_lifecycle_arguments() -> None:
    dev_help = help_text("dev")
    assert "--command" in dev_help
    assert "--profile" in dev_help
    parsed_dev_profile = cli.build_parser().parse_args(
        ["dev", "--server", "compute-a", "--profile", "full-tests"]
    )
    assert parsed_dev_profile.profile == "full-tests"
    assert parsed_dev_profile.command is None
    parsed_dev_command = cli.build_parser().parse_args(
        ["dev", "--server", "compute-a", "--command", "true"]
    )
    assert parsed_dev_command.command == "true"
    assert parsed_dev_command.profile is None
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["dev", "--server", "compute-a"])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            [
                "dev",
                "--server",
                "compute-a",
                "--command",
                "true",
                "--profile",
                "full-tests",
            ]
        )
    prepare_help = help_text("prepare")
    assert "--out" in prepare_help
    assert "--candidate-server" in prepare_help
    run_help = help_text("run")
    for option in (
        "--source-repo",
        "--prepared-manifest",
        "--queue-priority",
        "--workload-class",
        "--min-cores",
        "--cores",
        "--candidate-server",
        "--output-relpath",
        "--wait",
    ):
        assert option in run_help
    assert "--output-path" not in run_help
    monitor_help = help_text("monitor")
    assert "--task-id" in monitor_help
    assert "--all" not in monitor_help
    wait_help = help_text("wait")
    assert "--run-id" in wait_help
    assert "--until" in wait_help
    assert "reportable" in wait_help
    assert "--max-wait" in wait_help
    assert "--connection-grace" in wait_help
    parsed_wait = cli.build_parser().parse_args(["wait", "--run-id", "rr-test"])
    assert parsed_wait.max_wait is None
    assert parsed_wait.connection_grace is None
    cohort_wait_help = help_text("wait-cohort")
    assert "--run-id" in cohort_wait_help
    assert "reportable" in cohort_wait_help
    assert "--max-wait" in cohort_wait_help
    assert "--connection-grace" in cohort_wait_help
    parsed_cohort_wait = cli.build_parser().parse_args(
        [
            "wait-cohort",
            "--run-id",
            "rr-first",
            "--run-id",
            "rr-second",
        ]
    )
    assert parsed_cohort_wait.run_ids == ["rr-first", "rr-second"]
    assert "--run-id" in help_text("stop")
    assert "--apply" in help_text("cleanup")
    decommissioned_help = help_text("close-decommissioned-run")
    assert "--run-id" in decommissioned_help
    assert "--server" in decommissioned_help
    assert "--reason" in decommissioned_help
    assert "--apply" in decommissioned_help
    run_purge_help = help_text("purge-run")
    assert "--run-id" in run_purge_help
    assert "--apply" in run_purge_help
    assert "--delete-artifacts" in run_purge_help
    purge_help = help_text("purge-task")
    assert "--task-id" in purge_help
    assert "--apply" in purge_help
    assert "--delete-artifacts" in purge_help
    assert "--project-config" in help_text("sync-outputs")
    prune_outputs_help = help_text("prune-outputs")
    assert "--run-id" in prune_outputs_help
    assert "--server" in prune_outputs_help
    assert "--apply" in prune_outputs_help
    sync_pool_help = help_text("sync-pool")
    assert "--server-registry" in sync_pool_help
    assert "--source-repo" in sync_pool_help
    add_server_help = help_text("add-server")
    assert "--run-id" in add_server_help
    assert "--server" in add_server_help
    assert "--source-repo" in add_server_help
    web_help = help_text("web")
    assert "--server-registry" in web_help
    assert "--source-repo" in web_help
    assert "--port" in web_help
    assert "--no-open" in web_help
    assert "--server" in help_text("drain-server")
    assert "--server" in help_text("resume-server")
    retire_help = help_text("retire-server")
    assert "--server" in retire_help
    assert "--server-registry" in retire_help
    assert "--ssh-config" in retire_help
    assert "--known-hosts" in retire_help
    assert "--allow-unreachable" in retire_help
    assert "--apply" in retire_help


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
        "remote-runner validate-run",
        "remote-runner monitor",
        "remote-runner wait",
        "remote-runner wait-cohort",
        "remote-runner stop",
        "remote-runner cleanup",
        "remote-runner purge-run",
        "remote-runner purge-task",
        "remote-runner sync-outputs",
        "remote-runner prune-outputs",
    ):
        assert command in skill
    assert "remote-runner tui" not in skill
    assert "--profile NAME" in skill
    assert "RR_RESOURCE_JSON" in skill
    assert "Treat the local Git repository as the only source authority" in skill
    assert (
        "Run, follow, and validate durable remote workloads"
        == metadata["interface"]["short_description"]
    )
    prompt = metadata["interface"]["default_prompt"]
    assert "Detach by default" in prompt
    assert "--wait --until reportable" in prompt
    assert "only when I explicitly ask to wait" in prompt
    assert "Never add model polling" in prompt


def test_codex_docs_define_the_attached_completion_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    lifecycle = (ROOT / "references" / "lifecycle.md").read_text(encoding="utf-8")

    assert "event-driven Codex history follow-ups" not in readme
    assert "事件驱动的 Codex 任务历史回报" not in readme_zh
    assert "Process exit plus final stdout JSON is the completion signal" in skill
    assert "do not trigger model polling" in skill
    assert "Unchanged timeouts renew CLI transport without a model turn" in lifecycle
    for document in (readme, readme_zh, skill, lifecycle):
        assert "remote-runner wakeup" not in document


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
