from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from .config import ManagedProjectConfig, load_managed_project_config
from .controller.client import call_controller
from .dashboard import build_server_inventory
from .execution_registry import (
    load_yaml,
    replace_config_text,
    replace_config_yaml,
    resolve_project_config,
)
from .output_sync_retirement import inspect_or_apply_archive_source
from .pool import DEFAULT_SERVER_REGISTRY
from .server_draining import update as update_server_drain
from .ssh_retirement import inspect_local_ssh_cleanup


RETIREMENT_SCHEMA = 2
DEFAULT_SSH_CONFIG = Path("~/.ssh/config").expanduser()
DEFAULT_KNOWN_HOSTS = Path("~/.ssh/known_hosts").expanduser()


class RetirementBlockedError(ValueError):
    def __init__(self, blockers: list[dict[str, Any]]) -> None:
        self.blockers = blockers
        codes = ", ".join(sorted({str(item.get("code")) for item in blockers}))
        super().__init__(f"server retirement is blocked: {codes}")


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a mapping")
    return value


def _proposed_project_document(
    raw: dict[str, Any],
    config: ManagedProjectConfig,
    server: str,
) -> tuple[dict[str, Any], list[dict[str, object]]]:
    proposed = deepcopy(raw)
    remotes = _mapping(proposed.get("remote"), "project config remote")
    _mapping(remotes.get(server), f"project config remote.{server}")
    changes: list[dict[str, object]] = [
        {"action": "remove", "field": f"remote.{server}"}
    ]

    output_sync = proposed.get("output_sync")
    if isinstance(output_sync, dict) and output_sync.get("target_server") == server:
        raise ValueError(
            f"cannot retire output synchronization target {server!r}; "
            "move output_sync.target_server first"
        )
    remaining_automatic = [
        name
        for name, runtime in config.remotes.items()
        if name != server and runtime.enabled and runtime.auto_select
    ]
    if not remaining_automatic:
        raise ValueError(
            f"cannot retire {server!r}; the project must retain at least one "
            "enabled automatic candidate"
        )
    del remotes[server]

    scheduling = proposed.get("scheduling")
    if isinstance(scheduling, dict):
        testing = scheduling.get("testing")
        if isinstance(testing, dict) and isinstance(testing.get("servers"), list):
            before = testing["servers"]
            after = [name for name in before if name != server]
            if after != before:
                changes.append(
                    {"action": "remove", "field": "scheduling.testing.servers"}
                )
                if after:
                    testing["servers"] = after
                else:
                    scheduling.pop("testing")

    if isinstance(output_sync, dict):
        source_hosts = output_sync.get("source_hosts")
        if isinstance(source_hosts, dict) and server in source_hosts:
            del source_hosts[server]
            changes.append(
                {"action": "remove", "field": f"output_sync.source_hosts.{server}"}
            )
        prune = output_sync.get("prune_after_sync")
        if isinstance(prune, dict) and isinstance(prune.get("servers"), list):
            before = prune["servers"]
            after = [name for name in before if name != server]
            if after != before:
                prune["servers"] = after
                changes.append(
                    {
                        "action": "remove",
                        "field": "output_sync.prune_after_sync.servers",
                    }
                )
    return proposed, changes


def _proposed_registry_document(
    raw: dict[str, Any], server: str
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, object]]]:
    proposed = deepcopy(raw)
    servers = _mapping(proposed.get("servers"), "global server registry servers")
    entry = dict(
        _mapping(servers.get(server), f"global server registry servers.{server}")
    )
    del servers[server]
    return proposed, entry, [{"action": "remove", "field": f"servers.{server}"}]


def _validate_project_document(path: Path, proposed: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}.retire-",
        suffix=".yaml",
        dir=path.parent,
    ) as handle:
        import yaml

        yaml.safe_dump(proposed, handle, sort_keys=False)
        handle.flush()
        load_managed_project_config(Path(handle.name))


def _unchanged(path: Path, original: bytes) -> None:
    if path.read_bytes() != original:
        raise RuntimeError(
            f"configuration changed during retirement: {path}; "
            "the server remains drained, review the files before retrying"
        )


def _path_argument(args: argparse.Namespace, name: str, default: Path) -> Path:
    return Path(getattr(args, name, default)).expanduser().resolve()


