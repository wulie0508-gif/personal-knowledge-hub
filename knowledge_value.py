"""Classify knowledge value separately from extraction quality."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ValueAssessment:
    knowledge_type: str
    value_score: int
    priority: str
    reasons: list[str] = field(default_factory=list)
    summary: str = ""
    highlights: list[str] = field(default_factory=list)


MARKETING_TERMS = {
    "报名", "招募", "扫码", "加微信", "限时", "优惠", "名额", "咨询",
    "训练营", "课程", "领取", "购买", "入群", "点击下方",
}
NEWS_TERMS = {
    "刚刚", "发布", "来了", "将于", "今日", "最新", "独家", "首次",
    "开幕", "回应", "宣布", "突发", "消息", "周", "月", "年",
}
INSIGHT_TERMS = {
    "为什么", "底层逻辑", "本质", "趋势", "变化", "未来", "启示",
    "观察", "研判", "框架", "分水岭", "地图", "意味着", "关键",
}
METHOD_TERMS = {
    "如何", "指南", "教程", "步骤", "工作流", "方法", "实战", "落地",
    "清单", "知识点", "skill", "模板", "操作", "怎么做",
}
CASE_TERMS = {
    "案例", "实践", "项目", "企业", "供应链", "数据中心", "机器人",
    "组织", "团队", "岗位", "产业",
}
REASONING_TERMS = {
    "因为", "因此", "所以", "意味着", "本质", "关键", "原因", "结果",
    "前提", "相比", "不同", "不是", "而是", "由此", "背后",
}


def _hits(text: str, terms: set[str]) -> int:
    lowered = text.lower()
    return sum(lowered.count(term.lower()) for term in terms)


def _sentences(text: str) -> list[str]:
    values = re.split(r"(?<=[。！？；])\s*|\n+", text)
    result: list[str] = []
    for value in values:
        clean = re.sub(r"\s+", " ", value).strip(" -#>*")
        if 18 <= len(clean) <= 180 and clean not in result:
            result.append(clean)
    return result


def _highlights(text: str, limit: int = 3) -> list[str]:
    candidates: list[tuple[int, str]] = []
    for sentence in _sentences(text):
        score = _hits(sentence, REASONING_TERMS) * 4
        score += _hits(sentence, INSIGHT_TERMS) * 2
        score += min(4, len(sentence) // 35)
        if re.search(r"\d", sentence):
            score += 1
        candidates.append((score, sentence))
    selected: list[str] = []
    for _, sentence in sorted(candidates, key=lambda item: (-item[0], -len(item[1]))):
        if any(sentence[:12] in existing or existing[:12] in sentence for existing in selected):
            continue
        selected.append(sentence)
        if len(selected) >= limit:
            break
    return selected


def assess(
    *,
    title: str,
    body: str,
    quality_score: int,
    content_status: str,
) -> ValueAssessment:
    combined = f"{title}\n{body}"
    marketing = _hits(combined, MARKETING_TERMS)
    news = _hits(title, NEWS_TERMS)
    insights = _hits(combined, INSIGHT_TERMS)
    methods = _hits(combined, METHOD_TERMS)
    title_insights = _hits(title, INSIGHT_TERMS)
    title_methods = _hits(title, METHOD_TERMS)
    cases = _hits(combined, CASE_TERMS)
    reasoning = _hits(body, REASONING_TERMS)

    if marketing >= 2:
        knowledge_type = "营销活动"
    elif news >= 1 and title_insights == 0 and title_methods == 0:
        knowledge_type = "时效资讯"
    elif methods >= 2 or ("如何" in title and methods):
        knowledge_type = "方法工具"
    elif insights >= 2 or title_insights:
        knowledge_type = "观点见解"
    elif cases >= 3:
        knowledge_type = "案例研究"
    else:
        knowledge_type = "参考资料"

    score = quality_score
    reasons: list[str] = []
    type_adjustments = {
        "观点见解": 14,
        "方法工具": 12,
        "案例研究": 7,
        "参考资料": 0,
        "时效资讯": -20,
        "营销活动": -32,
    }
    adjustment = type_adjustments[knowledge_type]
    score += adjustment
    reasons.append(f"{knowledge_type} {adjustment:+d}")

    compact_length = len(re.sub(r"\s+", "", body))
    if compact_length >= 1500:
        score += 8
        reasons.append("信息深度 +8")
    elif compact_length < 300:
        score -= 10
        reasons.append("正文较短 -10")
    reasoning_bonus = min(12, reasoning * 2)
    if reasoning_bonus:
        score += reasoning_bonus
        reasons.append(f"论证密度 +{reasoning_bonus}")
    if content_status == "metadata_only":
        score -= 25
        reasons.append("缺少正文 -25")
    if marketing >= 4:
        penalty = min(18, marketing * 2)
        score -= penalty
        reasons.append(f"转化引导 -{penalty}")

    score = max(0, min(100, score))
    if knowledge_type in {"观点见解", "方法工具", "案例研究"} and score >= 78:
        priority = "重点"
    elif knowledge_type == "时效资讯":
        priority = "速览"
    elif score >= 50:
        priority = "参考"
    elif knowledge_type == "营销活动" or score < 30:
        priority = "回收建议"
    else:
        priority = "速览"

    highlights = _highlights(body)
    summary = " ".join(highlights[:2])
    if len(summary) > 260:
        summary = summary[:257].rstrip() + "…"
    return ValueAssessment(
        knowledge_type=knowledge_type,
        value_score=score,
        priority=priority,
        reasons=reasons,
        summary=summary,
        highlights=highlights,
    )
