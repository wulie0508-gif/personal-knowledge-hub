#!/usr/bin/env python3
"""Reassess existing notes by long-term knowledge value and reorganize them."""

from __future__ import annotations

import json
import os
import re
import shutil
import urllib.parse
from pathlib import Path
from typing import Any

import knowledge_pipeline
import knowledge_value


START_MARKER = "<!-- knowledge-value:start -->"
END_MARKER = "<!-- knowledge-value:end -->"


def replace_frontmatter(text: str, name: str, value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False)
    pattern = rf"^{re.escape(name)}:\s*.*$"
    replacement = f"{name}: {rendered}"
    if re.search(pattern, text, re.MULTILINE):
        return re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)
    end = text.find("\n---", 3)
    if text.startswith("---") and end >= 0:
        return text[:end] + f"\n{replacement}" + text[end:]
    return text


def article_body(text: str) -> str:
    match = re.search(
        r"^## 正文\s*\n(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else text


def value_block(assessment: knowledge_value.ValueAssessment) -> str:
    rows = [START_MARKER]
    if assessment.priority == "重点":
        rows += [
            "> [!important] 重点知识",
            "> 这篇包含可复用的见解、方法或案例，优先进入你的长期知识网络。",
            "",
        ]
    if assessment.summary or assessment.highlights:
        rows += ["## 知识提炼", ""]
        if assessment.summary:
            rows += [assessment.summary, ""]
        if assessment.highlights:
            rows += ["### 关键点", ""]
            rows += [f"- {item}" for item in assessment.highlights]
            rows.append("")
    rows.append(END_MARKER)
    return "\n".join(rows)


def rewrite_image_links(text: str, old_parent: Path, new_parent: Path) -> str:
    def replacement(match: re.Match[str]) -> str:
        raw = match.group(1)
        decoded = urllib.parse.unquote(raw)
        if decoded.startswith(("http://", "https://")):
            return match.group(0)
        absolute = (old_parent / decoded).resolve()
        if not absolute.is_file():
            return match.group(0)
        relative = os.path.relpath(absolute, new_parent).replace("\\", "/")
        return match.group(0).replace(
            raw,
            urllib.parse.quote(relative, safe="/._-"),
        )

    return re.sub(r"!\[[^\]]*\]\(([^)]+)\)", replacement, text)


def reassess_note(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    quality = int(knowledge_pipeline.frontmatter_field(text, "quality_score") or 0)
    status = knowledge_pipeline.frontmatter_field(text, "content_status") or "metadata_only"
    assessment = knowledge_value.assess(
        title=knowledge_pipeline.frontmatter_field(text, "title") or path.stem,
        body=article_body(text),
        quality_score=quality,
        content_status=status,
    )
    if knowledge_pipeline.frontmatter_field(text, "priority_override").lower() == "true":
        assessment.priority = (
            knowledge_pipeline.frontmatter_field(text, "knowledge_priority")
            or assessment.priority
        )
    for name, value in (
        ("knowledge_type", assessment.knowledge_type),
        ("knowledge_value_score", assessment.value_score),
        ("knowledge_priority", assessment.priority),
        ("value_reasons", assessment.reasons),
        (
            "mastery_status",
            knowledge_pipeline.frontmatter_field(text, "mastery_status") or "未学习",
        ),
    ):
        text = replace_frontmatter(text, name, value)
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
    block = value_block(assessment)
    if "## 正文" in text:
        text = text.replace("## 正文", block + "\n\n## 正文", 1)
    else:
        text += "\n\n" + block
    value_line = (
        f"- 知识价值：{assessment.knowledge_type} · {assessment.priority}"
        f"（{assessment.value_score}/100）"
    )
    if re.search(r"(?m)^- 知识价值：.*$", text):
        text = re.sub(r"(?m)^- 知识价值：.*$", value_line, text, count=1)
    elif re.search(r"(?m)^- 原文：", text):
        text = re.sub(r"(?m)^(- 原文：)", value_line + "\n\\1", text, count=1)

    category = knowledge_pipeline.frontmatter_field(text, "category") or "其他"
    target_result = knowledge_pipeline.ArticleResult(
        source_url=knowledge_pipeline.frontmatter_field(text, "sourceUrl"),
        title=knowledge_pipeline.frontmatter_field(text, "title") or path.stem,
        category=category,
        knowledge_priority=assessment.priority,
    )
    destination_folder = knowledge_pipeline.output_folder(target_result)
    destination_folder.mkdir(parents=True, exist_ok=True)
    destination = destination_folder / path.name
    text = rewrite_image_links(text, path.parent, destination.parent)
    destination.write_text(text.rstrip() + "\n", encoding="utf-8")
    if destination.resolve() != path.resolve() and path.is_file():
        path.unlink()
    return {
        "title": target_result.title,
        "type": assessment.knowledge_type,
        "value": assessment.value_score,
        "priority": assessment.priority,
        "path": str(destination),
    }


def main() -> int:
    notes = list(knowledge_pipeline.ARTICLE_ROOT.rglob("*.md"))
    results = [reassess_note(path) for path in notes]
    counts: dict[str, int] = {}
    for result in results:
        key = str(result["priority"])
        counts[key] = counts.get(key, 0) + 1
    print(
        json.dumps(
            {"count": len(results), "priorities": counts, "articles": results},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