def _context(args: argparse.Namespace) -> dict[str, Any]:
    server = args.server
    if server == "all":
        raise ValueError("--server must name one configured server, not 'all'")
    config_path = resolve_project_config(args.project_config)
    registry_path = (
        Path(getattr(args, "server_registry", DEFAULT_SERVER_REGISTRY))
        .expanduser()
        .resolve(strict=True)
    )
    if registry_path == config_path:
        raise ValueError(
            "project config and global server registry must be different files"
        )
    config = load_managed_project_config(config_path)
    if server not in config.remotes:
        raise ValueError(f"server {server!r} is not configured for this project")
    project_raw = load_yaml(config_path)
    registry_raw = load_yaml(registry_path)
    proposed_project, project_changes = _proposed_project_document(
        project_raw, config, server
    )
    proposed_registry, registry_entry, registry_changes = _proposed_registry_document(
        registry_raw, server
    )
    ssh_alias = registry_entry.get("ssh", server)
    if not isinstance(ssh_alias, str) or not ssh_alias.strip():
        raise ValueError(f"global server registry SSH alias for {server!r} is invalid")
    _validate_project_document(config_path, proposed_project)
    ssh_config = _path_argument(args, "ssh_config", DEFAULT_SSH_CONFIG)
    known_hosts = _path_argument(args, "known_hosts", DEFAULT_KNOWN_HOSTS)
    local_ssh = inspect_local_ssh_cleanup(ssh_config, known_hosts, ssh_alias)
    inventory = build_server_inventory(config, registry_path)
    target = next((item for item in inventory if item["name"] == server), None)
    if target is None:
        raise ValueError(f"server {server!r} has no dashboard inventory")
    return {
        "server": server,
        "config": config,
        "config_path": config_path,
        "registry_path": registry_path,
        "project_bytes": config_path.read_bytes(),
        "registry_bytes": registry_path.read_bytes(),
        "proposed_project": proposed_project,
        "proposed_registry": proposed_registry,
        "project_changes": project_changes,
        "registry_changes": registry_changes,
        "ssh_alias": ssh_alias,
        "ssh_config": ssh_config,
        "known_hosts": known_hosts,
        "local_ssh": local_ssh,
        "inventory": target,
    }


def _controller_assessment(
    context: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    result = call_controller(
        context["config"],
        "assess-server-retirement",
        timeout=args.timeout,
        action_args=("--server", context["server"]),
        payload={"schema_version": 1, "server": context["inventory"]},
    )
    if (
        result.get("schema_version") != 1
        or result.get("server") != context["server"]
        or not isinstance(result.get("blockers"), list)
        or not isinstance(result.get("attention"), list)
    ):
        raise RuntimeError("controller returned an invalid retirement assessment")
    return result


def _effective_blockers(
    assessment: dict[str, Any], *, allow_unreachable: bool
) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in assessment["blockers"]
        if not (allow_unreachable and item.get("code") == "server_unreachable")
    ]


