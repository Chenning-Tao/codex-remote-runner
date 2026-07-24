from __future__ import annotations

import argparse
from typing import Any

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import resolve_project_config


def configure(args: argparse.Namespace) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    if config.output_sync is None:
        raise ValueError("project config does not define output_sync")
    return call_controller(
        config,
        "configure-output-sync",
        timeout=args.timeout,
        payload=config.output_sync.to_payload(),
    )
