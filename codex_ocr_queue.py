#!/usr/bin/env python3
"""Queue local article images for Codex vision and safely write results back."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

import ocr_provider
import runtime_config


ROOT = Path(__file__).resolve().parent
DATA_DIR = runtime_config.runtime_home()
QUEUE_ROOT = DATA_DIR / "codex-ocr-queue"
PENDING_DIR = QUEUE_ROOT / "pending"
COMPLETED_DIR = QUEUE_ROOT / "completed"
FAILED_DIR = QUEUE_ROOT / "failed"
RESULT_DIR = DATA_DIR / "codex-ocr-results"
DEFAULT_ARTICLE_ROOT = runtime_config.configured_vault() / "10_Sources" / "WeChat" / "Articles"
START_MARKER = "<!-- codex-ocr:start -->"
END_MARKER = "<!-- codex-ocr:end -->"


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def field(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", text, re.MULTILINE)
    if not match:
        return ""
    raw = match.group(1).strip()
    try:
        value = json.loads(raw)
        return str(value) if value is not None else ""
    except json.JSONDecodeError:
        return raw.strip("\"'")


def local_images(note: Path, text: str) -> list[str]:
    images: list[str] = []
    for raw in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        decoded = urllib.parse.unquote(raw)
        if decoded.startswith(("http://", "https://")):
            continue
        candidate = (note.parent / decoded).resolve()
        if candidate.is_file() and candidate.suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        }:
            images.append(str(candidate))
    return list(dict.fromkeys(images))


def job_id(note: Path, source_url: str) -> str:
    identity = f"{note.resolve()}\n{source_url}"
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]


def enqueue_note(note: Path) -> dict[str, Any] | None:
    if not note.is_file():
        return None
    text = note.read_text(encoding="utf-8", errors="replace")
    status = field(text, "ocr_status")
    if status not in {"pending", "pending_model", "pending_codex", "error"}:
        return None
    images = local_images(note, text)
    if not images:
        return None
    source_url = field(text, "sourceUrl") or field(text, "source")
    identifier = job_id(note, source_url)
    target = PENDING_DIR / f"{identifier}.json"
    existing: dict[str, Any] = {}
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    job = {
        "id": identifier,
        "created_at": existing.get("created_at") or now_text(),
        "updated_at": now_text(),
        "title": field(text, "title") or note.stem,
        "source_url": source_url,
        "note_path": str(note.resolve()),
        "image_paths": images,
        "status": "pending",
        "attempts": int(existing.get("attempts") or 0),
        "last_error": str(existing.get("last_error") or ""),
        "instructions": (
            "Use Codex vision to extract knowledge-relevant text. Ignore browser UI, "
            "QR codes, decorative text and ads. Never invent text that is not visible."
        ),
    }
    atomic_json(target, job)
    return job


def sync(article_root: Path = DEFAULT_ARTICLE_ROOT) -> dict[str, Any]:
    queued = 0
    scanned = 0
    if article_root.exists():
        for note in article_root.rglob("*.md"):
            scanned += 1
            if enqueue_note(note):
                queued += 1
    return {"scanned": scanned, "pending": len(list(PENDING_DIR.glob("*.json"))), "queued": queued}


def list_jobs(limit: int = 10) -> list[dict[str, Any]]:
    prune_stale()
    jobs: list[dict[str, Any]] = []
    for path in sorted(PENDING_DIR.glob("*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not Path(str(job.get("note_path") or "")).is_file():
            continue
        job["manifest_path"] = str(path)
        jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


def prune_stale() -> int:
    removed = 0
    for path in PENDING_DIR.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            note = Path(str(job.get("note_path") or ""))
        except (json.JSONDecodeError, OSError):
            note = Path()
        if not str(note) or not note.is_file():
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def _replace_frontmatter(text: str, name: str, value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False)
    pattern = rf"^{re.escape(name)}:\s*.*$"
    replacement = f"{name}: {rendered}"
    if re.search(pattern, text, re.MULTILINE):
        return re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)
    end = text.find("\n---", 3)
    if text.startswith("---") and end >= 0:
        return text[:end] + f"\n{replacement}" + text[end:]
    return text


def complete_job(
    identifier: str,
    result_file: Path,
    *,
    provider: str = "codex_vision",
) -> dict[str, Any]:
    manifest = PENDING_DIR / f"{identifier}.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"OCR 任务不存在：{identifier}")
    job = json.loads(manifest.read_text(encoding="utf-8"))
    note = Path(str(job["note_path"]))
    if not note.is_file():
        raise FileNotFoundError(f"文章笔记不存在：{note}")
    result = result_file.read_text(encoding="utf-8", errors="replace").strip()
    if not result:
        raise ValueError("OCR 结果为空")
    text = note.read_text(encoding="utf-8", errors="replace")
    text = re.sub(
        rf"\n?{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}\n?",
        "\n",
        text,
        flags=re.DOTALL,
    ).rstrip()
    block = "\n".join(
        [
            START_MARKER,
            "## 图片 OCR（Codex 视觉）" if provider == "codex_vision" else "## 图片 OCR（本地兜底）",
            "",
            result,
            "",
            END_MARKER,
        ]
    )
    if "<!-- knowledge-links:start -->" in text:
        text = text.replace(
            "<!-- knowledge-links:start -->",
            block + "\n\n<!-- knowledge-links:start -->",
            1,
        )
    else:
        text += "\n\n" + block
    compact_length = len(re.sub(r"\s+", "", result))
    text = _replace_frontmatter(text, "ocr_status", "complete")
    text = _replace_frontmatter(text, "ocr_provider", provider)
    text = _replace_frontmatter(text, "ocr_length", compact_length)
    note.write_text(text.rstrip() + "\n", encoding="utf-8")
    job.update(
        status="completed",
        completed_at=now_text(),
        updated_at=now_text(),
        provider=provider,
        ocr_length=compact_length,
        result_file=str(result_file.resolve()),
    )
    destination = COMPLETED_DIR / manifest.name
    atomic_json(destination, job)
    manifest.unlink()
    return job


def fail_job(identifier: str, error: str) -> dict[str, Any]:
    manifest = PENDING_DIR / f"{identifier}.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"OCR 任务不存在：{identifier}")
    job = json.loads(manifest.read_text(encoding="utf-8"))
    job["attempts"] = int(job.get("attempts") or 0) + 1
    job["last_error"] = error
    job["updated_at"] = now_text()
    job["fallback_due"] = job["attempts"] >= 2
    atomic_json(manifest, job)
    return job


def fallback_job(identifier: str) -> dict[str, Any]:
    manifest = PENDING_DIR / f"{identifier}.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"OCR 任务不存在：{identifier}")
    job = json.loads(manifest.read_text(encoding="utf-8"))
    paths = [Path(value) for value in job.get("image_paths") or []]
    results = ocr_provider.local_recognize(paths)
    rows: list[str] = []
    for index, result in enumerate(results, 1):
        if result.text.strip():
            rows.extend([f"### 图 {index}", "", result.text.strip(), ""])
    if not rows:
        failed_target = FAILED_DIR / manifest.name
        job.update(status="failed", updated_at=now_text(), last_error="本地 OCR 结果为空")
        atomic_json(failed_target, job)
        manifest.unlink()
        raise RuntimeError("本地 OCR 结果为空")
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    result_file = RESULT_DIR / f"{identifier}-paddle.md"
    result_file.write_text("\n".join(rows).strip() + "\n", encoding="utf-8")
    return complete_job(identifier, result_file, provider="paddle")


def status() -> dict[str, Any]:
    prune_stale()
    return {
        "pending_count": len(list(PENDING_DIR.glob("*.json"))),
        "completed_count": len(list(COMPLETED_DIR.glob("*.json"))),
        "failed_count": len(list(FAILED_DIR.glob("*.json"))),
        "queue_path": str(QUEUE_ROOT),
        **ocr_provider.provider_status(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex 视觉 OCR 队列")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("--article-root", type=Path, default=DEFAULT_ARTICLE_ROOT)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--limit", type=int, default=10)
    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--job", required=True)
    complete_parser.add_argument("--result-file", type=Path, required=True)
    fail_parser = subparsers.add_parser("fail")
    fail_parser.add_argument("--job", required=True)
    fail_parser.add_argument("--error", required=True)
    fallback_parser = subparsers.add_parser("fallback")
    fallback_parser.add_argument("--job", required=True)
    subparsers.add_parser("status")
    args = parser.parse_args()
    if args.command == "sync":
        value: Any = sync(args.article_root)
    elif args.command == "list":
        value = {"jobs": list_jobs(args.limit)}
    elif args.command == "complete":
        value = complete_job(args.job, args.result_file)
    elif args.command == "fail":
        value = fail_job(args.job, args.error)
    elif args.command == "fallback":
        value = fallback_job(args.job)
    else:
        value = status()
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