def _archive_inspection(
    context: dict[str, Any], args: argparse.Namespace
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = context["config"]
    if config.output_sync is None:
        return {"status": "not_configured", "public_keys": []}, []
    try:
        inspection = inspect_or_apply_archive_source(
            config.output_sync,
            context["server"],
            apply=False,
            timeout=args.timeout,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}, [
            {"code": "archive_cleanup_unavailable", "detail": str(exc)}
        ]
    return inspection, []


def assess(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    context = _context(args)
    assessment = _controller_assessment(context, args)
    archive, archive_blockers = _archive_inspection(context, args)
    blockers = (
        _effective_blockers(
            assessment,
            allow_unreachable=bool(getattr(args, "allow_unreachable", False)),
        )
        + archive_blockers
    )
    result = {
        "schema_version": RETIREMENT_SCHEMA,
        "server": context["server"],
        "ready": not blockers,
        "applied": False,
        "status": "ready_to_retire" if not blockers else "blocked",
        "assessment": {**assessment, "effective_blockers": blockers},
        "cleanup": {
            "project_config": {
                "path": str(context["config_path"]),
                "changes": context["project_changes"],
            },
            "global_registry": {
                "path": str(context["registry_path"]),
                "changes": context["registry_changes"],
            },
            "local_ssh": {
                "config": str(context["ssh_config"]),
                "known_hosts": str(context["known_hosts"]),
                "host_block": context["local_ssh"]["block"],
                "known_host_records": context["local_ssh"]["known_host_records"],
                "shared_identity_files_preserved": context["local_ssh"]["block"][
                    "identity_files"
                ],
            },
            "archive_source": archive,
        },
        "preserved": [
            "controller drain and historical run records",
            "remote runtime and output data",
            "shared local SSH identity files",
        ],
    }
    return result, context


def _revoke_source_keys(
    ssh_alias: str,
    public_keys: list[object],
    *,
    timeout: int,
) -> dict[str, Any]:
    blobs: list[str] = []
    for value in public_keys:
        if not isinstance(value, str):
            raise ValueError("archive retirement public key is invalid")
        fields = value.split()
        if len(fields) < 2:
            raise ValueError("archive retirement public key is invalid")
        blobs.append(fields[1])
    if not blobs:
        return {"status": "not_configured", "removed": 0}
    payload = json.dumps(sorted(set(blobs)))
    program = (
        f"KEY_BLOBS = set({payload})\n"
        + r"""
import json
import os
from pathlib import Path
import stat
import tempfile

path = Path.home() / ".ssh" / "authorized_keys"
if not path.is_file():
    print(json.dumps({"ok": True, "removed": 0, "status": "already_absent"}))
    raise SystemExit(0)
original = path.read_text(encoding="utf-8")
kept = []
removed = 0
for line in original.splitlines(keepends=True):
    if KEY_BLOBS.intersection(line.split()):
        removed += 1
    else:
        kept.append(line)
if removed:
    descriptor, temporary_text = tempfile.mkstemp(prefix=".authorized_keys.", dir=path.parent)
    temporary = Path(temporary_text)
    try:
        os.fchmod(descriptor, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("".join(kept))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
print(json.dumps({"ok": True, "removed": removed, "status": "revoked" if removed else "already_absent"}))
"""
    )
    completed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={timeout}",
            ssh_alias,
            "python3 -",
        ],
        input=program,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout + 15,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("source key revocation returned invalid JSON") from exc
    if (
        completed.returncode != 0
        or not isinstance(result, dict)
        or result.get("ok") is not True
    ):
        raise RuntimeError(completed.stderr.strip() or "source key revocation failed")
    return result


def _apply_local_ssh(context: dict[str, Any]) -> dict[str, Any]:
    plan = context["local_ssh"]
    changes: dict[str, str] = {}
    ssh_config = context["ssh_config"]
    if ssh_config.is_file() and plan["original_config"] != plan["proposed_config"]:
        if ssh_config.read_text(encoding="utf-8") != plan["original_config"]:
            raise RuntimeError("local SSH config changed during retirement")
        replace_config_text(ssh_config, plan["proposed_config"])
        changes["ssh_config"] = "removed_host_block"
    known_hosts = context["known_hosts"]
    if (
        known_hosts.is_file()
        and plan["original_known_hosts"] != plan["proposed_known_hosts"]
    ):
        if known_hosts.read_text(encoding="utf-8") != plan["original_known_hosts"]:
            raise RuntimeError("local known_hosts changed during retirement")
        replace_config_text(known_hosts, plan["proposed_known_hosts"])
        changes["known_hosts"] = "removed_host_keys"
    return changes


def retire(args: argparse.Namespace) -> dict[str, Any]:
    preview, context = assess(args)
    if not args.apply:
        return preview
    blockers = preview["assessment"]["effective_blockers"]
    if blockers:
        raise RetirementBlockedError(blockers)

    controller = update_server_drain(args, drained=True)
    reassessment = _controller_assessment(context, args)
    effective = _effective_blockers(
        reassessment,
        allow_unreachable=bool(getattr(args, "allow_unreachable", False)),
    )
    if effective:
        raise RetirementBlockedError(effective)

    archive_preview = preview["cleanup"]["archive_source"]
    config = context["config"]
    if config.output_sync is not None:
        archive_preview = inspect_or_apply_archive_source(
            config.output_sync,
            context["server"],
            apply=False,
            timeout=args.timeout,
        )
    public_keys = archive_preview.get("public_keys", [])
    try:
        source_authorization = _revoke_source_keys(
            context["ssh_alias"], public_keys, timeout=args.timeout
        )
    except (OSError, RuntimeError, ValueError) as exc:
        if not getattr(args, "allow_unreachable", False):
            raise
        source_authorization = {
            "status": "unreachable_private_key_will_be_removed",
            "error": str(exc),
            "removed": 0,
        }
    archive = archive_preview
    if config.output_sync is not None:
        archive = inspect_or_apply_archive_source(
            config.output_sync,
            context["server"],
            apply=True,
            timeout=args.timeout,
            expected_digest=archive_preview.get("state_digest"),
        )

    local_ssh = _apply_local_ssh(context)
    _unchanged(context["config_path"], context["project_bytes"])
    _unchanged(context["registry_path"], context["registry_bytes"])
    try:
        replace_config_yaml(context["registry_path"], context["proposed_registry"])
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"server {context['server']!r} is drained and connection credentials "
            f"were cleaned, but the global registry was not updated: {exc}"
        ) from exc
    try:
        replace_config_yaml(context["config_path"], context["proposed_project"])
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"server {context['server']!r} is drained and removed from the global "
            f"registry, but the project config was not updated: {exc}"
        ) from exc
    return {
        **preview,
        "ready": True,
        "applied": True,
        "status": "retired",
        "assessment": {**reassessment, "effective_blockers": []},
        "controller": controller,
        "source_authorization": source_authorization,
        "archive_cleanup": archive,
        "local_ssh_cleanup": local_ssh,
    }


def request_server_retirement(
    args: argparse.Namespace,
    server: str,
) -> dict[str, Any]:
    retire_args = argparse.Namespace(**vars(args))
    retire_args.server = server
    retire_args.apply = True
    retire_args.allow_unreachable = False
    return retire(retire_args)


def request_server_retirement_preview(
    args: argparse.Namespace,
    server: str,
) -> dict[str, Any]:
    retire_args = argparse.Namespace(**vars(args))
    retire_args.server = server
    retire_args.apply = False
    retire_args.allow_unreachable = False
    return assess(retire_args)[0]
