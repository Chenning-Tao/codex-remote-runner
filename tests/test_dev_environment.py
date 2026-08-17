from __future__ import annotations

from pathlib import Path

from remote_runner._internal.config import DevProjectConfig
from remote_runner._internal.dev_execution import DevServer, DevSession, _session_payload


def test_dev_payload_injects_the_selected_project_python() -> None:
    config = DevProjectConfig(
        path=Path("/project/.remote-runner.yaml"),
        project_root=Path("/project"),
        project_id="example",
        source_root=Path("/project"),
        stale_after_seconds=86400,
        include=(),
        exclude=(),
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
    )

    assert payload["runner_config"]["project_python"] == "/srv/envs/example/bin/python3"
