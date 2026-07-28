#!/usr/bin/env python3
"""Audit corpus identity without changing a user's notes."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import knowledge_graph
import knowledge_schema
import runtime_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default=str(runtime_config.configured_vault()))
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    vault = Path(args.vault).expanduser()
    roots = [
        vault / "10_Sources" / "WeChat" / "Articles",
        vault / "10_Sources" / "Local" / "Articles",
        vault / "10_Sources" / "Enterprise" / "Articles",
        vault / "10_Sources" / "Xiaohongshu",
        vault / "10_Sources" / "Feishu",
    ]
    notes = knowledge_graph.read_notes(roots)
    explicit = Counter()
    inferred = Counter()
    review_paths: list[str] = []
    for note in notes:
        text = note.path.read_text(encoding="utf-8", errors="replace")
        value = knowledge_graph._field(text, "corpus_namespace")
        (explicit if knowledge_schema.normalize_namespace(value) else inferred)[
            note.corpus_namespace
        ] += 1
        if note.platform == "local" and not knowledge_schema.normalize_namespace(value):
            review_paths.append(str(note.path))
    report = {
        "vault": str(vault),
        "total_notes": len(notes),
        "explicit_namespace_counts": dict(explicit),
        "inferred_namespace_counts": dict(inferred),
        "manual_review_required": {
            "reason": "legacy local imports default to personal_memory until explicitly classified",
            "count": len(review_paths),
            "paths": review_paths,
        },
        "safe_next_step": "Review only the listed local imports, then add corpus_namespace explicitly. This audit did not modify notes.",
    }
    destination = Path(args.output) if args.output else runtime_config.private_path("reports", "corpus-namespace-audit.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(destination), **report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
