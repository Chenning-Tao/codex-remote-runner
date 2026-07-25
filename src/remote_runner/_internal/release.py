from __future__ import annotations

import argparse
import io
import json
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .config import load_managed_project_config
from .controller.layout import controller_release_layout


REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
ARTIFACT_MANIFEST = "release-artifact.json"
SOURCE_ARCHIVE = "source.tar.gz"
CONTROLLER_PAYLOAD = "controller-payload.tar.gz"
CONSTRAINTS = "constraints.txt"
UV_DISCOVERY_SCRIPT = r'''if command -v uv >/dev/null 2>&1; then
  command -v uv
elif [ -x /opt/homebrew/bin/uv ]; then
  printf '%s\n' /opt/homebrew/bin/uv
elif [ -x /usr/local/bin/uv ]; then
  printf '%s\n' /usr/local/bin/uv
elif [ -x /usr/bin/uv ]; then
  printf '%s\n' /usr/bin/uv
else
  exit 127
fi'''


@dataclass(frozen=True)
class ReleaseArtifact:
    root: Path
    revision: str
    wheel: Path
    constraints: Path
    source_archive: Path
    controller_payload: Path


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    input_data: bytes | str | None = None,
    text: bool = True,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[Any]:
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        timeout=timeout,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr
        if isinstance(stderr, bytes):
            detail = stderr.decode(errors="replace").strip()
        else:
            detail = stderr.strip()
        raise RuntimeError(detail or f"command failed: {shlex.join(argv)}")
    return completed


def resolve_clean_revision(repo: Path) -> str:
    resolved = repo.expanduser().resolve(strict=True)
    status = _run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=resolved,
    ).stdout
    if status.strip():
        raise ValueError("remote-runner release source must be clean and committed")
    revision = _run(["git", "rev-parse", "HEAD"], cwd=resolved).stdout.strip()
    if not REVISION_RE.fullmatch(revision):
        raise ValueError("remote-runner HEAD is not a full lowercase Git SHA")
    return revision


