from __future__ import annotations

import argparse

from .config import load_managed_project_config
from .execution_registry import resolve_project_config
from .pool import (
    normalize_candidate_servers,
    normalize_explicit_server,
    probe_project_pool,
)
from .preparation_manifest import build_preparation_manifest
from .source import prepare_revision
from .submission import (
    prepared_server_manifest,
    reachable_targets,
    resolve_source_repo,
)


def prepare(args: argparse.Namespace) -> dict[str, object]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    source_repo = resolve_source_repo(config.local_repo, args.source_repo)
    server_registry = args.server_registry.expanduser().resolve(strict=True)
    requested_server = normalize_explicit_server(getattr(args, "server", None))
    candidate_servers = normalize_candidate_servers(
        getattr(args, "candidate_servers", None)
    )
    pool = probe_project_pool(
        config,
        server_registry,
        explicit_server=requested_server,
        ssh_profile=args.ssh_profile,
        timeout=args.timeout,
        candidate_servers=candidate_servers,
    )
    targets, candidates = reachable_targets(pool, explicit_server=requested_server)
    preparation = prepare_revision(
        source_repo,
        project_id=config.project_id,
        targets=targets,
        explicit_server=requested_server,
        timeout=args.prepare_timeout,
    )
    return build_preparation_manifest(
        config=config,
        server_registry_path=server_registry,
        preparation=preparation,
        prepared_servers=prepared_server_manifest(preparation, candidates),
    )
