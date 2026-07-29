#!/usr/bin/env python3
"""Apply an ordered, reviewed corpus-identity ruleset with backups.

Rules live in the private runtime, not in the public repository. Each rule
matches a note's original ``source_path``/``source`` by prefix or suffix and
assigns an explicit namespace plus identity metadata. Dry-run is the default.
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


def _normalise(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/").casefold()


def _matches(source: str, note_path: Path, rule: dict[str, Any]) -> bool:
    value = _normalise(source)
    path_value = _normalise(note_path)
    prefixes = [_normalise(item) for item in rule.get("source_prefixes") or []]
    suffixes = [_normalise(item) for item in rule.get("source_suffixes") or []]
    path_contains = [
        _normalise(item) for item in rule.get("note_path_contains") or []
    ]
    if prefixes and not any(
        value == prefix or value.startswith(prefix + "/")
        for prefix in prefixes
    ):
        return False
    if suffixes and not any(value.endswith(suffix) for suffix in suffixes):
        return False
    if path_contains and not all(item in path_value for item in path_contains):
        return False
    return bool(value) and bool(prefixes or suffixes or path_contains)


def _identity_fields(rule: dict[str, Any]) -> dict[str, Any]:
    namespace = knowledge_schema.normalize_namespace(rule.get("namespace"))
    if not namespace or namespace == knowledge_schema.SOURCE_ARCHIVE:
        raise ValueError(f"unsupported target namespace: {rule.get('namespace')}")
    defaults = knowledge_schema.DEFAULTS[namespace]
    fields: dict[str, Any] = {
        "corpus_namespace": namespace,
        "authorship": rule.get("authorship", defaults["authorship"]),
        "confidentiality": rule.get(
            "confidentiality",
            defaults["confidentiality"],
        ),
        "engagement_status": rule.get(
            "engagement_status",
            defaults["engagement_status"],
        ),
        "stance": rule.get("stance", defaults["stance"]),
        "persona_influence": rule.get(
            "persona_influence",
            defaults["persona_influence"],
        ),
        "identity_reviewed_at": now_text(),
        "identity_review_basis": rule.get(
            "review_basis",
            "reviewed_ruleset",
        ),
    }
    if namespace != knowledge_schema.PERSONAL_MEMORY:
        fields["persona_influence"] = 0.0
    return fields


def migrate(
    *,
    vault: Path,
    rules: list[dict[str, Any]],
    apply: bool = False,
) -> dict[str, Any]:
    article_root = vault / "10_Sources" / "Local" / "Articles"
    notes = knowledge_graph.read_notes([article_root])
    assignments: list[
        tuple[knowledge_graph.Note, str, dict[str, Any], dict[str, Any]]
    ] = []
    rule_counts: dict[str, int] = {}
    unmatched = 0
    for note in notes:
        text = note.path.read_text(encoding="utf-8", errors="replace")
        source = str(
            knowledge_graph._field(text, "source_path")
            or knowledge_graph._field(text, "source")
            or ""
        )
        matched_rule = next(
            (rule for rule in rules if _matches(source, note.path, rule)),
            None,
        )
        if not matched_rule:
            unmatched += 1
            continue
        fields = _identity_fields(matched_rule)
        changed = any(
            str(knowledge_graph._field(text, name, "")) != str(value)
            for name, value in fields.items()
            if name not in {"identity_reviewed_at"}
        )
        if not changed:
            continue
        rule_id = str(matched_rule.get("id") or "unnamed")
        rule_counts[rule_id] = rule_counts.get(rule_id, 0) + 1
        assignments.append((note, text, matched_rule, fields))

    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    backup_root = runtime_config.private_path(
        "backups",
        f"corpus-identity-{stamp}",
    )
    changed_paths: list[str] = []
    if apply:
        for note, text, _rule, fields in assignments:
            relative = note.path.relative_to(article_root)
            backup = backup_root / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(note.path, backup)
            for name, value in fields.items():
                text = refresh_knowledge_value.replace_frontmatter(
                    text,
                    name,
                    value,
                )
            note.path.write_text(text, encoding="utf-8")
            changed_paths.append(str(note.path))

    report = {
        "generated_at": now_text(),
        "mode": "apply" if apply else "dry_run",
        "candidate_count": len(assignments),
        "changed_count": len(changed_paths),
        "unmatched_count": unmatched,
        "rule_counts": rule_counts,
        "backup_root": str(backup_root) if apply else "",
        "changed_paths": changed_paths,
    }
    report_path = runtime_config.private_path(
        "reports",
        "corpus-identity-migration.json",
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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--vault",
        default=str(runtime_config.configured_vault()),
    )
    parser.add_argument("--rules", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    rules_payload = json.loads(
        Path(args.rules).read_text(encoding="utf-8")
    )
    rules = rules_payload.get("rules") if isinstance(rules_payload, dict) else None
    if not isinstance(rules, list) or not rules:
        parser.error("rules file must contain a non-empty 'rules' list")
    result = migrate(
        vault=Path(args.vault).expanduser(),
        rules=rules,
        apply=bool(args.apply),
    )
    print(
        json.dumps(
            {
                "mode": result["mode"],
                "candidate_count": result["candidate_count"],
                "changed_count": result["changed_count"],
                "rule_counts": result["rule_counts"],
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
