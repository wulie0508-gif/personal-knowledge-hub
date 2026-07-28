#!/usr/bin/env python3
"""Move Codex-confirmed very-low-value notes into the recoverable local trash."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import app
import knowledge_pipeline


AUTO_REMOVE_MAX_SCORE = 29


def run() -> dict[str, Any]:
    jobs = app.load_jobs()
    jobs_by_url = {
        knowledge_pipeline.history_source.canonical_article_url(
            str(job.get("url") or "")
        ): job
        for job in jobs
        if job.get("kind") == "wechat_mp"
    }
    removed: list[dict[str, Any]] = []
    for item in knowledge_pipeline.current_results_from_notes():
        try:
            note_text = Path(item.note_path).read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            continue
        if (
            item.knowledge_priority != "回收建议"
            or item.knowledge_value_score > AUTO_REMOVE_MAX_SCORE
            or knowledge_pipeline.frontmatter_field(
                note_text,
                "curation_provider",
            ).lower()
            != "codex"
        ):
            continue
        canonical = knowledge_pipeline.history_source.canonical_article_url(
            item.source_url
        )
        job = jobs_by_url.get(canonical) or {
            "kind": "wechat_mp",
            "url": item.source_url,
            "title": item.title,
            "output_file": item.note_path,
        }
        removal = app.move_job_article_to_trash(
            job,
            record_user_feedback=False,
        )
        app.update_jobs_for_url(
            item.source_url,
            status="removed",
            feedback="auto_remove",
            message=(
                f"Codex 判断长期价值仅 {item.knowledge_value_score}/100，"
                "已自动移入可恢复的本地回收区"
            ),
            output_file="",
            trash_path=removal["trash_path"],
        )
        removed.append(
            {
                "title": item.title,
                "score": item.knowledge_value_score,
                "trash_path": removal["trash_path"],
            }
        )
    report = app.regenerate_knowledge_views()
    return {
        "removed_count": len(removed),
        "removed": removed,
        "validation": report.get("status"),
        "remaining_notes": report.get("note_count"),
    }


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(run(), ensure_ascii=False, indent=2))
