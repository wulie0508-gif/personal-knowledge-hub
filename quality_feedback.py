"""Learn conservative article-quality preferences from explicit user feedback."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import runtime_config


ROOT = Path(__file__).resolve().parent
STORE_PATH = runtime_config.private_path("quality-preferences.json")
LOCK = threading.RLock()
EMPTY_MODEL: dict[str, Any] = {
    "version": 1,
    "accounts": {},
    "categories": {},
    "terms": {},
    "events": [],
}
STOP_TERMS = {
    "一个",
    "这个",
    "那个",
    "什么",
    "怎么",
    "为什么",
    "可以",
    "还是",
    "已经",
    "我们",
    "你们",
    "他们",
    "现在",
    "今天",
    "刚刚",
}


def _load() -> dict[str, Any]:
    try:
        value = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else dict(EMPTY_MODEL)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return json.loads(json.dumps(EMPTY_MODEL))


def _save(model: dict[str, Any]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STORE_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(model, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(STORE_PATH)


def title_terms(title: str) -> list[str]:
    values: list[str] = []
    values.extend(
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+._-]{2,}", title)
    )
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", title):
        if len(segment) <= 6:
            values.append(segment)
        for index in range(max(0, len(segment) - 1)):
            values.append(segment[index : index + 2])
    unique: list[str] = []
    for value in values:
        if value not in STOP_TERMS and value not in unique:
            unique.append(value)
        if len(unique) >= 18:
            break
    return unique


def _increment(bucket: dict[str, Any], key: str, label: str) -> None:
    if not key:
        return
    stats = bucket.setdefault(key, {"keep": 0, "remove": 0})
    stats[label] = int(stats.get(label) or 0) + 1


def _rebuild(model: dict[str, Any]) -> None:
    model["accounts"] = {}
    model["categories"] = {}
    model["terms"] = {}
    for event in model.get("events", []):
        event_label = str(event.get("label") or "")
        if event_label not in {"keep", "remove"}:
            continue
        _increment(model["accounts"], str(event.get("account") or ""), event_label)
        _increment(
            model["categories"],
            str(event.get("category") or ""),
            event_label,
        )
        for term in title_terms(str(event.get("title") or "")):
            _increment(model["terms"], term, event_label)


def record_feedback(
    *,
    url: str,
    title: str,
    account: str,
    category: str,
    label: str,
) -> dict[str, Any]:
    if label not in {"keep", "remove"}:
        raise ValueError("反馈必须是 keep 或 remove")
    with LOCK:
        model = _load()
        events = [
            event
            for event in model.setdefault("events", [])
            if event.get("url") != url
        ]
        events.append(
            {
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "url": url,
                "title": title,
                "account": account,
                "category": category,
                "label": label,
                "source": "explicit",
            }
        )
        model["events"] = events[-500:]
        _rebuild(model)
        _save(model)
        return summary(model)


def _frontmatter_value(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", text, re.MULTILINE)
    if not match:
        return ""
    raw = match.group(1).strip()
    try:
        value = json.loads(raw)
        return str(value) if value is not None else ""
    except json.JSONDecodeError:
        return raw.strip("\"'")


def ingest_trash_history(paths: list[Path]) -> dict[str, Any]:
    """Treat deleted WeChat Markdown notes as negative preference signals.

    Only notes that explicitly identify as WeChat articles are considered. The
    operation is idempotent by source URL, so periodic scans are safe.
    """
    candidates: list[Path] = []
    for root in paths:
        if root.is_dir():
            candidates.extend(root.rglob("*.md"))
    learned = 0
    scanned = 0
    with LOCK:
        model = _load()
        events = list(model.setdefault("events", []))
        by_url = {
            str(event.get("url") or ""): event
            for event in events
            if event.get("url")
        }
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        for note in candidates:
            try:
                text = note.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            platform = _frontmatter_value(text, "platform")
            url = _frontmatter_value(text, "sourceUrl")
            if platform != "wechat_mp" and "mp.weixin.qq.com/" not in url:
                continue
            if not url:
                continue
            scanned += 1
            existing = by_url.get(url)
            source_path = str(note.resolve())
            if (
                existing
                and existing.get("label") == "remove"
                and existing.get("source_path") == source_path
            ):
                continue
            event = {
                "at": now,
                "url": url,
                "title": _frontmatter_value(text, "title") or note.stem,
                "account": _frontmatter_value(text, "account"),
                "category": _frontmatter_value(text, "category") or "其他",
                "label": "remove",
                "source": "trash",
                "source_path": source_path,
            }
            events = [item for item in events if item.get("url") != url]
            events.append(event)
            by_url[url] = event
            learned += 1
        model["events"] = events[-500:]
        _rebuild(model)
        model["trash_scan"] = {
            "at": now,
            "scanned_notes": scanned,
            "new_signals": learned,
            "paths": [str(path) for path in paths],
        }
        _save(model)
        result = summary(model)
        result["trash_scan"] = model["trash_scan"]
        return result


def _preference(stats: dict[str, Any] | None) -> tuple[float, int]:
    stats = stats or {}
    keep = int(stats.get("keep") or 0)
    remove = int(stats.get("remove") or 0)
    total = keep + remove
    return ((keep - remove) / (total + 2) if total else 0.0), total


def score_adjustment(
    title: str,
    account: str,
    category: str,
) -> tuple[int, list[str]]:
    model = _load()
    adjustment = 0.0
    reasons: list[str] = []

    account_pref, account_total = _preference(
        model.get("accounts", {}).get(account)
    )
    if account_total:
        account_delta = account_pref * 18
        adjustment += account_delta
        reasons.append(f"公众号偏好 {account_delta:+.0f}")

    category_pref, category_total = _preference(
        model.get("categories", {}).get(category)
    )
    if category_total >= 2:
        category_delta = category_pref * 10
        adjustment += category_delta
        reasons.append(f"分类偏好 {category_delta:+.0f}")

    term_deltas: list[float] = []
    for term in title_terms(title):
        preference, total = _preference(model.get("terms", {}).get(term))
        if total >= 2:
            term_deltas.append(preference * 4)
    if term_deltas:
        term_delta = max(-10.0, min(10.0, sum(term_deltas)))
        adjustment += term_delta
        reasons.append(f"标题特征 {term_delta:+.0f}")

    return round(max(-30.0, min(30.0, adjustment))), reasons


def auto_remove_decision(
    *,
    account: str,
    category: str,
    score: int,
) -> tuple[bool, float, list[str]]:
    """Only auto-remove after repeated, highly consistent explicit feedback."""
    model = _load()
    account_stats = model.get("accounts", {}).get(account) or {}
    keep = int(account_stats.get("keep") or 0)
    remove = int(account_stats.get("remove") or 0)
    total = keep + remove
    confidence = remove / total if total else 0.0
    reasons: list[str] = []
    if total >= 3 and confidence >= 0.8:
        reasons.append(f"你已移除该公众号内容 {remove}/{total} 次")

    category_stats = model.get("categories", {}).get(category) or {}
    category_keep = int(category_stats.get("keep") or 0)
    category_remove = int(category_stats.get("remove") or 0)
    category_total = category_keep + category_remove
    category_confidence = (
        category_remove / category_total if category_total else 0.0
    )
    category_signal = category_total >= 6 and category_confidence >= 0.9
    if category_signal:
        reasons.append(
            f"你已移除该分类内容 {category_remove}/{category_total} 次"
        )

    decision = score < 45 and (
        (total >= 3 and confidence >= 0.8) or category_signal
    )
    return decision, max(confidence, category_confidence), reasons


def summary(model: dict[str, Any] | None = None) -> dict[str, Any]:
    model = model or _load()
    events = model.get("events", [])
    keep = sum(1 for event in events if event.get("label") == "keep")
    remove = sum(1 for event in events if event.get("label") == "remove")
    trash_learned = sum(
        1
        for event in events
        if event.get("label") == "remove" and event.get("source") == "trash"
    )
    return {
        "feedback_count": len(events),
        "keep_count": keep,
        "remove_count": remove,
        "learned_accounts": len(model.get("accounts", {})),
        "learned_categories": len(model.get("categories", {})),
        "trash_learned_count": trash_learned,
        "trash_scan": model.get("trash_scan", {}),
        "auto_remove_rule": "同公众号至少 3 次反馈、删除率 ≥80%、文章分数 <45",
    }
