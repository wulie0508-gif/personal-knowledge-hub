#!/usr/bin/env python3
"""Plan or copy a legacy runtime into an external private data directory.

The default command is read-only. ``--copy`` performs a copy, never a move,
so the existing local knowledge base remains recoverable until the user has
verified the new installation.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import runtime_config


def directory_size(path: Path) -> int:
    return sum(
        item.stat().st_size
        for item in path.rglob("*")
        if item.is_file()
    )


def plan(source: Path, destination: Path) -> dict[str, object]:
    entries = []
    for item in sorted(source.iterdir()) if source.is_dir() else []:
        entries.append(
            {
                "name": item.name,
                "kind": "directory" if item.is_dir() else "file",
                "bytes": directory_size(item) if item.is_dir() else item.stat().st_size,
                "exists_at_destination": (destination / item.name).exists(),
            }
        )
    return {
        "source": str(source),
        "destination": str(destination),
        "source_exists": source.is_dir(),
        "same_location": source.resolve() == destination.resolve() if source.exists() else False,
        "entries": entries,
        "total_bytes": sum(int(item["bytes"]) for item in entries),
        "operation": "copy_only_no_deletion",
    }


def copy_runtime(source: Path, destination: Path) -> list[str]:
    if source.resolve() == destination.resolve():
        raise ValueError("destination must be different from the legacy runtime directory")
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for item in source.iterdir():
        target = destination / item.name
        if target.exists():
            raise FileExistsError(f"destination already contains {target.name}; nothing was overwritten")
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
        copied.append(item.name)
    return copied


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", required=True, help="new SECOND_BRAIN_HOME")
    parser.add_argument("--copy", action="store_true", help="copy after showing a safe plan")
    args = parser.parse_args()

    source = runtime_config.LEGACY_RUNTIME_HOME
    destination = Path(args.destination).expanduser()
    report = plan(source, destination)
    if args.copy:
        if not report["source_exists"]:
            raise FileNotFoundError(source)
        report["copied"] = copy_runtime(source, destination)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
