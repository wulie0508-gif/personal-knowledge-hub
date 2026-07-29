"""Build a small, local-only context packet that helps an AI understand its user.

The hot context is deliberately different from the RAG corpus:

* user-authored notes and explicit feedback may describe the user;
* browsing history is an observed interest signal, never a belief or endorsement;
* external research is excluded and remains available only through retrieval.

The generated JSON is bounded so an agent can load it at the start of a task
without paying the cost of searching or summarising the whole knowledge base.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import knowledge_schema
import runtime_config


SCHEMA_VERSION = 1
DEFAULT_MAX_CHARS = 6_000
MAX_RECENT_MEMORIES = 18
MAX_RECENT_OBSERVATIONS = 16
MAX_TOPIC_SIGNALS = 12

CONTEXT_PATH = runtime_config.private_path("context", "ai-context.json")
PREFERENCE_PATH = runtime_config.private_path("quality-preferences.json")
WATCHER_STATE_PATH = runtime_config.private_path("wechat-history-state.json")

_STOP_WORDS = {
    "一个",
    "一些",
    "这个",
    "那个",
    "什么",
    "怎么",
    "为什么",
    "如何",
    "可以",
    "我们",
    "他们",
    "今天",
    "现在",
    "最新",
    "关于",
    "开始",
    "进行",
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
}


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return copy.deepcopy(default)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _clean(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _note_value(note: Any, name: str, default: Any = "") -> Any:
    return getattr(note, name, default)


def _memory_sort_key(note: Any) -> tuple[float, int, str, str]:
    return (
        float(_note_value(note, "persona_influence", 0.0) or 0.0),
        int(_note_value(note, "value_score", 0) or 0),
        str(
            _note_value(note, "curated_at", "")
            or _note_value(note, "publish_date", "")
        ),
        str(_note_value(note, "title", "")),
    )


def _personal_notes(notes: Iterable[Any]) -> list[Any]:
    result = [
        note
        for note in notes
        if _note_value(note, "corpus_namespace")
        == knowledge_schema.PERSONAL_MEMORY
        and _note_value(note, "authorship") == "self"
        and bool(_note_value(note, "identity_explicit", False))
        and float(_note_value(note, "persona_influence", 0.0) or 0.0) > 0
    ]
    return sorted(result, key=_memory_sort_key, reverse=True)


def _memory_summary(note: Any) -> str:
    summary = _clean(_note_value(note, "value_summary", ""))
    if summary:
        return summary
    body = str(_note_value(note, "body", "") or "")
    body = re.sub(r"^---.*?---", "", body, count=1, flags=re.DOTALL)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.DOTALL)
    return _clean(body)


def _term_candidates(text: str) -> list[str]:
    terms: list[str] = [
        value.casefold()
        for value in re.findall(r"[A-Za-z][A-Za-z0-9+._-]{2,}", text)
    ]
    for segment in re.findall(r"[\u4e00-\u9fff]{2,8}", text):
        terms.append(segment)
        if len(segment) > 4:
            terms.extend(
                segment[index : index + 2]
                for index in range(len(segment) - 1)
            )
    return [
        term
        for term in terms
        if term.casefold() not in _STOP_WORDS and len(term) >= 2
    ]


def _top_topics(values: Iterable[str], limit: int = MAX_TOPIC_SIGNALS) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for value in values:
        counts.update(dict.fromkeys(_term_candidates(value), 1))
    return [
        {"topic": topic, "observations": count}
        for topic, count in counts.most_common(limit)
    ]


def _explicit_preferences() -> dict[str, Any]:
    model = _load_json(PREFERENCE_PATH, {})

    def ranked(bucket_name: str, positive: bool) -> list[dict[str, Any]]:
        bucket = model.get(bucket_name) or {}
        values: list[tuple[float, int, str, int, int]] = []
        for name, raw in bucket.items():
            keep = int((raw or {}).get("keep") or 0)
            remove = int((raw or {}).get("remove") or 0)
            total = keep + remove
            if not name or not total:
                continue
            score = (keep - remove) / total
            if (positive and score <= 0) or (not positive and score >= 0):
                continue
            values.append((abs(score), total, str(name), keep, remove))
        values.sort(reverse=True)
        return [
            {
                "name": name,
                "keep": keep,
                "remove": remove,
                "confidence": round(score, 2),
            }
            for score, _total, name, keep, remove in values[:6]
        ]

    events = model.get("events") or []
    return {
        "signal_type": "explicit_feedback",
        "event_count": len(events),
        "preferred_sources": ranked("accounts", True),
        "avoided_sources": ranked("accounts", False),
        "preferred_categories": ranked("categories", True),
        "avoided_categories": ranked("categories", False),
    }


def _observed_trajectory() -> dict[str, Any]:
    state = _load_json(WATCHER_STATE_PATH, {})
    observations = [
        item
        for item in (state.get("recent_observations") or [])
        if isinstance(item, dict) and _clean(item.get("title"))
    ]
    observations.sort(
        key=lambda item: str(item.get("observed_at") or ""),
        reverse=True,
    )
    recent = [
        {
            "title": _clean(item.get("title"), 160),
            "observed_at": str(item.get("observed_at") or ""),
            "signal_type": "observed_reading",
            "confidence": "interest_signal_only",
        }
        for item in observations[:MAX_RECENT_OBSERVATIONS]
    ]
    return {
        "signal_count": len(observations),
        "meaning": (
            "These are pages observed in local reading history. They may indicate "
            "attention or curiosity, but never agreement, belief, mastery, or authorship."
        ),
        "topic_signals": _top_topics(
            str(item.get("title") or "") for item in observations
        ),
        "recent": recent,
    }


def build_context(
    notes: Iterable[Any],
    preference_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the durable, private base packet from confirmed personal notes."""

    personal = _personal_notes(notes)
    topic_counts = Counter(
        str(_note_value(note, "category", "") or "")
        for note in personal
        if _note_value(note, "category", "")
    )
    concept_counts = Counter(
        str(concept)
        for note in personal
        for concept in (_note_value(note, "concepts", ()) or ())
        if concept
    )
    recent_memories = [
        {
            "title": _clean(_note_value(note, "title", ""), 160),
            "date": str(
                _note_value(note, "curated_at", "")
                or _note_value(note, "publish_date", "")
            ),
            "summary": _memory_summary(note),
            "stance": str(_note_value(note, "stance", "") or "unreviewed"),
            "confidence": "confirmed_self_authored",
        }
        for note in personal[:MAX_RECENT_MEMORIES]
    ]
    feedback = _explicit_preferences()
    aggregate = preference_summary or {}
    if not feedback["event_count"]:
        feedback["event_count"] = int(aggregate.get("feedback_count") or 0)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_text(),
        "purpose": (
            "Small hot context for personalisation. Load this before retrieval; "
            "use detailed RAG only when the task needs a specific memory or citation."
        ),
        "identity_boundary": {
            "confirmed_self": (
                "User-authored personal_memory with positive persona_influence."
            ),
            "observed_behavior": (
                "Reading traces are weak attention signals and must not be stated "
                "as the user's view."
            ),
            "external_reference": (
                "Professional, enterprise and archive corpora are excluded from "
                "the persona packet and are retrieved only on demand."
            ),
        },
        "confirmed_self": {
            "note_count": len(personal),
            "top_themes": [
                {"name": name, "notes": count}
                for name, count in topic_counts.most_common(MAX_TOPIC_SIGNALS)
            ],
            "top_concepts": [
                {"name": name, "notes": count}
                for name, count in concept_counts.most_common(MAX_TOPIC_SIGNALS)
            ],
            "recent_memories": recent_memories,
        },
        "explicit_preferences": feedback,
        "observed_trajectory": _observed_trajectory(),
        "retrieval_policy": {
            "default": "personal_summary_only",
            "personal_recall": "search personal_memory first",
            "external_evidence": "opt-in and always labelled external",
            "archive": "disabled unless explicitly requested or core evidence is insufficient",
        },
        "context_budget": {
            "default_max_chars": DEFAULT_MAX_CHARS,
            "full_articles_in_hot_context": 0,
            "max_recent_memories": MAX_RECENT_MEMORIES,
            "max_recent_observations": MAX_RECENT_OBSERVATIONS,
        },
    }


