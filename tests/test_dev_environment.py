from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

from remote_runner._internal.config import DevProfile, DevProjectConfig
from remote_runner._internal.dev_execution import (
    DEV_RUNNER_SOURCE,
    DevServer,
    DevSession,
    _session_payload,
    resolve_dev_invocation,
)


def test_dev_payload_injects_the_selected_project_python() -> None:
    config = DevProjectConfig(
        path=Path("/project/.remote-runner.yaml"),
        project_root=Path("/project"),
        project_id="example",
        source_root=Path("/project"),
        stale_after_seconds=86400,
        include=(),
        exclude=(),
        profiles={},
        project_python_by_server={"compute-a": "/srv/envs/example/bin/python3"},
    )
    server = DevServer(
        name="compute-a",
        machine_id="compute-a",
        ssh="compute-a",
        ssh_profile="auto",
        cores=8,
        dev_root="/srv/remote-runner-dev",
        machine_fingerprint="sha256:example",
    )
    session = DevSession(
        project_id="example",
        session_id="dev-0123456789abcdef",
        token="0123456789abcdef0123456789abcdef",
        remote_root="/srv/remote-runner-dev/example/tmp/dev-0123456789abcdef",
        cache_root="/srv/remote-runner-dev/example/cache",
    )

    payload = _session_payload(
        "create",
        config=config,
        server=server,
        session=session,
        command="true",
        profile="full-tests",
    )

    assert payload["runner_config"]["schema_version"] == 2
    assert payload["runner_config"]["project_python"] == "/srv/envs/example/bin/python3"
    assert payload["runner_config"]["server_name"] == "compute-a"
    assert payload["runner_config"]["machine_id"] == "compute-a"
    assert payload["runner_config"]["profile"] == "full-tests"


def test_dev_invocation_resolves_profile_command_and_filters() -> None:
    profile = DevProfile(
        name="full-tests",
        command="scripts/validate.sh",
        include=("artifact.bin",),
        exclude=("results/",),
    )
    config = DevProjectConfig(
        path=Path("/project/.remote-runner.yaml"),
        project_root=Path("/project"),
        project_id="example",
        source_root=Path("/project"),
        stale_after_seconds=86400,
        include=("shared.json",),
        exclude=("scratch/",),
        profiles={profile.name: profile},
        project_python_by_server={},
    )

    invocation = resolve_dev_invocation(config, command=None, profile="full-tests")

    assert invocation.profile == "full-tests"
    assert invocation.command == "scripts/validate.sh"
    assert invocation.include == ("shared.json", "artifact.bin")
    assert invocation.exclude == ("scratch/", "results/")

    direct = resolve_dev_invocation(config, command="true", profile=None)
    assert direct.profile is None
    assert direct.command == "true"
    assert direct.include == ("shared.json",)


def test_dev_runner_exports_resource_json_and_selected_profile(tmp_path: Path) -> None:
    session = tmp_path / "dev-0123456789abcdef"
    source = session / "source"
    source.mkdir(parents=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    environment_path = source / "environment.json"
    command_code = (
        "import json,os,time; from pathlib import Path; "
        "Path('environment.json').write_text(json.dumps({"
        "'resources': json.loads(os.environ['RR_RESOURCE_JSON']), "
        "'profile': os.environ.get('RR_DEV_PROFILE')}), encoding='utf-8'); "
        "time.sleep(0.2)"
    )
    (session / "runner.py").write_text(DEV_RUNNER_SOURCE, encoding="utf-8")
    (session / "command.sh").write_text(
        f"{shlex.quote(sys.executable)} -c {shlex.quote(command_code)}\n",
        encoding="utf-8",
    )
    (session / "cleanup.py").write_text(
        "import json\nprint('RR_DEV_RESULT '+json.dumps({'ok': True}))\n",
        encoding="utf-8",
    )
    (session / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "dev_root": str(tmp_path),
                "project_id": "example",
                "session_id": session.name,
                "token": "0" * 32,
                "cores": 8,
                "cache_root": str(cache),
                "build_environment": {},
                "project_python": None,
                "server_name": "compute-a",
                "machine_id": "compute-a-physical",
                "profile": "full-tests",
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(session / "runner.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    exported = json.loads(environment_path.read_text(encoding="utf-8"))
    resources = exported["resources"]
    assert exported["profile"] == "full-tests"
    assert resources["schema"] == "remote-runner-dev-resources/v1"
    assert resources["server"] == "compute-a"
    assert resources["machine_id"] == "compute-a-physical"
    assert resources["profile"] == "full-tests"
    assert resources["configured_cores"] == resources["assigned_cores"] == 8
    assert resources["observed_logical_cpus"] is None or (
        resources["observed_logical_cpus"] > 0
    )
    assert resources["memory_total_bytes"] is None or resources[
        "memory_total_bytes"
    ] > 0
    assert resources["memory_available_bytes"] is None or (
        0 <= resources["memory_available_bytes"] <= resources["memory_total_bytes"]
    )
    assert resources["platform"]["system"]
    assert resources["platform"]["machine"]
