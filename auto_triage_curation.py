#!/usr/bin/env python3
"""Rule-triage obvious news/marketing; leave durable knowledge to Codex."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import codex_curation_queue

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


ROOT = Path(__file__).resolve().parent
RESULT_DIR = ROOT / "data" / "rule-curation-results"
TRIAGE_VERSION = 1

DURABLE_RE = re.compile(
    r"原理|教程|方法|方法论|复盘|实践|指南|为什么|如何|研究|框架|深度|"
    r"访谈|对话|案例|拆解|论文|源码|开源|技术解析|经验|思考|"
    r"实测|评测|对比"
)
STRONG_MARKETING_RE = re.compile(
    r"报名通道|扫码报名|立即购买|限时优惠|课程招生|训练营|领取福利|"
    r"免费领取|直播预约|招聘|岗位热招|早鸟票|优惠券"
)
MARKETING_RE = re.compile(
    r"报名|课程|优惠|福利|直播|活动|大会|峰会|门票|购买|咨询|社群|"
    r"训练营|招聘|岗位|招生|限时|免费领"
)
NEWS_RE = re.compile(
    r"刚刚|官宣|发布|上线|推出|开幕|获批|融资|完成.{0,8}轮|榜单|"
    r"财报|季度|最新|首次|突破|宣布|签约|涨价|降价|更新|正式开放"
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])\s+|\n+")


def compact(text: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit].rstrip()


def highlights_from_body(body: str) -> list[str]:
    values = []
    for sentence in SENTENCE_SPLIT_RE.split(body):
        sentence = compact(sentence, 140)
        if (
            len(sentence) < 20
            or "发自 凹非寺" in sentence
            or re.search(r"公众号\s+[A-Za-z0-9]", sentence)
        ):
            continue
        values.append(sentence)
        if len(values) >= 2:
            break
    return values


def clean_article_body(note_text: str) -> str:
    text = re.sub(
        r"\A---\s*\n.*?\n---\s*\n",
        "",
        note_text,
        count=1,
        flags=re.DOTALL,
    )
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped == "---"
            or stripped.startswith("# ")
            or stripped.startswith("> [!info]")
            or stripped.startswith("> 公众号")
            or stripped.startswith("> 作者")
            or stripped.startswith("> 发布时间")
            or stripped.startswith("> 原文")
            or stripped.startswith("> 封面")
            or stripped.startswith("![[")
        ):
            continue
        rows.append(stripped)
    return "\n".join(rows)


def classify(job: dict, note_text: str) -> dict | None:
    title = compact(str(job.get("title") or ""), 240)
    body = clean_article_body(note_text)
    sample = compact(body, 3000)
    combined = f"{title} {sample[:800]}"

    if STRONG_MARKETING_RE.search(combined) or (
        len(MARKETING_RE.findall(combined)) >= 3
        and not DURABLE_RE.search(title)
    ):
        return {
            "knowledge_type": "营销活动",
            "value_score": 30,
            "priority": "回收建议",
            "summary": compact(title, 160),
            "highlights": highlights_from_body(body)[:1],
            "reason": "规则初筛：内容以报名、促销、招聘或活动转化为主，保留待复核，不自动删除。",
            "provider": "rules",
        }

    if NEWS_RE.search(title) and not DURABLE_RE.search(title):
        return {
            "knowledge_type": "时效资讯",
            "value_score": 40,
            "priority": "速览",
            "summary": compact(title, 160),
            "highlights": highlights_from_body(body),
            "reason": "规则初筛：标题和正文以产品发布、公司动态或时点事件为主，先压缩为速览。",
            "provider": "rules",
        }
    return None


def run(limit: int) -> dict:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    completed = []
    inspected = 0
    retained_for_codex = 0
    for job in codex_curation_queue.list_jobs(limit=100000):
        if int(job.get("rule_triage_version") or 0) >= TRIAGE_VERSION:
            continue
        if inspected >= limit:
            break
        inspected += 1
        note = Path(str(job.get("note_path") or ""))
        if not note.is_file():
            continue
        text = note.read_text(encoding="utf-8", errors="replace")
        result = classify(job, text)
        if result is None:
            job["rule_triage_version"] = TRIAGE_VERSION
            job["rule_triage_result"] = "retain_for_codex"
            codex_curation_queue.atomic_json(
                Path(str(job["manifest_path"])),
                job,
            )
            retained_for_codex += 1
            continue
        result_path = RESULT_DIR / f"{job['id']}-rules.json"
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        value = codex_curation_queue.complete(job["id"], result_path)
        completed.append(
            {
                "id": job["id"],
                "title": job.get("title", ""),
                "priority": result["priority"],
                "note_path": value.get("note_path", ""),
            }
        )
    return {
        "inspected": inspected,
        "completed": len(completed),
        "retained_for_codex": retained_for_codex,
        "items": completed,
        "remaining": codex_curation_queue.status()["pending_count"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    result = run(max(1, args.limit))
    if args.summary_only:
        result.pop("items", None)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
