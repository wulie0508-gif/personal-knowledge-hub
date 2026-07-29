#!/usr/bin/env python3
"""Safely make previously reviewed personal imports explicit.

Legacy local imports were historically inferred as ``personal_memory``. This
tool does not trust that inference. It only upgrades notes whose original
``source_path`` matches a source root or URI prefix supplied by the user.
Dry-run is the default; apply mode creates recoverable backups first.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import knowledge_graph
import knowledge_schema
import refresh_knowledge_value
import runtime_config


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalise(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/").casefold()


def source_allowed(
    source: str,
    roots: list[str],
    prefixes: list[str],
) -> bool:
    value = _normalise(source)
    if not value:
        return False
    for root in roots:
        allowed = _normalise(root)
        if value == allowed or value.startswith(allowed + "/"):
            return True
    return any(value.startswith(_normalise(prefix)) for prefix in prefixes)


def migrate(
    *,
    vault: Path,
    source_roots: list[str],
    source_prefixes: list[str],
    apply: bool = False,
    persona_influence: float = 0.85,
) -> dict[str, Any]:
    article_root = vault / "10_Sources" / "Local" / "Articles"
    notes = knowledge_graph.read_notes([article_root])
    candidates: list[tuple[knowledge_graph.Note, str]] = []
    skipped_explicit = 0
    skipped_unmatched = 0
    for note in notes:
        text = note.path.read_text(encoding="utf-8", errors="replace")
        explicit = knowledge_schema.normalize_namespace(
            knowledge_graph._field(text, "corpus_namespace")
        )
        if explicit:
            skipped_explicit += 1
            continue
        source = str(
            knowledge_graph._field(text, "source_path")
            or knowledge_graph._field(text, "source")
            or ""
        )
        if (
            note.corpus_namespace == knowledge_schema.PERSONAL_MEMORY
            and source_allowed(source, source_roots, source_prefixes)
        ):
            candidates.append((note, source))
        else:
            skipped_unmatched += 1

    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    backup_root = runtime_config.private_path(
        "backups",
        f"personal-identity-{stamp}",
    )
    changed: list[str] = []
    if apply:
        for note, _source in candidates:
            relative = note.path.relative_to(article_root)
            backup = backup_root / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(note.path, backup)
            text = note.path.read_text(encoding="utf-8", errors="replace")
            fields: dict[str, Any] = {
                "corpus_namespace": knowledge_schema.PERSONAL_MEMORY,
                "authorship": "self",
                "confidentiality": "private",
                "engagement_status": "read",
                "persona_influence": max(
                    0.0,
                    min(float(persona_influence), 1.0),
                ),
                "identity_reviewed_at": now_text(),
                "identity_review_basis": "user_confirmed_source_root",
            }
            for name, value in fields.items():
                text = refresh_knowledge_value.replace_frontmatter(
                    text,
                    name,
                    value,
                )
            note.path.write_text(text, encoding="utf-8")
            changed.append(str(note.path))

    report = {
        "generated_at": now_text(),
        "mode": "apply" if apply else "dry_run",
        "candidate_count": len(candidates),
        "changed_count": len(changed),
        "skipped_explicit_count": skipped_explicit,
        "skipped_unmatched_count": skipped_unmatched,
        "backup_root": str(backup_root) if apply else "",
        "criteria": {
            "source_roots": source_roots,
            "source_prefixes": source_prefixes,
            "persona_influence": persona_influence,
        },
        "candidate_paths": [str(note.path) for note, _ in candidates],
        "changed_paths": changed,
    }
    report_path = runtime_config.private_path(
        "reports",
        "personal-identity-migration.json",
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(report_path)
    return {"report_path": str(report_path), **report}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Explicitly classify reviewed legacy local imports as personal. "
            "Dry-run is the default."
        )
    )
    parser.add_argument(
        "--vault",
        default=str(runtime_config.configured_vault()),
    )
    parser.add_argument(
        "--source-root",
        action="append",
        default=[],
        help="Approved original source directory; may be repeated.",
    )
    parser.add_argument(
        "--source-prefix",
        action="append",
        default=[],
        help="Approved non-file source prefix; may be repeated.",
    )
    parser.add_argument("--persona-influence", type=float, default=0.85)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.source_root and not args.source_prefix:
        parser.error("provide at least one --source-root or --source-prefix")
    result = migrate(
        vault=Path(args.vault).expanduser(),
        source_roots=list(args.source_root),
        source_prefixes=list(args.source_prefix),
        apply=bool(args.apply),
        persona_influence=float(args.persona_influence),
    )
    print(
        json.dumps(
            {
                "mode": result["mode"],
                "candidate_count": result["candidate_count"],
                "changed_count": result["changed_count"],
                "backup_root": result["backup_root"],
                "report_path": result["report_path"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
