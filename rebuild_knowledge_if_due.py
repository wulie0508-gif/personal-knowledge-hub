#!/usr/bin/env python3
"""Rebuild knowledge views only when accumulated changes justify the cost."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import runtime_config


ROOT = Path(__file__).resolve().parent
DATA_DIR = runtime_config.runtime_home()
STATE_PATH = DATA_DIR / "reports" / "knowledge-rebuild-state.json"
VALIDATION_PATH = DATA_DIR / "reports" / "knowledge-validation.json"
SELECTION_PATH = DATA_DIR / "wechat-text-selection" / "selection-report.json"
COMPLETED_DIR = DATA_DIR / "codex-curation-queue" / "completed"
SELECTION_DIGEST_VERSION = 2


def now() -> datetime:
    return datetime.now().astimezone()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def completed_count() -> int:
    if not COMPLETED_DIR.is_dir():
        return 0
    return sum(1 for path in COMPLETED_DIR.glob("*.json") if path.is_file())


def selection_digest() -> str:
    report = read_json(SELECTION_PATH, {})
    rows: set[tuple[str, str]] = set()
    for account, entry in sorted((report.get("accounts") or {}).items()):
        for item in entry.get("items") or []:
            source_url = str(item.get("source_url") or "")
            if source_url:
                rows.add((str(account), source_url))
    payload = json.dumps(
        sorted(rows),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validation_generated_at() -> str:
    report = read_json(VALIDATION_PATH, {})
    return str((report.get("validation") or {}).get("generated_at") or "")


def parsed_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def current_snapshot() -> dict[str, Any]:
    return {
        "completed_count": completed_count(),
        "selection_digest": selection_digest(),
        "validation_generated_at": validation_generated_at(),
    }


def save_state(snapshot: dict[str, Any], action: str, reasons: list[str]) -> None:
    write_json_atomic(
        STATE_PATH,
        {
            "updated_at": now().isoformat(timespec="seconds"),
            "last_action": action,
            "last_reasons": reasons,
            "selection_digest_version": SELECTION_DIGEST_VERSION,
            "last_completed_count": int(snapshot["completed_count"]),
            "last_selection_digest": str(snapshot["selection_digest"]),
            "last_validation_generated_at": str(
                snapshot["validation_generated_at"]
            ),
        },
    )


def due_reasons(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    min_curation_delta: int,
    max_age_hours: float,
) -> list[str]:
    reasons: list[str] = []
    previous_count = int(state.get("last_completed_count") or 0)
    if int(snapshot["completed_count"]) - previous_count >= min_curation_delta:
        reasons.append("curation_delta")
    if (
        snapshot["selection_digest"]
        and snapshot["selection_digest"] != state.get("last_selection_digest")
    ):
        reasons.append("selection_changed")
    generated = parsed_datetime(str(snapshot["validation_generated_at"]))
    if generated is None:
        reasons.append("validation_missing")
    elif now() - generated >= timedelta(hours=max_age_hours):
        reasons.append("validation_stale")
    return reasons


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-curation-delta", type=int, default=50)
    parser.add_argument("--max-age-hours", type=float, default=6)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    snapshot = current_snapshot()
    state = read_json(STATE_PATH, {})
    if not state:
        save_state(snapshot, "initialized", ["existing_validation_adopted"])
        print(
            json.dumps(
                {"action": "initialized", "rebuilt": False, **snapshot},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if int(state.get("selection_digest_version") or 0) < SELECTION_DIGEST_VERSION:
        save_state(
            snapshot,
            "migrated",
            ["selection_digest_canonicalized"],
        )
        print(
            json.dumps(
                {
                    "action": "migrated",
                    "rebuilt": False,
                    "reasons": ["selection_digest_canonicalized"],
                    **snapshot,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    reasons = due_reasons(
        state,
        snapshot,
        max(1, args.min_curation_delta),
        max(0.25, args.max_age_hours),
    )
    if args.force:
        reasons.append("forced")
    if not reasons:
        print(
            json.dumps(
                {"action": "skipped", "rebuilt": False, **snapshot},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    import app

    app.regenerate_knowledge_views()
    snapshot = current_snapshot()
    save_state(snapshot, "rebuilt", reasons)
    print(
        json.dumps(
            {
                "action": "rebuilt",
                "rebuilt": True,
                "reasons": reasons,
                **snapshot,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
