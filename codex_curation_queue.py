#!/usr/bin/env python3
"""Queue articles for Codex semantic curation and apply structured results."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import knowledge_pipeline
import refresh_knowledge_value
import runtime_config


ROOT = Path(__file__).resolve().parent
DATA_DIR = runtime_config.runtime_home()
QUEUE_ROOT = DATA_DIR / "codex-curation-queue"
PENDING_DIR = QUEUE_ROOT / "pending"
COMPLETED_DIR = QUEUE_ROOT / "completed"
UNAVAILABLE_DIR = QUEUE_ROOT / "unavailable"
RESULT_DIR = DATA_DIR / "codex-curation-results"
MATURITY_REPORT = DATA_DIR / "reports" / "knowledge-maturity.json"
VERSION = 1
QUEUE_POLICY_VERSION = 2
ALLOWED_TYPES = {
    "观点见解",
    "方法工具",
    "案例研究",
    "参考资料",
    "时效资讯",
    "营销活动",
}
ALLOWED_PRIORITIES = {"重点", "参考", "速览", "回收建议"}
START_MARKER = "<!-- knowledge-value:start -->"
END_MARKER = "<!-- knowledge-value:end -->"
VAULT = runtime_config.configured_vault()
LONG_TERM_TITLE_RE = re.compile(
    r"方法|方法论|框架|原理|复盘|实践|教程|指南|研究|为什么|如何|"
    r"案例|拆解|评测|对比|访谈|对话|思考|经验|技术解析|源码|开源"
)
PERSONAL_THEME_RE = re.compile(
    r"agent|rag|ai|大模型|机器人|具身|清洁能源|储能|光伏|风电|"
    r"商业化|saas|组织|知识管理|obsidian|数据中心|投资|财务|"
    r"单位经济性|证据|research",
    re.IGNORECASE,
)
EPHEMERAL_TITLE_RE = re.compile(
    r"早报|晚报|周报|月报|刚刚|官宣|发布|上线|融资|榜单|招聘|"
    r"报名|直播|峰会|大会|优惠|福利|限时"
)
UNAVAILABLE_BODY_RE = re.compile(
    r"^\s*(?:未抓取到正文内容[。.]?|正文内容暂不可用[。.]?)\s*$"
)


def source_roots() -> list[Path]:
    return [
        knowledge_pipeline.ARTICLE_ROOT,
        VAULT / "10_Sources" / "Local" / "Articles",
        VAULT / "10_Sources" / "Xiaohongshu",
        VAULT / "10_Sources" / "Feishu",
    ]


def candidate_notes() -> list[Path]:
    paths: dict[Path, None] = {}
    for root in source_roots():
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            paths[path.resolve()] = None
    # Full subscription backfills land in the WeChat root before semantic
    # curation moves them into Articles/<priority>/<category>. Include only
    # generated article notes; exclude indexes, summaries and hand-written
    # support files.
    if knowledge_pipeline.WECHAT_ROOT.is_dir():
        for path in knowledge_pipeline.WECHAT_ROOT.glob("*.md"):
            if path.name.startswith("_"):
                continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                head = []
                for _ in range(200):
                    line = handle.readline()
                    if not line:
                        break
                    head.append(line.rstrip("\n"))
            if (
                any(line.startswith("source_url:") for line in head)
                and any(line.startswith("article_slug:") for line in head)
                and (
                    any(
                        line.strip() == 'knowledge_scope: "selected"'
                        for line in head
                    )
                    or any(
                        line.strip() in {
                            'account: "腾讯研究院"',
                            "account: 腾讯研究院",
                        }
                        for line in head
                    )
                )
            ):
                paths[path.resolve()] = None
    return sorted(paths)


def output_folder_for_note(text: str, priority: str, category: str) -> Path:
    platform = knowledge_pipeline.frontmatter_field(text, "platform")
    base_by_platform = {
        "local": VAULT / "10_Sources" / "Local" / "Articles",
        "xiaohongshu": VAULT / "10_Sources" / "Xiaohongshu",
        "feishu": VAULT / "10_Sources" / "Feishu",
    }
    base = base_by_platform.get(platform, knowledge_pipeline.ARTICLE_ROOT)
    if priority == "重点":
        return base / "重点知识" / knowledge_pipeline.safe_name(category, 30)
    if priority == "速览":
        return base / "资讯速览"
    if priority == "回收建议":
        return base / "低价值待清理"
    return base / knowledge_pipeline.safe_name(category, 30)


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def publication_year(job: dict[str, Any]) -> str:
    value = " ".join(
        str(job.get(key) or "")
        for key in ("publish_date", "note_path", "title")
    )
    match = re.search(r"\b(20\d{2})\b", value)
    return match.group(1) if match else ""


def completed_year_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in COMPLETED_DIR.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        year = publication_year(job)
        if year:
            counts[year] = counts.get(year, 0) + 1
    return counts


def historical_deep_read_cutoff() -> str:
    """Return the maturity timestamp once bulk historical reading should stop."""
    try:
        report = json.loads(MATURITY_REPORT.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    if not report.get("stop_recommended"):
        return ""
    return str(report.get("generated_at") or "")


def paused_by_maturity(job: dict[str, Any], cutoff: str) -> bool:
    if not cutoff or str(job.get("account") or "") != "腾讯研究院":
        return False
    # Keep genuinely new incremental articles actionable. Only the historical
    # backlog that existed when saturation was reached is paused.
    return str(job.get("created_at") or "") <= cutoff


def curation_rank(
    job: dict[str, Any],
    year_counts: dict[str, int] | None = None,
) -> int:
    title = str(job.get("title") or "")
    account = str(job.get("account") or "")
    body_length = max(0, int(job.get("body_length") or 0))
    score = max(0, min(100, int(job.get("current_value") or 0)))
    if LONG_TERM_TITLE_RE.search(title):
        score += 28
    if PERSONAL_THEME_RE.search(title):
        score += 18
    if EPHEMERAL_TITLE_RE.search(title) and not LONG_TERM_TITLE_RE.search(title):
        score -= 28
    if body_length >= 8000:
        score += 18
    elif body_length >= 3000:
        score += 13
    elif body_length >= 1200:
        score += 7
    elif body_length < 400:
        score -= 18
    current_priority = str(job.get("current_priority") or "")
    score += {
        "重点": 20,
        "参考": 8,
        "速览": -12,
        "回收建议": -25,
    }.get(current_priority, 0)
    if account == "腾讯研究院":
        # The user explicitly treats this source as a deep-reading corpus.
        score += 1000
        year = publication_year(job)
        if year_counts is not None and year:
            # Avoid spending hundreds of turns on recent daily briefs while
            # older research years remain untouched. Under-covered years get
            # a bounded bonus; title/value signals still decide within a year.
            score += max(0, 80 - min(80, year_counts.get(year, 0) * 5))
    return score


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def identifier(note: Path) -> str:
    return hashlib.sha1(str(note.resolve()).encode("utf-8")).hexdigest()[:16]


def unavailable_body(body: str) -> bool:
    normalized = str(body or "").strip()
    if UNAVAILABLE_BODY_RE.fullmatch(normalized):
        return True
    return any(
        normalized.endswith(marker)
        for marker in (
            "未抓取到正文内容。",
            "未抓取到正文内容.",
            "正文内容暂不可用。",
            "正文内容暂不可用.",
        )
    )


def enqueue(note: Path) -> dict[str, Any] | None:
    if not note.is_file():
        return None
    stat = note.stat()
    job_id = identifier(note)
    path = PENDING_DIR / f"{job_id}.json"
    existing: dict[str, Any] = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    if (
        existing
        and int(existing.get("source_mtime_ns") or 0) == stat.st_mtime_ns
        and int(existing.get("source_size") or -1) == stat.st_size
        and existing.get("curation_rank") is not None
        and int(existing.get("queue_policy_version") or 0) >= QUEUE_POLICY_VERSION
    ):
        return None
    text = note.read_text(encoding="utf-8", errors="replace")
    version = int(knowledge_pipeline.frontmatter_field(text, "curation_version") or 0)
    status = knowledge_pipeline.frontmatter_field(text, "curation_status")
    if version >= VERSION and status == "complete":
        return None
    body = refresh_knowledge_value.article_body(text)
    if unavailable_body(body):
        path.unlink(missing_ok=True)
        unavailable = {
            "id": job_id,
            "created_at": existing.get("created_at") or now_text(),
            "updated_at": now_text(),
            "source_mtime_ns": stat.st_mtime_ns,
            "source_size": stat.st_size,
            "note_path": str(note.resolve()),
            "title": knowledge_pipeline.frontmatter_field(text, "title") or note.stem,
            "account": knowledge_pipeline.frontmatter_field(text, "account"),
            "publish_date": knowledge_pipeline.frontmatter_field(text, "publish_date"),
            "status": "unavailable",
            "reason": "正文未抓取，仅有元数据；不进入深度整理与成熟度统计。",
            "queue_policy_version": QUEUE_POLICY_VERSION,
        }
        atomic_json(UNAVAILABLE_DIR / f"{job_id}.json", unavailable)
        return None
    (UNAVAILABLE_DIR / f"{job_id}.json").unlink(missing_ok=True)
    job = {
        "id": job_id,
        "created_at": existing.get("created_at") or now_text(),
        "updated_at": now_text(),
        "source_mtime_ns": stat.st_mtime_ns,
        "source_size": stat.st_size,
        "note_path": str(note.resolve()),
        "title": knowledge_pipeline.frontmatter_field(text, "title") or note.stem,
        "account": knowledge_pipeline.frontmatter_field(text, "account"),
        "publish_date": knowledge_pipeline.frontmatter_field(text, "publish_date"),
        "category": knowledge_pipeline.frontmatter_field(text, "category"),
        "current_type": knowledge_pipeline.frontmatter_field(text, "knowledge_type"),
        "current_priority": knowledge_pipeline.frontmatter_field(text, "knowledge_priority"),
        "current_value": int(
            knowledge_pipeline.frontmatter_field(text, "knowledge_value_score") or 0
        ),
        "body_length": len(re.sub(r"\s+", "", body)),
        "status": "pending",
        "attempts": int(existing.get("attempts") or 0),
        "queue_policy_version": QUEUE_POLICY_VERSION,
        "instructions": (
            "Read the note, classify long-term knowledge value, and extract only "
            "reusable insights. News should normally be 速览; strong explanations, "
            "methods and cases may be 重点. Marketing should be 回收建议 unless it "
            "contains durable methods. Do not reward sensational wording."
        ),
    }
    job["curation_rank"] = curation_rank(job)
    atomic_json(path, job)
    return job


def prune_stale(allowed_paths: set[Path] | None = None) -> int:
    removed = 0
    for path in PENDING_DIR.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            note = Path(str(job.get("note_path") or ""))
        except (json.JSONDecodeError, OSError):
            note = Path()
        if (
            not note.is_file()
            or allowed_paths is not None
            and note.resolve() not in allowed_paths
        ):
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def sync() -> dict[str, Any]:
    candidates = candidate_notes()
    allowed_paths = {note.resolve() for note in candidates}
    queued = 0
    for note in candidates:
        if enqueue(note):
            queued += 1
    pruned = prune_stale(allowed_paths)
    return {
        "queued": queued,
        "pruned": pruned,
        "pending": len(list(PENDING_DIR.glob("*.json"))),
        "unavailable": len(list(UNAVAILABLE_DIR.glob("*.json"))),
    }


def list_jobs(limit: int = 10) -> list[dict[str, Any]]:
    prune_stale()
    year_counts = completed_year_counts()
    maturity_cutoff = historical_deep_read_cutoff()
    jobs: list[dict[str, Any]] = []
    for path in PENDING_DIR.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if paused_by_maturity(job, maturity_cutoff):
            continue
        # Recalculate so policy changes (for example, source-specific deep
        # reading priority) apply to already queued jobs as well.
        job["curation_rank"] = curation_rank(job, year_counts)
        job["publish_year"] = publication_year(job)
        job["manifest_path"] = str(path)
        jobs.append(job)
    jobs.sort(
        key=lambda item: (
            -int(item.get("curation_rank") or 0),
            str(item.get("created_at") or ""),
            str(item.get("id") or ""),
        )
    )
    return jobs[: max(0, limit)]


def validate_result(value: dict[str, Any]) -> dict[str, Any]:
    knowledge_type = str(value.get("knowledge_type") or "")
    priority = str(value.get("priority") or "")
    if knowledge_type not in ALLOWED_TYPES:
        raise ValueError(f"无效 knowledge_type：{knowledge_type}")
    if priority not in ALLOWED_PRIORITIES:
        raise ValueError(f"无效 priority：{priority}")
    score = max(0, min(100, int(value.get("value_score") or 0)))
    summary = str(value.get("summary") or "").strip()
    highlights = [
        str(item).strip()
        for item in value.get("highlights") or []
        if str(item).strip()
    ][:5]
    reason = str(value.get("reason") or "").strip()
    provider = str(value.get("provider") or "codex").strip().lower()
    if provider not in {"codex", "rules"}:
        raise ValueError(f"无效 provider：{provider}")
    return {
        "knowledge_type": knowledge_type,
        "priority": priority,
        "value_score": score,
        "summary": summary,
        "highlights": highlights,
        "reason": reason,
        "provider": provider,
    }


def render_block(value: dict[str, Any]) -> str:
    rows = [START_MARKER]
    if value["priority"] == "重点":
        rows += [
            "> [!important] 重点知识",
            "> Codex 判断这篇包含可复用的见解、方法或案例。",
            "",
        ]
    rows += ["## Codex 知识整理", "", value["summary"] or "未生成摘要。", ""]
    if value["highlights"]:
        rows += ["### 可复用的关键点", ""]
        rows += [f"- {item}" for item in value["highlights"]]
        rows.append("")
    if value["reason"]:
        rows += [f"> 分类理由：{value['reason']}", ""]
    rows.append(END_MARKER)
    return "\n".join(rows)


def normalize_subscription_note(text: str, value: dict[str, Any]) -> str:
    """Bridge full-subscription notes into the canonical knowledge schema."""
    source_url = (
        knowledge_pipeline.frontmatter_field(text, "sourceUrl")
        or knowledge_pipeline.frontmatter_field(text, "source_url")
    )
    title = knowledge_pipeline.frontmatter_field(text, "title")
    text = re.sub(
        r"(?m)^>\s*封面[：:].*https?://mmbiz\.qpic\.cn/.*$\n?",
        "",
        text,
    )
    if "## 正文" not in text:
        separators = list(re.finditer(r"(?m)^---\s*$", text))
        if len(separators) >= 3:
            position = separators[2].end()
            text = text[:position] + "\n\n## 正文\n" + text[position:]
        else:
            text += "\n\n## 正文\n"

    body = re.sub(
        r"\A---\s*\n.*?\n---\s*\n",
        "",
        text,
        count=1,
        flags=re.DOTALL,
    )
    body = re.sub(
        rf"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}",
        "",
        body,
        flags=re.DOTALL,
    )
    body_length = len(re.sub(r"\s+", "", body))
    category = (
        knowledge_pipeline.frontmatter_field(text, "category")
        or knowledge_pipeline.categorize(title, body)
    )
    score = max(45, int(value["value_score"])) if body_length else 0
    tier = "高" if score >= 75 else ("中" if score >= 50 else "低")
    image_count = len(re.findall(r"!\[\[[^\]]+\]\]|!\[[^\]]*\]\([^)]+\)", text))
    fields = (
        ("sourceUrl", source_url),
        ("platform", "wechat_mp"),
        ("category", category),
        ("quality_score", score),
        ("quality_tier", tier),
        ("content_status", "complete" if body_length else "metadata_only"),
        ("text_length", body_length),
        ("image_count", image_count),
        ("kept_image_count", image_count),
        ("ocr_length", 0),
        ("ocr_status", "pending_codex" if image_count >= 5 else "not_applicable"),
        ("cover", ""),
    )
    for name, field_value in fields:
        text = refresh_knowledge_value.replace_frontmatter(text, name, field_value)
    return text


def complete(job_id: str, result_file: Path) -> dict[str, Any]:
    manifest = PENDING_DIR / f"{job_id}.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"整理任务不存在：{job_id}")
    job = json.loads(manifest.read_text(encoding="utf-8"))
    note = Path(str(job["note_path"]))
    text = note.read_text(encoding="utf-8", errors="replace")
    value = validate_result(json.loads(result_file.read_text(encoding="utf-8")))
    if knowledge_pipeline.frontmatter_field(text, "priority_override").lower() == "true":
        value["priority"] = (
            knowledge_pipeline.frontmatter_field(text, "knowledge_priority")
            or value["priority"]
        )
    text = normalize_subscription_note(text, value)
    for name, field_value in (
        ("knowledge_type", value["knowledge_type"]),
        ("knowledge_value_score", value["value_score"]),
        ("knowledge_priority", value["priority"]),
        ("value_reasons", [value["reason"]] if value["reason"] else []),
        ("curation_status", "complete"),
        ("curation_version", VERSION),
        ("curated_at", now_text()),
        ("curation_provider", value["provider"]),
    ):
        text = refresh_knowledge_value.replace_frontmatter(text, name, field_value)
    text = re.sub(
        rf"\n?{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}\n?",
        "\n",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\n?<!-- knowledge-links:start -->.*?<!-- knowledge-links:end -->\n?",
        "\n",
        text,
        flags=re.DOTALL,
    )
    if "## 正文" in text:
        text = text.replace("## 正文", render_block(value) + "\n\n## 正文", 1)
    else:
        text += "\n\n" + render_block(value)
    value_line = (
        f"- 知识价值：{value['knowledge_type']} · {value['priority']}"
        f"（{value['value_score']}/100）"
    )
    if re.search(r"(?m)^- 知识价值：.*$", text):
        text = re.sub(r"(?m)^- 知识价值：.*$", value_line, text, count=1)
    target_folder = output_folder_for_note(
        text,
        value["priority"],
        knowledge_pipeline.frontmatter_field(text, "category") or "其他",
    )
    target_folder.mkdir(parents=True, exist_ok=True)
    target = target_folder / note.name
    text = refresh_knowledge_value.rewrite_image_links(text, note.parent, target.parent)
    target.write_text(text.rstrip() + "\n", encoding="utf-8")
    if target.resolve() != note.resolve() and note.is_file():
        note.unlink()
    job.update(
        status="completed",
        completed_at=now_text(),
        result_file=str(result_file.resolve()),
        note_path=str(target.resolve()),
        result=value,
    )
    destination = COMPLETED_DIR / manifest.name
    atomic_json(destination, job)
    manifest.unlink()
    return job


def fail(job_id: str, error: str) -> dict[str, Any]:
    manifest = PENDING_DIR / f"{job_id}.json"
    job = json.loads(manifest.read_text(encoding="utf-8"))
    job["attempts"] = int(job.get("attempts") or 0) + 1
    job["last_error"] = error
    job["updated_at"] = now_text()
    atomic_json(manifest, job)
    return job


def repair_completed_schema() -> dict[str, Any]:
    repaired = 0
    skipped = 0
    for manifest in COMPLETED_DIR.glob("*.json"):
        try:
            job = json.loads(manifest.read_text(encoding="utf-8"))
            note = Path(str(job.get("note_path") or ""))
            value = job.get("result") or {}
            if not note.is_file() or not value:
                skipped += 1
                continue
            text = note.read_text(encoding="utf-8", errors="replace")
            normalized = normalize_subscription_note(text, value)
            priority = str(value.get("priority") or "参考")
            category = knowledge_pipeline.frontmatter_field(normalized, "category") or "其他"
            target_folder = output_folder_for_note(normalized, priority, category)
            target_folder.mkdir(parents=True, exist_ok=True)
            target = target_folder / note.name
            normalized = refresh_knowledge_value.rewrite_image_links(
                normalized,
                note.parent,
                target.parent,
            )
            target.write_text(normalized.rstrip() + "\n", encoding="utf-8")
            if target.resolve() != note.resolve() and note.is_file():
                note.unlink()
            job["note_path"] = str(target.resolve())
            atomic_json(manifest, job)
            repaired += 1
        except (OSError, ValueError, json.JSONDecodeError):
            skipped += 1
    return {"repaired": repaired, "skipped": skipped}


def status() -> dict[str, Any]:
    prune_stale()
    maturity_cutoff = historical_deep_read_cutoff()
    return {
        "pending_count": len(list(PENDING_DIR.glob("*.json"))),
        "completed_count": len(list(COMPLETED_DIR.glob("*.json"))),
        "historical_deep_read_paused": bool(maturity_cutoff),
        "historical_deep_read_cutoff": maturity_cutoff,
        "queue_path": str(QUEUE_ROOT),
        "version": VERSION,
    }


def main() -> int:
    # Windows terminals often default to GBK. Historical article titles can
    # contain zero-width or other Unicode characters that GBK cannot encode,
    # which must not interrupt an otherwise valid curation queue operation.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Codex 知识整理队列")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync")
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--limit", type=int, default=10)
    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--job", required=True)
    complete_parser.add_argument("--result-file", type=Path, required=True)
    fail_parser = subparsers.add_parser("fail")
    fail_parser.add_argument("--job", required=True)
    fail_parser.add_argument("--error", required=True)
    subparsers.add_parser("repair-schema")
    subparsers.add_parser("status")
    args = parser.parse_args()
    if args.command == "sync":
        value: Any = sync()
    elif args.command == "list":
        value = {"jobs": list_jobs(args.limit)}
    elif args.command == "complete":
        value = complete(args.job, args.result_file)
    elif args.command == "fail":
        value = fail(args.job, args.error)
    elif args.command == "repair-schema":
        value = repair_completed_schema()
    else:
        value = status()
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