def _fit_budget(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    fitted = copy.deepcopy(payload)
    maximum = max(1_500, min(int(max_chars or DEFAULT_MAX_CHARS), 20_000))
    shrink_paths = (
        ("confirmed_self", "recent_memories"),
        ("observed_trajectory", "recent"),
        ("confirmed_self", "top_concepts"),
        ("confirmed_self", "top_themes"),
        ("observed_trajectory", "topic_signals"),
        ("explicit_preferences", "preferred_sources"),
        ("explicit_preferences", "avoided_sources"),
        ("explicit_preferences", "preferred_categories"),
        ("explicit_preferences", "avoided_categories"),
    )
    while len(json.dumps(fitted, ensure_ascii=False)) > maximum:
        changed = False
        for parent, name in shrink_paths:
            values = fitted.get(parent, {}).get(name)
            if isinstance(values, list) and values:
                values.pop()
                changed = True
                if len(json.dumps(fitted, ensure_ascii=False)) <= maximum:
                    break
        if not changed:
            break
    if len(json.dumps(fitted, ensure_ascii=False)) > maximum:
        preferences = fitted.get("explicit_preferences") or {}
        fitted["explicit_preferences"] = {
            "signal_type": "explicit_feedback",
            "event_count": int(preferences.get("event_count") or 0),
        }
        fitted["purpose"] = "Compact personal context; retrieve details only on demand."
        fitted["identity_boundary"] = {
            "confirmed_self": "may represent the user",
            "observed_behavior": "attention only; never agreement",
            "external_reference": "retrieval only; never persona",
        }
        fitted.setdefault("observed_trajectory", {})["meaning"] = (
            "Observed attention only; never agreement, belief, mastery, or authorship."
        )
        fitted["retrieval_policy"] = {
            "default": "personal_summary_only",
            "external_evidence": "explicit opt-in",
            "archive": "explicit fallback only",
        }
        fitted.pop("loaded_at", None)
    fitted.setdefault("context_budget", {})["applied_max_chars"] = maximum
    fitted["context_budget"]["actual_chars"] = 0
    for _ in range(3):
        actual = len(json.dumps(fitted, ensure_ascii=False))
        if fitted["context_budget"]["actual_chars"] == actual:
            break
        fitted["context_budget"]["actual_chars"] = actual
    return fitted


def get_agent_context(max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    """Load the durable base and overlay the latest local reading trajectory."""

    payload = _load_json(
        CONTEXT_PATH,
        build_context([], {}),
    )
    payload["observed_trajectory"] = _observed_trajectory()
    payload["loaded_at"] = now_text()
    return _fit_budget(payload, max_chars)


def write_context(
    notes: Iterable[Any],
    vault: Path,
    preference_summary: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Persist machine-readable hot context and a human-readable Obsidian view."""

    payload = build_context(notes, preference_summary)
    _atomic_json(CONTEXT_PATH, payload)

    markdown_path = (
        vault / "20_Knowledge" / "AI上下文" / "AI个人上下文.md"
    )
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        "# AI 个人上下文",
        "",
        f"> 自动生成于 {payload['generated_at']}。这是常驻的轻量摘要，不是全文知识库。",
        "",
        "## 使用边界",
        "",
        "- 本人内容可以描述为“你的记录/判断”。",
        "- 浏览轨迹只代表看过或关注过，不能推断赞同、掌握或作者身份。",
        "- 外部研究与企业资料不进入个人画像；需要求证时再单独检索。",
        "",
        "## 已确认的个人主题",
        "",
    ]
    themes = payload["confirmed_self"]["top_themes"]
    rows.extend(
        f"- {item['name']}（{item['notes']} 条本人记录）"
        for item in themes
    )
    if not themes:
        rows.append("- 暂无已确认主题")
    rows += ["", "## 最近的本人记录", ""]
    memories = payload["confirmed_self"]["recent_memories"]
    rows.extend(
        f"- **{item['title']}**：{item['summary']}"
        for item in memories
    )
    if not memories:
        rows.append("- 暂无本人记录")
    rows += ["", "## 仅作观察的阅读轨迹", ""]
    observations = payload["observed_trajectory"]["recent"]
    rows.extend(
        f"- {item['observed_at']} · {item['title']}（仅代表浏览）"
        for item in observations
    )
    if not observations:
        rows.append("- 尚未积累新的本地浏览轨迹")
    rows += [
        "",
        "## AI 调用顺序",
        "",
        "1. 默认先读取本页对应的紧凑 JSON。",
        "2. 问到“我以前怎么想/何时想到”时，只检索 `personal_memory`。",
        "3. 需要事实与方法时，再按需加入外部证据，并保留来源身份。",
        "4. 原文库仅用于引用回溯，不进入常驻上下文。",
    ]
    markdown_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    legacy_path = (
        vault / "20_Knowledge" / "AI上下文" / "我的阅读画像.md"
    )
    legacy_path.write_text(
        "# 我的阅读画像\n\n"
        "> 旧版库存统计画像已停用。请使用 [[AI个人上下文]]；"
        "外部文章不会再被统计成你的兴趣或观点。\n",
        encoding="utf-8",
    )
    return CONTEXT_PATH, markdown_path


def status() -> dict[str, Any]:
    payload = _load_json(CONTEXT_PATH, {})
    return {
        "ready": CONTEXT_PATH.is_file(),
        "schema_version": payload.get("schema_version"),
        "generated_at": payload.get("generated_at", ""),
        "confirmed_note_count": int(
            (payload.get("confirmed_self") or {}).get("note_count") or 0
        ),
        "observed_signal_count": int(
            _observed_trajectory().get("signal_count") or 0
        ),
        "default_max_chars": DEFAULT_MAX_CHARS,
    }
