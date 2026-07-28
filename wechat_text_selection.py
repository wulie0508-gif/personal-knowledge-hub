#!/usr/bin/env python3
"""Convert WeChat notes to text-only and select a compact knowledge corpus.

Ordinary public accounts contribute at most ``--limit`` representative notes.
Tencent Research Institute is intentionally uncapped and remains eligible for
article-by-article Codex curation.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import knowledge_pipeline
import refresh_knowledge_value
import runtime_config


VAULT = runtime_config.configured_vault()
WECHAT_ROOT = VAULT / "10_Sources" / "WeChat"
REPORT_JSON = (
    runtime_config.runtime_home()
    / "wechat-text-selection"
    / "selection-report.json"
)
OVERVIEW = VAULT / "20_Knowledge" / "微信公众号精选.md"
TENCENT_ACCOUNT = "腾讯研究院"

DURABLE_RE = re.compile(
    r"方法|方法论|框架|原理|复盘|实践|教程|指南|研究|为什么|如何|"
    r"案例|拆解|评测|对比|访谈|对话|思考|经验|技术解析|论文|源码|开源"
)
THEME_RE = re.compile(
    r"agent|rag|ai|大模型|模型|机器人|具身|清洁能源|储能|光伏|风电|"
    r"商业化|saas|组织|知识管理|数据中心|投资|财务|证据|研究|产品|"
    r"治理|战略|增长",
    re.IGNORECASE,
)
EPHEMERAL_RE = re.compile(
    r"早报|晚报|日报|周报|月报|刚刚|官宣|发布|上线|融资|榜单|招聘|"
    r"报名|直播|峰会|大会|优惠|福利|限时|送票|获奖"
)
MARKETING_RE = re.compile(
    r"报名|购买|优惠|福利|训练营|课程|招生|招聘|门票|咨询|扫码"
)


def field(text: str, name: str, default: str = "") -> str:
    return str(knowledge_pipeline.frontmatter_field(text, name) or default)


def article_body(text: str) -> str:
    return refresh_knowledge_value.article_body(text)


def strip_image_references(text: str) -> str:
    """Remove image payloads/references while preserving useful alt text."""

    text = re.sub(r"(?m)^>\s*封面[：:].*$\n?", "", text)
    text = re.sub(r"(?m)^\s*!\[\[[^\]]+\]\]\s*$\n?", "", text)

    def markdown_image(match: re.Match[str]) -> str:
        alt = re.sub(r"\s+", " ", match.group(1)).strip()
        return alt if alt else ""

    text = re.sub(
        r"!\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)",
        markdown_image,
        text,
    )
    text = re.sub(r"<img\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = refresh_knowledge_value.replace_frontmatter(text, "cover", "")
    text = refresh_knowledge_value.replace_frontmatter(text, "image_mode", "none")
    text = refresh_knowledge_value.replace_frontmatter(text, "image_count", 0)
    text = refresh_knowledge_value.replace_frontmatter(text, "kept_image_count", 0)
    text = refresh_knowledge_value.replace_frontmatter(
        text, "ocr_status", "not_required_text_only"
    )
    return re.sub(r"\n{4,}", "\n\n\n", text).rstrip() + "\n"


def note_score(path: Path, text: str) -> int:
    title = field(text, "title", path.stem)
    body = article_body(text)
    value = int(field(text, "knowledge_value_score", "0") or 0)
    priority = field(text, "knowledge_priority")
    score = value
    score += {
        "重点": 80,
        "参考": 35,
        "速览": -20,
        "回收建议": -90,
    }.get(priority, 0)
    if field(text, "curation_status") == "complete":
        score += 25
    if DURABLE_RE.search(title):
        score += 32
    if THEME_RE.search(title):
        score += 22
    if EPHEMERAL_RE.search(title) and not DURABLE_RE.search(title):
        score -= 35
    if MARKETING_RE.search(title) and not DURABLE_RE.search(title):
        score -= 45
    compact_length = len(re.sub(r"\s+", "", body))
    if compact_length:
        score += min(30, int(math.log10(max(10, compact_length)) * 8))
    if compact_length < 500:
        score -= 45
    content_status = field(text, "content_status", "complete")
    if content_status in {"metadata_only", "failed", "incomplete"}:
        score -= 100
    return score


def candidate_notes() -> list[dict[str, Any]]:
    by_source: dict[str, dict[str, Any]] = {}
    if not WECHAT_ROOT.is_dir():
        return []
    for path in WECHAT_ROOT.rglob("*.md"):
        if path.name.startswith("_") or "_assets" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        account = field(text, "account")
        source_url = field(text, "source_url") or field(text, "sourceUrl")
        if not account or not source_url:
            continue
        item = {
            "path": path,
            "text": text,
            "account": account,
            "source_url": source_url,
            "title": field(text, "title", path.stem),
            "publish_date": field(text, "publish_date"),
            "score": note_score(path, text),
            "curated": "Articles" in path.parts,
        }
        current = by_source.get(source_url)
        if current is None or (
            bool(item["curated"]),
            int(item["score"]),
            path.stat().st_mtime_ns,
        ) > (
            bool(current["curated"]),
            int(current["score"]),
            current["path"].stat().st_mtime_ns,
        ):
            by_source[source_url] = item
    return list(by_source.values())


def all_physical_notes() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not WECHAT_ROOT.is_dir():
        return items
    for path in WECHAT_ROOT.rglob("*.md"):
        if path.name.startswith("_") or "_assets" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        account = field(text, "account")
        source_url = field(text, "source_url") or field(text, "sourceUrl")
        if not account or not source_url:
            continue
        items.append(
            {
                "path": path,
                "text": text,
                "account": account,
                "source_url": source_url,
                "score": note_score(path, text),
            }
        )
    return items


def select_notes(items: list[dict[str, Any]], limit: int) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[str(item["account"])].append(item)
    selected: dict[str, list[dict[str, Any]]] = {}
    for account, values in grouped.items():
        values.sort(
            key=lambda item: (
                -int(item["score"]),
                str(item.get("publish_date") or ""),
                str(item["title"]),
            )
        )
        selected[account] = values if account == TENCENT_ACCOUNT else values[:limit]
    return selected


def wiki_path(path: Path) -> str:
    return path.relative_to(VAULT).with_suffix("").as_posix()


def apply_selection(limit: int) -> dict[str, Any]:
    items = candidate_notes()
    physical_items = all_physical_notes()
    selected = select_notes(items, limit)
    selected_paths = {
        item["path"].resolve()
        for values in selected.values()
        for item in values
    }
    changed = 0
    textified = 0
    all_markdown_paths = [
        path
        for path in WECHAT_ROOT.rglob("*.md")
        if "_assets" not in path.parts
    ]
    for path in all_markdown_paths:
        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            # Curation may atomically move a note into Articles while this
            # maintenance pass is walking the vault. The new path will be
            # picked up on the next pass, so a vanished old path is benign.
            continue
        updated = strip_image_references(original)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed += 1
        if "image_mode: \"none\"" in updated or "image_mode: none" in updated:
            textified += 1

    for item in physical_items:
        path = item["path"]
        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            # A completed curation job can relocate this note after the
            # candidate snapshot was taken.
            continue
        updated = strip_image_references(original)
        scope = "selected" if path.resolve() in selected_paths else "not_selected"
        updated = refresh_knowledge_value.replace_frontmatter(
            updated, "knowledge_scope", scope
        )
        updated = refresh_knowledge_value.replace_frontmatter(
            updated, "selection_score", int(item["score"])
        )
        updated = refresh_knowledge_value.replace_frontmatter(
            updated, "selection_policy", "tencent_all" if item["account"] == TENCENT_ACCOUNT else f"top_{limit}"
        )
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed += 1

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy": {
            "ordinary_account_limit": limit,
            "tencent_research_limit": None,
            "image_mode": "none",
        },
        "candidate_count": len(items),
        "selected_count": len(selected_paths),
        "textified_count": textified,
        "physical_note_count": len(all_markdown_paths),
        "changed_count": changed,
        "accounts": {
            account: {
                "available": sum(1 for item in items if item["account"] == account),
                "selected": len(values),
                "items": [
                    {
                        "title": item["title"],
                        "publish_date": item["publish_date"],
                        "score": item["score"],
                        "path": str(item["path"]),
                        "source_url": item["source_url"],
                    }
                    for item in values
                ],
            }
            for account, values in sorted(selected.items())
        },
    }
    temporary = REPORT_JSON.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(REPORT_JSON)

    OVERVIEW.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        "# 微信公众号精选",
        "",
        f"> 更新：{report['generated_at']} · 普通公众号每号约 {limit} 篇 · 腾讯研究院全量证据库 + 代表样本深读 · 纯文字",
        "",
        "外部文章保留作者、公众号、日期和原文链接，不代表用户本人观点。",
        "",
    ]
    for account, values in sorted(selected.items()):
        rows += [f"## {account}（{len(values)} 篇）", ""]
        for item in values:
            date = f"{item['publish_date']} · " if item["publish_date"] else ""
            rows.append(
                f"- [[{wiki_path(item['path'])}|{item['title']}]] — {date}候选分 {item['score']}"
            )
        rows.append("")
    OVERVIEW.write_text("\n".join(rows).rstrip() + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    report = apply_selection(max(1, args.limit))
    print(
        json.dumps(
            {
                "candidate_count": report["candidate_count"],
                "selected_count": report["selected_count"],
                "textified_count": report["textified_count"],
                "changed_count": report["changed_count"],
                "report": str(REPORT_JSON),
                "overview": str(OVERVIEW),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