def _extract_git_archive(repo: Path, revision: str, destination: Path) -> None:
    archive = _run(
        ["git", "archive", "--format=tar", revision],
        cwd=repo,
        text=False,
    ).stdout
    destination.mkdir(parents=True, mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        bundle.extractall(destination, filter="data")


def _write_source_archive(source: Path, destination: Path) -> None:
    with tarfile.open(destination, mode="w:gz") as bundle:
        for path in sorted(source.rglob("*")):
            bundle.add(path, arcname=path.relative_to(source), recursive=False)


def _write_controller_payload(artifact: ReleaseArtifact) -> None:
    manifest_path = artifact.root / ARTIFACT_MANIFEST
    with tarfile.open(artifact.controller_payload, mode="w:gz") as bundle:
        for path in (
            artifact.wheel,
            artifact.constraints,
            artifact.source_archive,
            manifest_path,
        ):
            bundle.add(path, arcname=path.name)


def build_release(repo: Path, output_dir: Path) -> ReleaseArtifact:
    resolved_repo = repo.expanduser().resolve(strict=True)
    revision = resolve_clean_revision(resolved_repo)
    output = output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="remote-runner-release-") as temporary:
        source = Path(temporary) / "source"
        _extract_git_archive(resolved_repo, revision, source)
        revision_module = source / "src" / "remote_runner" / "_revision.py"
        revision_module.write_text(
            '"""Build-time source revision receipt."""\n\n'
            f'SOURCE_REVISION = "{revision}"\n',
            encoding="utf-8",
        )
        _run(["uv", "build", "--wheel", "--out-dir", str(output)], cwd=source)
        _run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--extra",
                "tui",
                "--extra",
                "web",
                "--no-emit-project",
                "--no-annotate",
                "--no-header",
                "--output-file",
                str(output / CONSTRAINTS),
            ],
            cwd=source,
        )
        _write_source_archive(source, output / SOURCE_ARCHIVE)

    wheels = sorted(output.glob("codex_remote_runner-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("release build must produce exactly one remote-runner wheel")
    manifest = {
        "revision": revision,
        "wheel": wheels[0].name,
        "constraints": CONSTRAINTS,
        "source_archive": SOURCE_ARCHIVE,
    }
    (output / ARTIFACT_MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    artifact = ReleaseArtifact(
        root=output,
        revision=revision,
        wheel=wheels[0],
        constraints=output / CONSTRAINTS,
        source_archive=output / SOURCE_ARCHIVE,
        controller_payload=output / CONTROLLER_PAYLOAD,
    )
    _write_controller_payload(artifact)
    return artifact


def load_release_artifact(root: Path) -> ReleaseArtifact:
    resolved = root.expanduser().resolve(strict=True)
    raw = json.loads((resolved / ARTIFACT_MANIFEST).read_text(encoding="utf-8"))
    revision = str(raw.get("revision", ""))
    if not REVISION_RE.fullmatch(revision):
        raise ValueError("release artifact has an invalid revision")
    artifact = ReleaseArtifact(
        root=resolved,
        revision=revision,
        wheel=resolved / str(raw["wheel"]),
        constraints=resolved / str(raw["constraints"]),
        source_archive=resolved / str(raw["source_archive"]),
        controller_payload=resolved / CONTROLLER_PAYLOAD,
    )
    for path in (
        artifact.wheel,
        artifact.constraints,
        artifact.source_archive,
        artifact.controller_payload,
    ):
        if not path.is_file():
            raise FileNotFoundError(f"release artifact is incomplete: {path.name}")
    return artifact


def install_local_tool(artifact: ReleaseArtifact) -> None:
    _run(
        [
            "uv",
            "tool",
            "install",
            "--force",
            "--python",
            "3.12",
            "--constraints",
            str(artifact.constraints),
            f"{artifact.wheel}[tui,web]",
        ]
    )
    tool_dir = _run(["uv", "tool", "dir"]).stdout.strip()
    if not tool_dir:
        raise RuntimeError("uv did not report its tool environment directory")
    bin_dir = _run(["uv", "tool", "dir", "--bin"]).stdout.strip()
    if not bin_dir:
        raise RuntimeError("uv did not report its tool executable directory")
    interpreter = (
        Path(tool_dir).expanduser() / "codex-remote-runner" / "bin" / "python"
    )
    executable = Path(bin_dir).expanduser() / "remote-runner"
    clean_environment = os.environ.copy()
    clean_environment.pop("PYTHONPATH", None)
    _run(
        [
            str(interpreter),
            "-c",
            (
                "import rich; import textual; import remote_runner.tui; "
                "from remote_runner.web_app import STATIC_ROOT; "
                "assert (STATIC_ROOT / 'index.html').is_file()"
            ),
        ],
        env=clean_environment,
    )
    version = _run(
        [str(executable), "--version"],
        env=clean_environment,
    ).stdout.strip()
    match = re.fullmatch(r"remote-runner [^ ]+ \(([0-9a-f]{40})\)", version)
    if match is None or match.group(1) != artifact.revision:
        raise RuntimeError(
            "installed local remote-runner revision does not match the release artifact"
        )


def _ssh_argv(target: str, timeout: int) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={timeout}",
        target,
    ]


def _remote_json(
    target: str,
    remote_argv: Sequence[str],
    *,
    timeout: int,
) -> dict[str, Any]:
    completed = _run(
        [*_ssh_argv(target, timeout), shlex.join(remote_argv)],
        timeout=timeout + 120,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("controller release command returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("controller release command returned invalid data")
    return result


def _controller_uv_path(target: str, timeout: int) -> str:
    try:
        completed = _run(
            [
                *_ssh_argv(target, timeout),
                shlex.join(["sh", "-c", UV_DISCOVERY_SCRIPT]),
            ],
            timeout=timeout + 10,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"uv is not available in the controller SSH environment: {exc}"
        ) from exc
    discovered = completed.stdout.strip()
    if not discovered.startswith("/"):
        raise RuntimeError("uv is not available in the controller SSH environment")
    return discovered


STAGE_SCRIPT = r'''set -eu
root=$1
revision=$2
uv=$3
payload=$4
wheel=$5
runner="$root/runner"
release="$runner/releases/$revision"
staging="$runner/releases/.$revision.staging.$$"
umask 077
mkdir -p "$runner/releases"
if [ -d "$release" ]; then
  test "$(cat "$release/.deployed-revision")" = "$revision"
  rm -f "$payload"
  exit 0
fi
trap 'rm -rf "$staging"' EXIT
mkdir -p "$staging/artifact" "$staging/source"
tar -xzf "$payload" -C "$staging/artifact"
tar -xzf "$staging/artifact/source.tar.gz" -C "$staging/source"
"$uv" venv "$staging/venv" --python 3.12
"$uv" pip install \
  --python "$staging/venv/bin/python" \
  --constraints "$staging/artifact/constraints.txt" \
  "$staging/artifact/$wheel"
"$staging/venv/bin/python" -c \
  'import sys; import remote_runner._internal.controller.service; import remote_runner._internal.controller.dispatcher; from remote_runner._revision import SOURCE_REVISION; raise SystemExit(0 if SOURCE_REVISION == sys.argv[1] else 1)' \
  "$revision"
printf '%s\n' "$revision" > "$staging/.deployed-revision"
chmod -R go-rwx "$staging"
mv "$staging" "$release"
trap - EXIT
rm -f "$payload"
'''


def stage_controller_release(
    artifact: ReleaseArtifact,
    *,
    controller_ssh: str,
    controller_root: str,
    timeout: int = 20,
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("controller release timeout must be positive")
    uv_probe = _controller_uv_path(controller_ssh, timeout)
    remote_payload = f"/tmp/remote-runner-{artifact.revision}.tar.gz"
    _run(
        ["scp", str(artifact.controller_payload), f"{controller_ssh}:{remote_payload}"],
        timeout=timeout + 120,
    )
    remote_command = shlex.join(
        [
            "sh",
            "-c",
            STAGE_SCRIPT,
            "remote-runner-stage",
            controller_root,
            artifact.revision,
            uv_probe,
            remote_payload,
            artifact.wheel.name,
        ]
    )
    _run(
        [*_ssh_argv(controller_ssh, timeout), remote_command],
        timeout=timeout + 300,
    )
    layout = controller_release_layout(controller_root)
    staged_interpreter = str(
        PurePosixPath(layout.releases_root)
        / artifact.revision
        / "venv"
        / "bin"
        / "python"
    )
    result = _remote_json(
        controller_ssh,
        [
            staged_interpreter,
            "-m",
            "remote_runner._internal.controller.release_gate",
            "--controller-root",
            controller_root,
            "inspect",
        ],
        timeout=timeout,
    )
    if result.get("revision") != artifact.revision:
        raise RuntimeError(
            "staged controller remote-runner revision does not match the release artifact"
        )
    return result


def activate_controller_release(
    artifact: ReleaseArtifact,
    *,
    controller_ssh: str,
    controller_root: str,
    timeout: int = 20,
) -> dict[str, Any]:
    layout = controller_release_layout(controller_root)
    staged_interpreter = str(
        PurePosixPath(layout.releases_root)
        / artifact.revision
        / "venv"
        / "bin"
        / "python"
    )
    return _remote_json(
        controller_ssh,
        [
            staged_interpreter,
            "-m",
            "remote_runner._internal.controller.release_gate",
            "--controller-root",
            controller_root,
            "activate",
            "--revision",
            artifact.revision,
        ],
        timeout=timeout,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and deploy private remote-runner releases.")
    subparsers = parser.add_subparsers(dest="action", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--repo", type=Path, default=Path.cwd())
    build.add_argument("--out", type=Path, required=True)
    install = subparsers.add_parser("install-local")
    install.add_argument("--artifact", type=Path, required=True)
    for action in ("stage-controller", "activate-controller"):
        command = subparsers.add_parser(action)
        command.add_argument("--artifact", type=Path, required=True)
        command.add_argument("--project-config", type=Path, required=True)
        command.add_argument("--timeout", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "build":
            artifact = build_release(args.repo, args.out)
            result = {"revision": artifact.revision, "artifact": str(artifact.root)}
        elif args.action == "install-local":
            artifact = load_release_artifact(args.artifact)
            install_local_tool(artifact)
            result = {"revision": artifact.revision, "installed": True}
        else:
            artifact = load_release_artifact(args.artifact)
            config = load_managed_project_config(args.project_config)
            operation = (
                stage_controller_release
                if args.action == "stage-controller"
                else activate_controller_release
            )
            result = operation(
                artifact,
                controller_ssh=config.controller.ssh,
                controller_root=config.controller.root,
                timeout=args.timeout,
            )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
