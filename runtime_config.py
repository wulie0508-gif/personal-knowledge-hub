"""Runtime configuration for the local-first second-brain system.

Code remains inside the repository. User data, indexes, queues and logs can
live in ``SECOND_BRAIN_HOME`` so a repository is safe to share without
copying a person's vault or credentials. If the variable is absent, the
legacy ``./data`` directory remains the runtime home for backward
compatibility.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
LEGACY_RUNTIME_HOME = PROJECT_ROOT / "data"
DEFAULT_VAULT = Path.home() / "Documents" / "Obsidian Vault"
RUNTIME_HOME_ENV = "SECOND_BRAIN_HOME"
VAULT_ENV = "SECOND_BRAIN_VAULT"


def runtime_home() -> Path:
    """Return the private runtime root without creating or moving anything."""

    value = os.environ.get(RUNTIME_HOME_ENV, "").strip()
    return Path(value).expanduser() if value else LEGACY_RUNTIME_HOME


def config_path() -> Path:
    return runtime_home() / "config.json"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def configured_vault() -> Path:
    """Resolve the vault from environment, private runtime config, then legacy default."""

    configured = os.environ.get(VAULT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    value = str(load_json(config_path(), {}).get("vault_path") or "").strip()
    return Path(value).expanduser() if value else DEFAULT_VAULT


def private_path(*parts: str) -> Path:
    return runtime_home().joinpath(*parts)


def ensure_runtime_layout() -> dict[str, Path]:
    """Create empty private runtime directories; never migrates existing data."""

    root = runtime_home()
    layout = {
        "root": root,
        "metadata": root / "metadata",
        "indexes": root / "indexes",
        "queues": root / "queues",
        "reports": root / "reports",
        "logs": root / "logs",
        "cache": root / "cache",
        "backups": root / "backups",
        "secrets": root / "secrets",
    }
    for path in layout.values():
        path.mkdir(parents=True, exist_ok=True)
    return layout


def runtime_summary() -> dict[str, str]:
    return {
        "project_root": str(PROJECT_ROOT),
        "runtime_home": str(runtime_home()),
        "vault_path": str(configured_vault()),
        "mode": "external" if runtime_home() != LEGACY_RUNTIME_HOME else "legacy_compatible",
    }
