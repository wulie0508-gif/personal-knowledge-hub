"""Build Obsidian links, topic maps and a local AI-searchable index."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import knowledge_schema
import personal_context
import runtime_config


ROOT = Path(__file__).resolve().parent
DATA_DIR = runtime_config.runtime_home()
DEFAULT_VAULT = runtime_config.configured_vault()
INDEX_PATH = DATA_DIR / "knowledge-index.sqlite3"
MATURITY_PATH = DATA_DIR / "reports" / "knowledge-maturity.json"
START_MARKER = "<!-- knowledge-links:start -->"
END_MARKER = "<!-- knowledge-links:end -->"
VALUE_START_MARKER = "<!-- knowledge-value:start -->"
VALUE_END_MARKER = "<!-- knowledge-value:end -->"
STOP_WORDS = {
    "一个", "这个", "那个", "我们", "你们", "他们", "自己", "什么", "为什么",
    "怎么", "可以", "以及", "就是", "已经", "还是", "可能", "进行", "目前",
    "文章", "正文", "来源", "公众", "平台", "微信", "内容", "图片", "作者",
}
CORE_EXCLUDED_PRIORITIES = {"速览", "回收建议"}
GRAPH_TIERS = {"personal", "enterprise", "core", "reference"}
OBSIDIAN_GRAPH_FILTER = (
    '(path:"20_Knowledge" OR path:"10_Sources/WeChat/Articles" OR '
    'path:"10_Sources/Local/Articles" OR path:"10_Sources/Xiaohongshu" OR '
    'path:"10_Sources/Feishu")'
)
MATURITY_WINDOW = 100
MIN_DEEP_CURATED_FOR_STOP = 300
MAX_DEEP_CURATED_BEFORE_STOP = 600
REFERENCE_LIBRARY_MODE = True
MIN_CONCEPT_COVERAGE_FOR_STOP = 0.85
MIN_YEAR_COVERAGE_FOR_STOP = 0.80
MAX_NEW_CONCEPT_RATE_FOR_STOP = 0.05
MAX_NEW_CONCEPT_RATE_AT_BUDGET_CAP = 0.10
MAX_HIGH_VALUE_RATE_FOR_STOP = 0.15
MIN_REDUNDANCY_RATE_FOR_STOP = 0.60
MAX_DOCUMENT_TOKENS = 600
MAX_VECTOR_TOKENS = 96
MAX_CANDIDATE_TOKENS = 14
MAX_TOKEN_POSTINGS = 240
MAX_CANDIDATES = 180
CONCEPT_RULES: dict[str, tuple[str, ...]] = {
    "智能体与工作流": ("agent", "智能体", "工作流", "workflow", "codex", "claude code"),
    "检索增强生成": ("rag", "检索增强", "向量数据库", "知识库"),
    "大语言模型": ("大模型", "llm", "transformer", "gpt", "claude", "gemini", "kimi", "qwen"),
    "具身智能与机器人": ("具身智能", "机器人", "physical ai", "robot"),
    "提示词与上下文工程": ("prompt", "提示词", "上下文工程", "context engineering"),
    "模型训练与微调": ("模型训练", "微调", "fine-tuning", "finetuning", "rlhf", "强化学习"),
    "数据与评测": ("数据集", "benchmark", "基准测试", "模型评测", "评测框架"),
    "算力与数据中心": ("算力", "数据中心", "gpu", "芯片", "液冷", "冷却"),
    "AI 产品与商业化": ("ai 产品", "ai产品", "商业化", "变现", "monetization"),
    "产品设计与用户体验": ("产品设计", "用户体验", "ux", "交互设计"),
    "组织与职业": ("组织设计", "团队", "岗位", "招聘", "职业"),
    "创业与投资": ("创业", "融资", "投资", "风险投资", "创始人"),
    "增长与内容": ("增长", "内容运营", "营销", "流量"),
    "个人知识管理": ("obsidian", "知识管理", "第二大脑", "个人知识库"),
    "证据与研究": ("证据链", "研究方法", "尽调", "evidence"),
    "AI 治理与合规": ("ai治理", "人工智能治理", "合规", "隐私", "监管"),
    "开源生态": ("开源", "github", "open source"),
    "教育与学习": ("教育", "大学", "学校", "学习方法"),
    "清洁能源": ("清洁能源", "光伏", "风电", "储能", "氢能"),
    "可持续商业": ("esg", "可持续", "减碳", "碳排"),
    "供应链与制造": ("供应链", "制造业", "工厂", "产业链"),
}


@dataclass
class Note:
    path: Path
    title: str
    url: str
    platform: str
    account: str
    category: str
    quality: int
    knowledge_type: str
    priority: str
    curation_status: str
    mastery_status: str
    content_status: str
    value_score: int
    value_summary: str
    highlights: tuple[str, ...]
    publish_date: str
    curated_at: str
    corpus_namespace: str
    authorship: str
    confidentiality: str
    engagement_status: str
    stance: str
    persona_influence: float
    identity_explicit: bool
    corpus_tier: str
    retrieval_weight: float
    body: str
    tokens: Counter[str]
    concepts: tuple[str, ...]


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _field(text: str, name: str, default: Any = "") -> Any:
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", text, re.MULTILINE)
    if not match:
        return default
    raw = match.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip("\"'")


def _without_generated_links(text: str) -> str:
    text = re.sub(
        rf"\n?{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}\n?",
        "\n",
        text,
        flags=re.DOTALL,
    )
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return text


def _curation_payload(text: str) -> tuple[str, tuple[str, ...]]:
    match = re.search(
        rf"{re.escape(VALUE_START_MARKER)}(.*?){re.escape(VALUE_END_MARKER)}",
        text,
        flags=re.DOTALL,
    )
    if not match:
        return "", ()
    block = match.group(1)
    summary_match = re.search(
        r"## Codex 知识整理\s*\n+(.*?)(?=\n### |\n> 分类理由：|\Z)",
        block,
        flags=re.DOTALL,
    )
    summary = (
        re.sub(r"\s+", " ", summary_match.group(1)).strip()
        if summary_match
        else ""
    )
    highlights_match = re.search(
        r"### 可复用的关键点\s*\n+(.*?)(?=\n## |\n> 分类理由：|\Z)",
        block,
        flags=re.DOTALL,
    )
    highlights: list[str] = []
    if highlights_match:
        highlights = [
            re.sub(r"\s+", " ", item).strip()
            for item in re.findall(r"^\s*-\s+(.+)$", highlights_match.group(1), re.MULTILINE)
            if item.strip()
        ][:5]
    return summary, tuple(highlights)


def _corpus_tier(
    *,
    platform: str,
    curation_status: str,
    priority: str,
    corpus_namespace: str,
) -> tuple[str, float]:
    if corpus_namespace == knowledge_schema.SOURCE_ARCHIVE:
        return "evidence", 0.20
    if corpus_namespace == knowledge_schema.PERSONAL_MEMORY:
        return "personal", 1.45
    if corpus_namespace == knowledge_schema.ENTERPRISE_INTERNAL:
        return "enterprise", 1.35
    if priority == "回收建议":
        return "evidence", 0.20
    if curation_status != "complete":
        return "evidence", 0.30
    if priority == "重点":
        return "core", 1.30
    if priority == "参考":
        return "reference", 1.00
    if priority == "速览":
        return "brief", 0.62
    return "reference", 0.90


def tokenize(text: str) -> Counter[str]:
    text = re.sub(r"\[\[[^|\]]+\|([^\]]+)\]\]", r"\1", text)
    text = re.sub(
        r"\[\[([^\]#|]+)(?:#[^\]]*)?\]\]",
        lambda match: match.group(1).replace("\\", "/").rsplit("/", 1)[-1],
        text,
    )
    values: list[str] = []
    values.extend(
        word.lower()
        for word in re.findall(r"[A-Za-z][A-Za-z0-9+._-]{2,}", text)
    )
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if len(segment) <= 6:
            values.append(segment)
        values.extend(segment[index : index + 2] for index in range(len(segment) - 1))
    counter = Counter(
        value
        for value in values
        if value not in STOP_WORDS and len(value.strip()) >= 2
    )
    return Counter(dict(counter.most_common(MAX_DOCUMENT_TOKENS)))


def extract_concepts(title: str, body: str) -> tuple[str, ...]:
    normalized_title = title.casefold()
    normalized_body = body.casefold()

    def keyword_count(text: str, keyword: str) -> int:
        normalized_keyword = keyword.casefold()
        if re.search(r"[a-z]", normalized_keyword):
            return len(
                re.findall(
                    rf"(?<![a-z0-9]){re.escape(normalized_keyword)}(?![a-z0-9])",
                    text,
                )
            )
        return text.count(normalized_keyword)

    scored: list[tuple[int, str]] = []
    for concept, keywords in CONCEPT_RULES.items():
        title_hits = sum(keyword_count(normalized_title, keyword) for keyword in keywords)
        body_hits = sum(keyword_count(normalized_body, keyword) for keyword in keywords)
        if title_hits or body_hits >= 2:
            scored.append((title_hits * 8 + min(body_hits, 8), concept))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(concept for _, concept in scored[:6])


def document_tokens(title: str, body: str) -> Counter[str]:
    tokens = tokenize(body)
    for token, frequency in tokenize(title).items():
        tokens[token] += frequency * 5
    return Counter(dict(tokens.most_common(MAX_DOCUMENT_TOKENS)))


def read_notes(article_roots: list[Path]) -> list[Note]:
    notes: list[Note] = []
    seen: set[Path] = set()
    seen_urls: set[str] = set()
    for article_root in article_roots:
        if not article_root.exists():
            continue
        for path in article_root.rglob("*.md"):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            text = path.read_text(encoding="utf-8", errors="replace")
            platform = str(_field(text, "platform"))
            source_url = str(_field(text, "sourceUrl") or _field(text, "source_url"))
            is_raw_wechat = (
                article_root.name == "WeChat"
                and path.parent.resolve() == article_root.resolve()
            )
            if is_raw_wechat:
                scope = str(_field(text, "knowledge_scope"))
                account = str(_field(text, "account"))
                if scope != "selected" and account != "腾讯研究院":
                    continue
                if not platform and canonical_source_url(source_url).startswith(
                    "https://mp.weixin.qq.com/"
                ):
                    platform = "wechat_mp"
            if platform not in {"wechat_mp", "local", "xiaohongshu", "feishu"}:
                continue
            canonical = canonical_source_url(source_url)
            if canonical and canonical in seen_urls:
                continue
            if canonical:
                seen_urls.add(canonical)
            body = _without_generated_links(text)
            title = str(_field(text, "title", path.stem))
            priority = str(_field(text, "knowledge_priority", "参考"))
            curation_status = str(_field(text, "curation_status", "pending"))
            value_summary, highlights = _curation_payload(text)
            explicit_namespace = knowledge_schema.normalize_namespace(
                _field(text, "corpus_namespace")
            )
            corpus_namespace = knowledge_schema.infer_namespace(
                platform=platform,
                path_text=str(path),
                explicit=explicit_namespace,
                is_raw_evidence=is_raw_wechat and curation_status != "complete",
            )
            identity = knowledge_schema.identity_metadata(
                namespace=corpus_namespace,
                fields={
                    "authorship": _field(text, "authorship"),
                    "confidentiality": _field(text, "confidentiality"),
                    "engagement_status": _field(text, "engagement_status"),
                    "stance": _field(text, "stance"),
                    "persona_influence": _field(text, "persona_influence"),
                },
            )
            corpus_tier, retrieval_weight = _corpus_tier(
                platform=platform,
                curation_status=curation_status,
                priority=priority,
                corpus_namespace=corpus_namespace,
            )
            notes.append(
                Note(
                    path=path,
                    title=title,
                    url=source_url or str(_field(text, "source")),
                    platform=platform,
                    account=str(_field(text, "account") or _field(text, "author") or platform),
                    category=str(_field(text, "category", "其他")),
                    quality=int(_field(text, "quality_score", 60) or 60),
                    knowledge_type=str(_field(text, "knowledge_type", "参考资料")),
                    priority=priority,
                    curation_status=curation_status,
                    mastery_status=str(_field(text, "mastery_status", "未学习")),
                    content_status=str(_field(text, "content_status", "complete")),
                    value_score=int(
                        _field(text, "knowledge_value_score", 0) or 0
                    ),
                    value_summary=value_summary,
                    highlights=highlights,
                    publish_date=str(_field(text, "publish_date", "")),
                    curated_at=str(_field(text, "curated_at", "")),
                    corpus_namespace=corpus_namespace,
                    authorship=str(identity["authorship"]),
                    confidentiality=str(identity["confidentiality"]),
                    engagement_status=str(identity["engagement_status"]),
                    stance=str(identity["stance"]),
                    persona_influence=float(identity["persona_influence"]),
                    identity_explicit=bool(explicit_namespace),
                    corpus_tier=corpus_tier,
                    retrieval_weight=retrieval_weight,
                    body=body,
                    tokens=document_tokens(title, body),
                    concepts=extract_concepts(title, body),
                )
            )
    return notes


def related_notes(notes: list[Note]) -> dict[Path, list[tuple[Note, float, list[str]]]]:
    def relation_type(
        source: Note,
        target: Note,
        *,
        explicit: bool,
        shared_concepts: list[str],
    ) -> str:
        """Name the relationship conservatively for readers and local AI tools.

        Lexical similarity alone cannot prove that two sources corroborate or
        contradict each other, so generated links use neutral relationship
        labels. Stronger claims are reserved for explicit links or for
        method/case and external-research/project pairings whose roles are
        visible in metadata.
        """

        if explicit:
            return "显式引用"
        namespaces = {source.corpus_namespace, target.corpus_namespace}
        if knowledge_schema.PERSONAL_MEMORY in namespaces and len(namespaces) > 1:
            return "外部研究—用户项目迁移"
        type_pair = {source.knowledge_type, target.knowledge_type}
        if type_pair == {"方法工具", "案例研究"}:
            return "方法—案例迁移"
        if type_pair == {"观点见解", "参考资料"}:
            return "观点—证据对照"
        if source.knowledge_type == target.knowledge_type == "案例研究":
            return "跨案例对照"
        if source.account and source.account == target.account:
            return "同源研究脉络"
        if shared_concepts:
            return "跨来源主题对照"
        return "语义延伸"

    def diversify(
        source: Note,
        candidates: list[tuple[Note, float, list[str]]],
    ) -> list[tuple[Note, float, list[str]]]:
        """Keep a useful mix instead of six near-duplicate same-source links."""

        ordered = sorted(
            candidates,
            key=lambda item: (-item[1], -item[0].quality, item[0].title),
        )
        chosen: list[tuple[Note, float, list[str]]] = []
        per_account: Counter[str] = Counter()
        for item in ordered:
            target = item[0]
            account_key = target.account or target.platform or "unknown"
            same_source = bool(source.account and target.account == source.account)
            cap = 3 if same_source else 2
            if per_account[account_key] >= cap:
                continue
            chosen.append(item)
            per_account[account_key] += 1
            if len(chosen) >= 6:
                return chosen
        # A shorter set of distinct, defensible links is better than filling
        # six slots with near-duplicate articles from the same publisher.
        return chosen

    def note_aliases(note: Note) -> set[str]:
        aliases = {note.title.strip(), note.path.stem.strip()}
        parts = list(note.path.with_suffix("").parts)
        for anchor in ("10_Sources", "20_Knowledge"):
            if anchor in parts:
                aliases.add("/".join(parts[parts.index(anchor) :]))
        aliases.add(note.path.with_suffix("").as_posix())
        return {alias.replace("\\", "/").strip("/") for alias in aliases if alias}

    alias_map: dict[str, Path] = {}
    for note in notes:
        for alias in note_aliases(note):
            alias_map.setdefault(alias, note.path)

    explicit_links: dict[Path, set[Path]] = defaultdict(set)
    reverse_explicit_links: dict[Path, set[Path]] = defaultdict(set)
    for note in notes:
        for raw_target in re.findall(r"\[\[([^|\]#]+)", note.body):
            target = raw_target.strip().replace("\\", "/").strip("/")
            if target.lower().endswith(".md"):
                target = target[:-3]
            resolved = alias_map.get(target)
            if resolved and resolved != note.path:
                explicit_links[note.path].add(resolved)
                reverse_explicit_links[resolved].add(note.path)

    core_notes = [note for note in notes if note.corpus_tier in GRAPH_TIERS]
    document_frequency: Counter[str] = Counter()
    for note in core_notes:
        document_frequency.update(note.tokens.keys())
    count = max(len(core_notes), 1)
    vectors: dict[Path, dict[str, float]] = {}
    norms: dict[Path, float] = {}
    for note in core_notes:
        weighted = [
            (
                token,
                (1 + math.log(freq))
                * math.log((count + 1) / (document_frequency[token] + 1)),
            )
            for token, freq in note.tokens.items()
        ]
        weighted.sort(key=lambda item: item[1], reverse=True)
        vector = dict(weighted[:MAX_VECTOR_TOKENS])
        vectors[note.path] = vector
        norms[note.path] = math.sqrt(sum(value * value for value in vector.values()))

    note_by_path = {note.path: note for note in notes}
    token_postings: dict[str, list[Path]] = defaultdict(list)
    for note in core_notes:
        for token in vectors[note.path]:
            frequency = document_frequency[token]
            if 2 <= frequency <= MAX_TOKEN_POSTINGS:
                token_postings[token].append(note.path)
    concept_postings: dict[str, list[Path]] = defaultdict(list)
    project_concept_postings: dict[str, list[Path]] = defaultdict(list)
    for note in core_notes:
        for concept in note.concepts:
            concept_postings[concept].append(note.path)
            if note.corpus_namespace == knowledge_schema.PERSONAL_MEMORY:
                project_concept_postings[concept].append(note.path)
    for paths in concept_postings.values():
        paths.sort(key=lambda path: note_by_path[path].quality, reverse=True)

    result: dict[Path, list[tuple[Note, float, list[str]]]] = {}
    for source in notes:
        if source.corpus_tier not in GRAPH_TIERS:
            result[source.path] = []
            continue
        candidates: list[tuple[Note, float, list[str]]] = []
        source_vector = vectors[source.path]
        candidate_votes: Counter[Path] = Counter()
        informative_tokens = sorted(
            source_vector,
            key=lambda token: source_vector[token],
            reverse=True,
        )[:MAX_CANDIDATE_TOKENS]
        for token in informative_tokens:
            for target_path in token_postings.get(token, []):
                if target_path != source.path:
                    candidate_votes[target_path] += 1
        for concept in source.concepts:
            added = 0
            for target_path in concept_postings.get(concept, []):
                if target_path == source.path:
                    continue
                candidate_votes[target_path] += 3
                added += 1
                if added >= 60:
                    break
            # Always consider the user's own project/method notes for a
            # semantically matching external source. Without this dedicated
            # posting list, thousands of Tencent articles crowd the smaller
            # local corpus out of the candidate window.
            if source.corpus_namespace != knowledge_schema.PERSONAL_MEMORY:
                for target_path in project_concept_postings.get(concept, []):
                    if target_path != source.path:
                        candidate_votes[target_path] += 8
        for target_path in explicit_links[source.path]:
            candidate_votes[target_path] += 100
        for source_path in reverse_explicit_links[source.path]:
            candidate_votes[source_path] += 100
        candidate_paths = [
            path
            for path, _ in candidate_votes.most_common(MAX_CANDIDATES)
            if path in vectors
        ]
        for target_path in candidate_paths:
            target = note_by_path[target_path]
            target_vector = vectors[target.path]
            shared = set(source_vector).intersection(target_vector)
            denominator = norms[source.path] * norms[target.path]
            cosine = (
                sum(source_vector[token] * target_vector[token] for token in shared)
                / denominator
                if denominator
                else 0.0
            )
            score = cosine
            reasons: list[str] = []
            source_points_to_target = target.path in explicit_links[source.path]
            target_points_to_source = source.path in explicit_links[target.path]
            if source_points_to_target or target_points_to_source:
                score += 0.65
                reasons.append(
                    "显式知识链" if source_points_to_target else "被引用知识链"
                )
            if source_points_to_target and target_points_to_source:
                score += 0.12
                reasons.append("双向引用")
            shared_concepts = sorted(set(source.concepts).intersection(target.concepts))
            if shared_concepts:
                score += min(0.26, 0.12 + 0.04 * len(shared_concepts))
                reasons.append("共同概念：" + "、".join(shared_concepts[:3]))
            if source.category == target.category and source.category != "其他":
                score += 0.04
                reasons.append(f"同属{source.category}")
            keywords = sorted(
                (
                    token
                    for token in shared
                    if re.search(r"[A-Za-z]", token) or len(token) >= 3
                ),
                key=lambda token: source_vector[token] * target_vector[token],
                reverse=True,
            )[:3]
            if keywords:
                reasons.append("共同主题：" + "、".join(keywords))
            explicit = source_points_to_target or target_points_to_source
            project_pair = (
                source.corpus_namespace != target.corpus_namespace
                and knowledge_schema.PERSONAL_MEMORY
                in {source.corpus_namespace, target.corpus_namespace}
            )
            external_note = (
                target
                if source.corpus_namespace == knowledge_schema.PERSONAL_MEMORY
                else source
            )
            if (
                project_pair
                and external_note.curation_status != "complete"
                and not explicit
            ):
                # Raw source text remains searchable, but a high-meaning edge
                # into the user's own methods/projects requires article-level
                # curation and an explicit evidence boundary first.
                continue
            kind = relation_type(
                source,
                target,
                explicit=explicit,
                shared_concepts=shared_concepts,
            )
            reasons.insert(0, kind)
            if source.account and source.account != target.account:
                reasons.append(f"来源对照：{source.account} ↔ {target.account}")
                if len(shared_concepts) >= 2 and cosine >= 0.10:
                    score += 0.02
            elif source.account:
                reasons.append(f"同一来源：{source.account}")
            is_project_migration = project_pair
            strong_project_semantics = (
                len(shared_concepts) >= 2
                or len(shared_concepts) == 1
                and bool(keywords)
                and cosine >= 0.10
            )
            if is_project_migration and strong_project_semantics:
                # The external article has already passed article-level Codex
                # curation. Two independent concept matches therefore form a
                # conservative "possible application" edge; a single concept
                # still needs a discriminating topic and lexical similarity.
                score += 0.08
            elif is_project_migration and shared_concepts and cosine >= 0.10:
                score += 0.05
            semantic_evidence = (
                bool(shared_concepts)
                and cosine >= 0.10
                or len(keywords) >= 2
                and cosine >= 0.18
                or is_project_migration
                and strong_project_semantics
                or is_project_migration
                and bool(shared_concepts)
                and len(keywords) >= 2
                and cosine >= 0.10
            )
            # Cross-domain links shape how an AI interprets the user. Keep them
            # out of the formal graph unless the evidence is materially
            # stronger than ordinary document similarity. Weak candidates may
            # still be found through retrieval without becoming persona edges.
            minimum_score = 0.42 if project_pair else 0.30
            project_edge_allowed = (
                not project_pair or strong_project_semantics
            )
            if explicit or (
                score >= minimum_score
                and semantic_evidence
                and project_edge_allowed
            ):
                candidates.append((target, score, reasons))
        result[source.path] = diversify(source, candidates)
    return result


def _wikilink(path: Path, vault: Path, title: str) -> str:
    relative = os.path.relpath(path, vault).replace("\\", "/")
    return f"[[{relative[:-3]}|{title}]]"


def write_note_links(
    notes: list[Note],
    links: dict[Path, list[tuple[Note, float, list[str]]]],
    vault: Path,
) -> int:
    link_count = 0
    for note in notes:
        text = note.path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(
            rf"\n?{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}\n?",
            "\n",
            text,
            flags=re.DOTALL,
        ).rstrip()
        rows = [START_MARKER, "## 知识关联", ""]
        if note.corpus_tier in GRAPH_TIERS and note.concepts:
            concept_links = " · ".join(
                f"[[20_Knowledge/核心概念/{concept}|{concept}]]"
                for concept in note.concepts
            )
            rows += [f"- 概念节点：{concept_links}", ""]
        for target, score, reasons in links.get(note.path, []):
            rows.append(
                f"- {_wikilink(target.path, vault, target.title)}"
                f" — {'；'.join(reasons)}（关联度 {score:.2f}）"
            )
            link_count += 1
        if not links.get(note.path):
            rows.append("- 暂无足够强的关联；后续文章进入后会自动重算。")
        rows += ["", END_MARKER]
        note.path.write_text(text + "\n\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return link_count


def write_topic_maps(notes: list[Note], vault: Path) -> int:
    topic_root = vault / "20_Knowledge" / "知识主题"
    topic_root.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[Note]] = defaultdict(list)
    for note in notes:
        if note.corpus_tier not in GRAPH_TIERS:
            continue
        grouped[note.category or "其他"].append(note)
    expected: set[Path] = set()
    for category, items in grouped.items():
        safe = re.sub(r'[\\/:*?"<>|]', " ", category).strip() or "其他"
        path = topic_root / f"{safe}.md"
        expected.add(path)
        rows = [
            "---",
            'type: "topic-map"',
            f"topic: {json.dumps(category, ensure_ascii=False)}",
            f"updated_at: {json.dumps(now_text(), ensure_ascii=False)}",
            "---",
            "",
            f"# {category}",
            "",
            f"> 自动聚合 {len(items)} 份跨来源资料；内容增删后自动更新。",
            "",
        ]
        ranked_items = sorted(
            items,
            key=lambda value: (
                -value.retrieval_weight,
                -value.value_score,
                -value.quality,
                value.title,
            ),
        )
        for item in ranked_items[:60]:
            rows.append(
                f"- {_wikilink(item.path, vault, item.title)}"
                f" — {item.account or '未知公众号'} · {item.knowledge_type}"
                f" · {item.value_score or item.quality}/100"
            )
        if len(ranked_items) > 60:
            rows += [
                "",
                f"> 仅展示最高价值的 60 篇；其余 {len(ranked_items) - 60} 篇仍可由本地索引检索。",
            ]
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    for stale in topic_root.glob("*.md"):
        if stale not in expected:
            stale.unlink()
    return len(grouped)


def write_concept_maps(notes: list[Note], vault: Path) -> int:
    concept_root = vault / "20_Knowledge" / "核心概念"
    concept_root.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[Note]] = defaultdict(list)
    for note in notes:
        if note.corpus_tier not in GRAPH_TIERS:
            continue
        for concept in note.concepts:
            grouped[concept].append(note)
    expected: set[Path] = set()
    for concept, items in grouped.items():
        safe = re.sub(r'[\\/:*?"<>|]', " ", concept).strip() or "其他"
        path = concept_root / f"{safe}.md"
        expected.add(path)
        sources = Counter(item.account for item in items if item.account)
        rows = [
            "---",
            'type: "concept-map"',
            f"concept: {json.dumps(concept, ensure_ascii=False)}",
            f"updated_at: {json.dumps(now_text(), ensure_ascii=False)}",
            "---",
            "",
            f"# {concept}",
            "",
            f"> 自动形成的跨来源概念节点：{len(items)} 篇资料，"
            f"{len(sources)} 个来源。这里只收录重点与参考内容。",
            "",
            "## 熔炼结论",
            "",
        ]
        ranked_items = sorted(
            items,
            key=lambda value: (
                -value.retrieval_weight,
                -value.value_score,
                -value.quality,
                value.account,
                value.title,
            ),
        )
        synthesis_count = 0
        for item in ranked_items:
            if not item.value_summary and not item.highlights:
                continue
            insight = item.value_summary or item.highlights[0]
            insight = re.sub(r"\s+", " ", insight).strip()
            if len(insight) > 220:
                insight = insight[:217].rstrip() + "…"
            rows.append(
                f"- **{item.knowledge_type}**：{insight} "
                f"— {_wikilink(item.path, vault, item.title)}"
            )
            synthesis_count += 1
            if synthesis_count >= 12:
                break
        if not synthesis_count:
            rows.append("- 当前尚无经过 Codex 整理的可复用结论。")
        rows += ["", "## 代表性证据来源", ""]
        for item in ranked_items[:40]:
            rows.append(
                f"- {_wikilink(item.path, vault, item.title)}"
                f" — {item.account or '未知来源'} · {item.knowledge_type}"
                f" · {item.value_score or item.quality}/100"
            )
        if len(ranked_items) > 40:
            rows += [
                "",
                f"> 为保持图谱轻量，仅连接 40 篇代表性证据；其余 {len(ranked_items) - 40} 篇保留在检索索引。",
            ]
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    for stale in concept_root.glob("*.md"):
        if stale not in expected:
            stale.unlink()
    return len(grouped)


def write_news_digest(notes: list[Note], vault: Path) -> Path:
    path = vault / "20_Knowledge" / "资讯速览.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    quick_reads = [
        note
        for note in notes
        if note.priority in {"速览", "回收建议"}
    ]
    rows = [
        "# 资讯速览",
        "",
        f"> 更新时间：{now_text()} · {len(quick_reads)} 篇。这里不进入核心知识图谱。",
        "",
    ]
    for note in sorted(quick_reads, key=lambda item: (-item.quality, item.title)):
        rows.append(
            f"- {_wikilink(note.path, vault, note.title)}"
            f" — {note.knowledge_type} · {note.priority} · {note.quality}/100"
        )
    if not quick_reads:
        rows.append("- 当前没有速览内容。")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    legacy = vault / "20_Knowledge" / "微信资讯速览.md"
    legacy.write_text(
        "# 微信资讯速览\n\n> 已并入跨来源的 [[20_Knowledge/资讯速览|资讯速览]]。\n",
        encoding="utf-8",
    )
    return path


def write_overview(
    notes: list[Note],
    links: dict[Path, list[tuple[Note, float, list[str]]]],
    vault: Path,
    archive_count: int,
) -> Path:
    path = vault / "20_Knowledge" / "个人知识星球.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    core_notes = [note for note in notes if note.corpus_tier in GRAPH_TIERS]
    categories = Counter(note.category for note in core_notes)
    accounts = Counter(note.account for note in core_notes if note.account)
    rows = [
        "# 个人知识星球",
        "",
        f"> 更新时间：{now_text()} · {len(core_notes)} 篇长期知识"
        f"（另有 {len(notes) - len(core_notes)} 篇速览）"
        f" · {archive_count} 篇可追溯原文进入本地证据索引",
        "",
        "## 可视化入口",
        "",
        "- [[20_Knowledge/作品集知识星系.canvas|作品集知识星系 Canvas]]：个人能力、代表作品与核心方法的固定星系图。",
        "- [[20_Knowledge/NEX知识星系.canvas|NEX知识星系 Canvas]]：企业证据、RAG、内容产品、QA 与运营交付的项目星系图。",
        "- [[20_Knowledge/清桥商业化知识星系.canvas|清桥商业化知识星系 Canvas]]：获客、Evidence 闸门、Agent、专家、产品阶梯、盈利模型与合规边界。",
        "- Obsidian 左侧「关系图谱」：只显示个人语料、重点、参考、主题与概念；未整理原文和速览默认隐藏。",
        "- 本地 Agent 检索顺序：个人语料 → 重点方法/观点 → 参考资料 → 速览 → 原文证据库。",
        "- [[20_Knowledge/AI上下文/语料熔炼与停采标准|语料熔炼与停采标准]]：查看当前成熟度和何时停止继续深挖。",
        "",
        "## 主题入口",
        "",
    ]
    for category, count in categories.most_common():
        rows.append(f"- [[20_Knowledge/知识主题/{category}|{category}]]（{count}）")
    concept_counts = Counter(
        concept for note in core_notes for concept in note.concepts
    )
    rows += ["", "## 核心概念入口", ""]
    for concept, count in concept_counts.most_common():
        rows.append(f"- [[20_Knowledge/核心概念/{concept}|{concept}]]（{count}）")
    rows += ["", "## 高频来源", ""]
    for account, count in accounts.most_common(12):
        rows.append(f"- {account}：{count} 篇")
    rows += ["", "## 关联最丰富的文章", ""]
    for note in sorted(core_notes, key=lambda item: len(links.get(item.path, [])), reverse=True)[:12]:
        rows.append(
            f"- {_wikilink(note.path, vault, note.title)}"
            f"（{len(links.get(note.path, []))} 条关联）"
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    legacy = vault / "20_Knowledge" / "微信知识图谱.md"
    legacy.write_text(
        "# 微信知识图谱\n\n> 已升级为跨来源的 [[20_Knowledge/个人知识星球|个人知识星球]]。\n",
        encoding="utf-8",
    )
    return path


def write_ai_profile(notes: list[Note], vault: Path, preference_summary: dict[str, Any]) -> Path:
    """Compatibility wrapper for the bounded, identity-safe context writer."""

    _context_path, markdown_path = personal_context.write_context(
        notes,
        vault,
        preference_summary,
    )
    return markdown_path


def _note_year(note: Note) -> str:
    match = re.search(r"\b(20\d{2})\b", note.publish_date or note.path.name)
    return match.group(1) if match else ""


def _token_set(text: str) -> set[str]:
    return set(tokenize(text).keys())


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def write_maturity_report(
    notes: list[Note],
    raw_paths: list[Path],
    links: dict[Path, list[tuple[Note, float, list[str]]]],
    vault: Path,
) -> tuple[Path, dict[str, Any]]:
    deep_notes = [
        note
        for note in notes
        if note.platform != "local"
        and note.curation_status == "complete"
        and note.corpus_tier in GRAPH_TIERS | {"brief"}
    ]
    deep_notes.sort(
        key=lambda note: (
            note.curated_at or "",
            note.path.stat().st_mtime_ns if note.path.exists() else 0,
            note.title,
        )
    )
    window_notes = deep_notes[-MATURITY_WINDOW:]
    previous_notes = deep_notes[:-MATURITY_WINDOW]
    previous_concepts = {
        concept for note in previous_notes for concept in note.concepts
    }
    window_concepts = {
        concept for note in window_notes for concept in note.concepts
    }
    new_concepts = window_concepts - previous_concepts
    new_concept_rate = (
        len(new_concepts) / max(len(window_concepts), 1)
        if window_notes
        else 1.0
    )
    high_value_rate = (
        sum(note.corpus_tier == "core" for note in window_notes)
        / max(len(window_notes), 1)
    )

    previous_vectors = [
        _token_set(note.value_summary or " ".join(note.highlights) or note.title)
        for note in previous_notes[-500:]
    ]
    seen_concepts = set(previous_concepts)
    redundant = 0
    for note in window_notes:
        vector = _token_set(
            note.value_summary or " ".join(note.highlights) or note.title
        )
        lexical_similarity = max(
            (_jaccard(vector, candidate) for candidate in previous_vectors),
            default=0.0,
        )
        note_concepts = set(note.concepts)
        concept_repetition = (
            len(note_concepts & seen_concepts) / len(note_concepts)
            if note_concepts
            else 0.0
        )
        # Broad concepts such as “AI 治理” repeat very early and cannot by
        # themselves prove that a new article is redundant. Require lexical
        # overlap as a second signal, while still allowing near-duplicate
        # summaries to qualify directly.
        if lexical_similarity >= 0.15 or (
            concept_repetition >= 0.80 and lexical_similarity >= 0.08
        ):
            redundant += 1
        previous_vectors.append(vector)
        seen_concepts.update(note_concepts)
    redundancy_rate = redundant / max(len(window_notes), 1)

    concept_counts = Counter(
        concept for note in deep_notes for concept in note.concepts
    )
    concept_coverage = (
        sum(count >= 3 for count in concept_counts.values())
        / max(len(CONCEPT_RULES), 1)
    )
    archive_years = {
        match.group(1)
        for path in raw_paths
        if _raw_note_has_usable_body(path)
        if (match := re.search(r"\b(20\d{2})\b", path.name))
    }
    curated_year_counts = Counter(
        year for note in deep_notes if (year := _note_year(note))
    )
    covered_years = {
        year
        for year in archive_years
        if curated_year_counts.get(year, 0) >= 5
    }
    year_coverage = len(covered_years) / max(len(archive_years), 1)

    stop_checks = {
        "deep_curated_count": len(deep_notes) >= MIN_DEEP_CURATED_FOR_STOP,
        "concept_coverage": concept_coverage >= MIN_CONCEPT_COVERAGE_FOR_STOP,
        "year_coverage": year_coverage >= MIN_YEAR_COVERAGE_FOR_STOP,
        "new_concept_rate": new_concept_rate <= MAX_NEW_CONCEPT_RATE_FOR_STOP,
        "high_value_rate": high_value_rate <= MAX_HIGH_VALUE_RATE_FOR_STOP,
        "redundancy_rate": redundancy_rate >= MIN_REDUNDANCY_RATE_FOR_STOP,
    }
    coverage_ready = (
        stop_checks["concept_coverage"] and stop_checks["year_coverage"]
    )
    diminishing_returns = (
        stop_checks["deep_curated_count"]
        and stop_checks["new_concept_rate"]
        and stop_checks["high_value_rate"]
        and stop_checks["redundancy_rate"]
    )
    budget_cap_reached = (
        len(deep_notes) >= MAX_DEEP_CURATED_BEFORE_STOP
        and new_concept_rate <= MAX_NEW_CONCEPT_RATE_AT_BUDGET_CAP
    )
    representative_sample_ready = (
        REFERENCE_LIBRARY_MODE
        and len(deep_notes) >= MIN_DEEP_CURATED_FOR_STOP
        and coverage_ready
    )
    stop_recommended = coverage_ready and (
        diminishing_returns
        or budget_cap_reached
        or representative_sample_ready
    )
    if stop_recommended:
        stage = "saturated"
        next_action = (
            "停止连续深挖历史文章；保留全量原文索引，只做增量更新和按问题回溯。"
        )
    elif len(deep_notes) < 100:
        stage = "accumulating"
        next_action = "继续积累高价值样本，优先方法、案例和用户长期主题。"
    elif len(deep_notes) < MIN_DEEP_CURATED_FOR_STOP:
        stage = "structuring"
        next_action = "继续分层整理，并补齐欠覆盖年份与主题。"
    else:
        stage = "coverage_gap"
        next_action = "数量已足够，停止追求篇数，优先补齐年份、概念与证据缺口。"

    tier_counts = Counter(note.corpus_tier for note in notes)
    report = {
        "generated_at": now_text(),
        "stage": stage,
        "stop_recommended": stop_recommended,
        "next_action": next_action,
        "policy": {
            "mode": (
                "enterprise_evidence_library"
                if REFERENCE_LIBRARY_MODE
                else "full_semantic_curation"
            ),
            "raw_articles_are_user_opinions": False,
            "historical_bulk_read_limit": MIN_DEEP_CURATED_FOR_STOP,
        },
        "corpus": {
            "personal": tier_counts.get("personal", 0),
            "core": tier_counts.get("core", 0),
            "reference": tier_counts.get("reference", 0),
            "brief": tier_counts.get("brief", 0),
            "evidence_archive": len(raw_paths),
            "deep_curated": len(deep_notes),
        },
        "metrics": {
            "window_size": len(window_notes),
            "concept_coverage": round(concept_coverage, 4),
            "year_coverage": round(year_coverage, 4),
            "new_concept_rate": round(new_concept_rate, 4),
            "high_value_rate": round(high_value_rate, 4),
            "redundancy_rate": round(redundancy_rate, 4),
            "relation_density": round(
                sum(len(links.get(note.path, [])) for note in notes)
                / max(len(notes), 1),
                4,
            ),
        },
        "thresholds": {
            "minimum_deep_curated": MIN_DEEP_CURATED_FOR_STOP,
            "maximum_deep_curated_before_stop": MAX_DEEP_CURATED_BEFORE_STOP,
            "minimum_concept_coverage": MIN_CONCEPT_COVERAGE_FOR_STOP,
            "minimum_year_coverage": MIN_YEAR_COVERAGE_FOR_STOP,
            "maximum_new_concept_rate": MAX_NEW_CONCEPT_RATE_FOR_STOP,
            "maximum_new_concept_rate_at_budget_cap": (
                MAX_NEW_CONCEPT_RATE_AT_BUDGET_CAP
            ),
            "maximum_high_value_rate": MAX_HIGH_VALUE_RATE_FOR_STOP,
            "minimum_redundancy_rate": MIN_REDUNDANCY_RATE_FOR_STOP,
        },
        "checks": {
            **stop_checks,
            "coverage_ready": coverage_ready,
            "diminishing_returns": diminishing_returns,
            "budget_cap_reached": budget_cap_reached,
            "representative_sample_ready": representative_sample_ready,
        },
        "year_counts": dict(sorted(curated_year_counts.items())),
    }
    MATURITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MATURITY_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, MATURITY_PATH)

    path = vault / "20_Knowledge" / "AI上下文" / "语料熔炼与停采标准.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    check_label = lambda value: "已达到" if value else "未达到"
    rows = [
        "# 语料熔炼与停采标准",
        "",
        f"> 自动更新于 {report['generated_at']}。目标不是囤积最多文章，而是让 AI 用更少上下文取得更可靠、更像你的回答。",
        "",
        "## 四层语料",
        "",
        f"- **个人语料**：{report['corpus']['personal']} 篇。用户自己的项目、日记、作品与明确反馈，检索权重最高。",
        f"- **核心知识**：{report['corpus']['core']} 篇。经过整理的重点方法、观点和案例，进入可视化图谱。",
        f"- **参考知识**：{report['corpus']['reference']} 篇。用于证据对照和补充解释，进入图谱但权重低于核心层。",
        f"- **速览与原文证据**：{report['corpus']['brief']} 篇速览、{report['corpus']['evidence_archive']} 篇原文。默认不进入图谱，只有回答需要时才回溯。",
        "- **机构边界**：腾讯研究院等外部文章属于来源证据，不等于用户观点；全量正文服务企业 Agent / RAG，个人知识层只吸收筛选后的框架、方法和案例。",
        "",
        "## 当前成熟度",
        "",
        f"- 阶段：**{stage}**",
        f"- 建议：{next_action}",
        f"- 已深度整理：{len(deep_notes)} 篇",
        f"- 概念覆盖：{concept_coverage:.0%}（{check_label(stop_checks['concept_coverage'])}）",
        f"- 年份覆盖：{year_coverage:.0%}（{check_label(stop_checks['year_coverage'])}）",
        f"- 最近 {len(window_notes)} 篇新概念率：{new_concept_rate:.0%}（{check_label(stop_checks['new_concept_rate'])}）",
        f"- 最近 {len(window_notes)} 篇重点率：{high_value_rate:.0%}（{check_label(stop_checks['high_value_rate'])}）",
        f"- 最近 {len(window_notes)} 篇高重复率：{redundancy_rate:.0%}（{check_label(stop_checks['redundancy_rate'])}）",
        "",
        "## 何时停止",
        "",
        "- 普通公众号：每个约 30 篇高价值样本后停止历史扩张，只追新增或按问题补证据。",
        f"- 腾讯研究院：全量原文留在机构证据库；外部代表样本深读达到 {MIN_DEEP_CURATED_FOR_STOP} 篇且概念/年份覆盖达标后，停止连续深挖。",
        f"- Token 预算保险：达到 {MAX_DEEP_CURATED_BEFORE_STOP} 篇后，只要概念、年份覆盖达标且最近窗口新概念率不超过 {MAX_NEW_CONCEPT_RATE_AT_BUDGET_CAP:.0%}，即停止批量深读，改为按问题定向回溯。",
        "- 停止不等于删除：未整理原文继续保留在本地证据索引中，AI在核心层回答不足时才检索它们。",
        "- 如果个人项目、职业方向或长期兴趣发生变化，重新打开相应主题的定向深挖，而不是恢复全量扫描。",
        "",
        "## AI 调用顺序",
        "",
        "1. 先检索个人语料，确定你的目标、偏好和已有判断。",
        "2. 再检索重点方法、观点和案例，组织答案骨架。",
        "3. 用参考层做跨来源校验，并标明来源观点不等于你的观点。",
        "4. 只有信息不足或需要原始证据时，回溯速览与全文原文库。",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path, report


def _raw_note_has_usable_body(path: Path) -> bool:
    try:
        if path.stat().st_size > 4096:
            return True
        text = path.read_text(encoding="utf-8", errors="replace").rstrip()
    except OSError:
        return False
    return not (
        text.endswith("未抓取到正文内容。")
        or text.endswith("未抓取到正文内容.")
        or text.endswith("正文内容暂不可用。")
        or text.endswith("正文内容暂不可用.")
    )


def strip_raw_generated_links(paths: list[Path]) -> int:
    changed = 0
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        if START_MARKER not in text:
            continue
        cleaned = re.sub(
            rf"\n?{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}\n?",
            "\n",
            text,
            flags=re.DOTALL,
        ).rstrip() + "\n"
        if cleaned != text:
            path.write_text(cleaned, encoding="utf-8")
            changed += 1
    return changed


def configure_obsidian_graph(vault: Path) -> Path:
    path = vault / ".obsidian" / "graph.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        config = {}
    config.update(
        {
            "search": OBSIDIAN_GRAPH_FILTER,
            "showTags": False,
            "showAttachments": False,
            "hideUnresolved": True,
            "showOrphans": False,
        }
    )
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def write_sqlite_index(
    notes: list[Note],
    links: dict[Path, list[tuple[Note, float, list[str]]]],
    evidence_notes: list[Note],
    raw_wechat_paths: list[Path],
) -> tuple[Path, int]:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(INDEX_PATH)
    archive_count = 0
    try:
        connection.executescript(
            """
            DROP TABLE IF EXISTS articles;
            DROP TABLE IF EXISTS links;
            DROP TABLE IF EXISTS source_archive;
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                source_url TEXT,
                account TEXT,
                category TEXT,
                quality INTEGER,
                knowledge_type TEXT,
                priority TEXT,
                curation_status TEXT,
                mastery_status TEXT,
                content_status TEXT,
                corpus_namespace TEXT NOT NULL,
                authorship TEXT NOT NULL,
                confidentiality TEXT NOT NULL,
                engagement_status TEXT NOT NULL,
                stance TEXT NOT NULL,
                persona_influence REAL NOT NULL,
                identity_explicit INTEGER NOT NULL,
                corpus_tier TEXT NOT NULL,
                retrieval_weight REAL NOT NULL,
                value_score INTEGER NOT NULL,
                publish_date TEXT,
                curated_at TEXT,
                summary TEXT,
                highlights TEXT,
                concepts TEXT,
                search_terms TEXT,
                body TEXT
            );
            CREATE TABLE links (
                source_path TEXT NOT NULL,
                target_path TEXT NOT NULL,
                score REAL NOT NULL,
                relation_type TEXT NOT NULL,
                evidence_strength TEXT NOT NULL,
                reasons TEXT NOT NULL
            );
            CREATE TABLE source_archive (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                source_url TEXT,
                account TEXT,
                category TEXT,
                quality INTEGER,
                priority TEXT,
                curation_status TEXT,
                content_status TEXT,
                corpus_namespace TEXT NOT NULL,
                authorship TEXT NOT NULL,
                confidentiality TEXT NOT NULL,
                corpus_tier TEXT NOT NULL,
                publish_date TEXT,
                curated_at TEXT,
                search_terms TEXT,
                body TEXT
            );
            """
        )
        archived_urls: set[str] = set()
        for note in notes:
            archive_url = canonical_source_url(note.url)
            if archive_url:
                archived_urls.add(archive_url)
            connection.execute(
                "INSERT OR REPLACE INTO source_archive(path,title,source_url,account,category,quality,priority,curation_status,content_status,corpus_namespace,authorship,confidentiality,corpus_tier,publish_date,curated_at,search_terms,body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(note.path),
                    note.title,
                    note.url,
                    note.account,
                    note.category,
                    note.quality,
                    note.priority,
                    note.curation_status,
                    note.content_status,
                    note.corpus_namespace,
                    note.authorship,
                    note.confidentiality,
                    note.corpus_tier,
                    note.publish_date,
                    note.curated_at,
                    " ".join(note.tokens),
                    note.body,
                ),
            )
            archive_count += 1
            if note.corpus_tier == "evidence":
                continue
            connection.execute(
                "INSERT INTO articles(path,title,source_url,account,category,quality,knowledge_type,priority,curation_status,mastery_status,content_status,corpus_namespace,authorship,confidentiality,engagement_status,stance,persona_influence,identity_explicit,corpus_tier,retrieval_weight,value_score,publish_date,curated_at,summary,highlights,concepts,search_terms,body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(note.path),
                    note.title,
                    note.url,
                    note.account,
                    note.category,
                    note.quality,
                    note.knowledge_type,
                    note.priority,
                    note.curation_status,
                    note.mastery_status,
                    note.content_status,
                    note.corpus_namespace,
                    note.authorship,
                    note.confidentiality,
                    note.engagement_status,
                    note.stance,
                    note.persona_influence,
                    int(note.identity_explicit),
                    note.corpus_tier,
                    note.retrieval_weight,
                    note.value_score or note.quality,
                    note.publish_date,
                    note.curated_at,
                    note.value_summary,
                    json.dumps(note.highlights, ensure_ascii=False),
                    "；".join(note.concepts),
                    " ".join(note.tokens),
                    note.body,
                ),
            )
            for target, score, reasons in links.get(note.path, []):
                relation_kind = reasons[0] if reasons else "语义延伸"
                evidence_strength = (
                    "强"
                    if score >= 0.75 or relation_kind == "显式引用"
                    else "中"
                    if score >= 0.35
                    else "弱"
                )
                connection.execute(
                    "INSERT INTO links(source_path,target_path,score,relation_type,evidence_strength,reasons) VALUES(?,?,?,?,?,?)",
                    (
                        str(note.path),
                        str(target.path),
                        score,
                        relation_kind,
                        evidence_strength,
                        "；".join(reasons),
                    ),
                )
        for note in evidence_notes:
            archive_url = canonical_source_url(note.url)
            if archive_url and archive_url in archived_urls:
                continue
            if archive_url:
                archived_urls.add(archive_url)
            connection.execute(
                "INSERT OR REPLACE INTO source_archive(path,title,source_url,account,category,quality,priority,curation_status,content_status,corpus_namespace,authorship,confidentiality,corpus_tier,publish_date,curated_at,search_terms,body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(note.path),
                    note.title,
                    note.url,
                    note.account,
                    note.category,
                    note.quality,
                    note.priority,
                    note.curation_status,
                    note.content_status,
                    note.corpus_namespace,
                    note.authorship,
                    note.confidentiality,
                    "evidence",
                    note.publish_date,
                    note.curated_at,
                    " ".join(note.tokens),
                    note.body,
                ),
            )
            archive_count += 1
        # Stream raw subscription notes into the evidence archive one file at
        # a time. They remain searchable without entering the core graph or
        # being held in memory as a second full corpus.
        for path in raw_wechat_paths:
            text = path.read_text(encoding="utf-8", errors="replace")
            source_url = str(_field(text, "source_url") or _field(text, "sourceUrl"))
            canonical = canonical_source_url(source_url)
            if canonical and canonical in archived_urls:
                continue
            if canonical:
                archived_urls.add(canonical)
            body = _without_generated_links(text)
            title = str(_field(text, "title", path.stem))
            has_usable_body = _raw_note_has_usable_body(path)
            indexed_body = body if has_usable_body else ""
            search_terms = " ".join(document_tokens(title, indexed_body))
            connection.execute(
                "INSERT OR REPLACE INTO source_archive(path,title,source_url,account,category,quality,priority,curation_status,content_status,corpus_namespace,authorship,confidentiality,corpus_tier,publish_date,curated_at,search_terms,body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(path),
                    title,
                    source_url,
                    str(_field(text, "account") or _field(text, "author") or "微信公众号"),
                    "未整理",
                    int(_field(text, "quality_score", 45) or 45),
                    "原文库",
                    str(_field(text, "curation_status", "pending")),
                    "complete" if has_usable_body else "metadata_only",
                    knowledge_schema.SOURCE_ARCHIVE,
                    "external",
                    "public_external",
                    "evidence",
                    str(
                        _field(text, "publish_date")
                        or _field(text, "published_at")
                    ),
                    str(_field(text, "curated_at")),
                    search_terms,
                    indexed_body,
                ),
            )
            archive_count += 1
        try:
            connection.execute("DROP TABLE IF EXISTS articles_fts")
            connection.execute(
                "CREATE VIRTUAL TABLE articles_fts USING fts5(title, account, category, summary, highlights, concepts, search_terms, path UNINDEXED)"
            )
            connection.execute(
                "INSERT INTO articles_fts(title,account,category,summary,highlights,concepts,search_terms,path) SELECT title,account,category,summary,highlights,concepts,search_terms,path FROM articles"
            )
            connection.execute("DROP TABLE IF EXISTS source_archive_fts")
            connection.execute(
                "CREATE VIRTUAL TABLE source_archive_fts USING fts5(title, account, category, search_terms, path UNINDEXED)"
            )
            connection.execute(
                "INSERT INTO source_archive_fts(title,account,category,search_terms,path) SELECT title,account,category,search_terms,path FROM source_archive"
            )
        except sqlite3.OperationalError:
            pass
        connection.commit()
        # Rebuilds replace large FTS tables. VACUUM returns the released pages
        # to disk instead of leaving a permanently inflated local index file.
        connection.execute("VACUUM")
    finally:
        connection.close()
    return INDEX_PATH, archive_count


def canonical_source_url(value: str) -> str:
    value = str(value or "").strip()
    match = re.search(r"https?://mp\.weixin\.qq\.com/s/([^?#\s]+)", value)
    if match:
        return f"https://mp.weixin.qq.com/s/{match.group(1)}"
    parsed = urlsplit(value)
    if (
        parsed.netloc.lower() == "mp.weixin.qq.com"
        and parsed.path.rstrip("/") == "/mp/appmsg/show"
    ):
        # Articles published before the modern /s/<slug> URL format use the
        # same path and identify the article only through query parameters.
        # Stripping the query collapses hundreds of distinct historical
        # articles into one archive row.
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        values = dict(pairs)
        identity_keys = (
            "__biz",
            "appmsgid",
            "mid",
            "itemidx",
            "idx",
            "sn",
            "sign",
        )
        identity = [(key, values[key]) for key in identity_keys if values.get(key)]
        if not identity:
            identity = sorted(pairs)
        return urlunsplit(
            ("https", "mp.weixin.qq.com", "/mp/appmsg/show", urlencode(identity), "")
        )
    return value.split("#", 1)[0].split("?", 1)[0].rstrip("/")


def raw_wechat_notes(vault: Path) -> list[Path]:
    root = vault / "10_Sources" / "WeChat"
    if not root.is_dir():
        return []
    # The selection pipeline already maintains an explicit manifest. Reusing
    # it avoids opening more than eight thousand Markdown files merely to read
    # two frontmatter fields on every maturity/index refresh.
    selection_report = DATA_DIR / "wechat-text-selection" / "selection-report.json"
    if vault.resolve() == DEFAULT_VAULT.resolve() and selection_report.is_file():
        try:
            payload = json.loads(selection_report.read_text(encoding="utf-8"))
            manifested: set[Path] = set()
            root_resolved = root.resolve()
            for account in (payload.get("accounts") or {}).values():
                for item in account.get("items") or []:
                    path = Path(str(item.get("path") or ""))
                    if (
                        path.is_file()
                        and path.parent.resolve() == root_resolved
                    ):
                        manifested.add(path)
            if manifested:
                return sorted(manifested)
        except (json.JSONDecodeError, OSError, TypeError, AttributeError):
            pass
    selected: list[Path] = []
    for path in root.glob("*.md"):
        if path.name.startswith("_"):
            continue
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = "".join(handle.readline() for _ in range(120))
        scope = str(_field(head, "knowledge_scope"))
        account = str(_field(head, "account"))
        if scope == "selected" or account == "腾讯研究院":
            selected.append(path)
    return sorted(selected)


def _namespace_filter(
    namespaces: tuple[str, ...] | None,
    table_alias: str = "",
) -> tuple[str, tuple[str, ...]]:
    if not namespaces:
        return "", ()
    placeholders = ",".join("?" for _ in namespaces)
    prefix = f"{table_alias}." if table_alias else ""
    return f" AND {prefix}corpus_namespace IN ({placeholders})", namespaces


def _attach_citation(item: dict[str, Any]) -> None:
    date = str(item.get("publish_date") or item.get("curated_at") or "")
    date_kind = (
        "published"
        if item.get("publish_date")
        else "curated"
        if item.get("curated_at")
        else "unknown"
    )
    item["temporal"] = {"date": date, "date_kind": date_kind}
    item["citation"] = {
        "title": str(item.get("title") or ""),
        "source_url": str(item.get("source_url") or ""),
        "date": date,
        "date_kind": date_kind,
        "local_note": str(item.get("path") or ""),
    }


def _date_select(
    connection: sqlite3.Connection,
    table: str,
    alias: str = "",
) -> str:
    columns = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if {"publish_date", "curated_at"} <= columns:
        prefix = f"{alias}." if alias else ""
        return f"{prefix}publish_date,{prefix}curated_at"
    return "'' AS publish_date,'' AS curated_at"


def _identity_explicit_select(
    connection: sqlite3.Connection,
    alias: str = "",
) -> str:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(articles)").fetchall()
    }
    if "identity_explicit" in columns:
        prefix = f"{alias}." if alias else ""
        return f"{prefix}identity_explicit"
    return "0 AS identity_explicit"


def _search_core(
    connection: sqlite3.Connection,
    terms: list[str],
    limit: int,
    namespaces: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    fts_query = " OR ".join(
        '"' + term.replace('"', '""') + '"' for term in terms
    )
    namespace_clause, namespace_params = _namespace_filter(namespaces, "a")
    joined_dates = _date_select(connection, "articles", "a")
    plain_dates = _date_select(connection, "articles")
    joined_identity_explicit = _identity_explicit_select(connection, "a")
    plain_identity_explicit = _identity_explicit_select(connection)
    rows: list[sqlite3.Row] = []
    try:
        rows = connection.execute(
            f"""
            SELECT a.title,a.path,a.source_url,a.account,a.category,a.quality,a.concepts,
                   a.priority,a.corpus_namespace,a.authorship,a.confidentiality,
                   a.engagement_status,a.stance,a.persona_influence,
                   {joined_identity_explicit},a.corpus_tier,a.retrieval_weight,a.value_score,
                   {joined_dates},a.summary,a.highlights,
                   substr(a.body,1,240) AS snippet,
                   a.body AS _rank_body,
                   bm25(articles_fts, 8.0, 4.0, 3.0, 6.0, 4.0, 2.0, 1.0)
                       AS _lexical_rank
            FROM articles_fts
            JOIN articles a ON a.path = articles_fts.path
            WHERE articles_fts MATCH ? {namespace_clause}
            ORDER BY _lexical_rank
                       - (0.35 * a.retrieval_weight),
                     a.value_score DESC,
                     a.quality DESC
            LIMIT ?
            """,
            (fts_query, *namespace_params, max(limit * 8, 80)),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        patterns = [f"%{term}%" for term in terms]
        match_clause = " OR ".join(
            "(title LIKE ? OR account LIKE ? OR category LIKE ? OR concepts LIKE ? OR body LIKE ?)"
            for _ in terms
        )
        score_clause = " + ".join(
            "(CASE WHEN title LIKE ? OR account LIKE ? OR category LIKE ? OR concepts LIKE ? OR body LIKE ? "
            "THEN 1 ELSE 0 END)"
            for _ in terms
        )
        score_params = [pattern for pattern in patterns for _ in range(5)]
        match_params = [pattern for pattern in patterns for _ in range(5)]
        # The fallback reads ``articles`` without an alias. Rebuild the
        # namespace predicate rather than reusing the FTS join's ``a.`` alias.
        namespace_clause, namespace_params = _namespace_filter(namespaces)
        rows = connection.execute(
            f"""
            SELECT title,path,source_url,account,category,quality,concepts,priority,
                   corpus_namespace,authorship,confidentiality,engagement_status,
                   stance,persona_influence,{plain_identity_explicit},
                   corpus_tier,retrieval_weight,value_score,
                   {plain_dates},summary,highlights,
                   substr(body,1,240) AS snippet,
                   ({score_clause}) AS matched_terms
            FROM articles
            WHERE ({match_clause}) {namespace_clause}
            ORDER BY (matched_terms + 2.0 * retrieval_weight) DESC,
                     value_score DESC,
                     quality DESC
            LIMIT ?
            """,
            (*score_params, *match_params, *namespace_params, limit),
        ).fetchall()
    results = [dict(row) for row in rows]
    if results and "_rank_body" in results[0]:
        for item in results:
            title = str(item.get("title") or "").lower()
            summary = str(item.get("summary") or "").lower()
            highlights = str(item.get("highlights") or "").lower()
            body = str(item.pop("_rank_body", "") or "").lower()
            concepts = str(item.get("concepts") or "").lower()
            matched_count = 0
            coverage_score = 0
            for term in terms:
                term_weight = min(max(len(term), 1), 6)
                field_weight = 0
                if term in title:
                    field_weight = max(field_weight, 5)
                if term in concepts:
                    field_weight = max(field_weight, 4)
                if term in summary:
                    field_weight = max(field_weight, 3)
                if term in highlights:
                    field_weight = max(field_weight, 2)
                if term in body:
                    field_weight = max(field_weight, 1)
                if field_weight:
                    matched_count += 1
                    coverage_score += term_weight * field_weight
            item["matched_term_count"] = matched_count
            item["_coverage_score"] = coverage_score
        results.sort(
            key=lambda item: (
                -int(item.get("matched_term_count") or 0),
                -int(item.get("_coverage_score") or 0),
                float(item.get("_lexical_rank") or 0),
                -float(item.get("retrieval_weight") or 0),
                -float(item.get("value_score") or 0),
            )
        )
        results = results[:limit]
        for item in results:
            item.pop("_coverage_score", None)
            item.pop("_lexical_rank", None)
    for item in results:
        item["tier"] = item.get("corpus_tier") or "reference"
        item["identity"] = {
            "namespace": item.get("corpus_namespace"),
            "authorship": item.get("authorship"),
            "confidentiality": item.get("confidentiality"),
            "engagement_status": item.get("engagement_status"),
            "stance": item.get("stance"),
            "represents_user": item.get("corpus_namespace")
            == knowledge_schema.PERSONAL_MEMORY
            and bool(item.get("identity_explicit")),
        }
        item["retrieval_reason"] = {
            knowledge_schema.PERSONAL_MEMORY: "个人第二大脑",
            knowledge_schema.PROFESSIONAL_REFERENCE: "专业研究资料",
            knowledge_schema.ENTERPRISE_INTERNAL: "企业内部资料",
            knowledge_schema.AUTHORITATIVE_EXTERNAL: "权威外部资料",
        }.get(str(item.get("corpus_namespace")), "知识索引")
        _attach_citation(item)
    return results


def _search_archive(
    connection: sqlite3.Connection,
    terms: list[str],
    limit: int,
    namespaces: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    fts_query = " OR ".join(
        '"' + term.replace('"', '""') + '"' for term in terms
    )
    namespace_clause, namespace_params = _namespace_filter(namespaces, "a")
    joined_dates = _date_select(connection, "source_archive", "a")
    plain_dates = _date_select(connection, "source_archive")
    rows: list[sqlite3.Row] = []
    try:
        rows = connection.execute(
            f"""
            SELECT a.title,a.path,a.source_url,a.account,a.category,a.quality,
                   a.priority,a.content_status,a.corpus_namespace,a.authorship,
                   a.confidentiality,a.corpus_tier,{joined_dates},
                   substr(a.body,1,240) AS snippet
            FROM source_archive_fts
            JOIN source_archive a ON a.path = source_archive_fts.path
            WHERE source_archive_fts MATCH ? {namespace_clause}
            ORDER BY bm25(source_archive_fts, 8.0, 4.0, 3.0, 1.0),
                     a.quality DESC
            LIMIT ?
            """,
            (fts_query, *namespace_params, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        patterns = [f"%{term}%" for term in terms]
        match_clause = " OR ".join(
            "(title LIKE ? OR account LIKE ? OR category LIKE ? OR body LIKE ?)"
            for _ in terms
        )
        score_clause = " + ".join(
            "(CASE WHEN title LIKE ? OR account LIKE ? OR category LIKE ? OR body LIKE ? "
            "THEN 1 ELSE 0 END)"
            for _ in terms
        )
        score_params = [pattern for pattern in patterns for _ in range(4)]
        match_params = [pattern for pattern in patterns for _ in range(4)]
        # Same alias rule as the core-search fallback above.
        namespace_clause, namespace_params = _namespace_filter(namespaces)
        rows = connection.execute(
            f"""
            SELECT title,path,source_url,account,category,quality,priority,
                   content_status,corpus_namespace,authorship,confidentiality,corpus_tier,
                   {plain_dates},
                   substr(body,1,240) AS snippet,
                   ({score_clause}) AS matched_terms
            FROM source_archive
            WHERE ({match_clause}) {namespace_clause}
            ORDER BY matched_terms DESC,
                     CASE priority
                       WHEN '重点' THEN 4
                       WHEN '参考' THEN 3
                       WHEN '速览' THEN 2
                       WHEN '原文库' THEN 1
                       ELSE 0
                     END DESC,
                     title
            LIMIT ?
            """,
            (*score_params, *match_params, *namespace_params, limit),
        ).fetchall()
    results = [dict(row) for row in rows]
    for item in results:
        item["tier"] = "evidence"
        item["identity"] = {
            "namespace": item.get("corpus_namespace"),
            "authorship": item.get("authorship"),
            "confidentiality": item.get("confidentiality"),
            "represents_user": False,
        }
        item["retrieval_reason"] = "原文证据回溯"
        _attach_citation(item)
    return results


def search(
    query: str,
    limit: int = 10,
    scope: str = "all",
) -> list[dict[str, Any]]:
    query = query.strip()
    if not query or not INDEX_PATH.is_file():
        return []
    scope = scope.strip().lower()
    namespaces = knowledge_schema.namespace_scope(scope)
    raw_terms = re.findall(
        r"[A-Za-z0-9+._-]+|[\u4e00-\u9fff]{2,}",
        query.lower(),
    )
    expanded_terms: list[str] = []
    for term in raw_terms:
        expanded_terms.append(term)
        if re.fullmatch(r"[\u4e00-\u9fff]{5,}", term):
            expanded_terms.extend(
                term[index : index + 2]
                for index in range(len(term) - 1)
            )
    terms = list(dict.fromkeys(expanded_terms))[:12]
    if not terms:
        terms = [query]
    connection = sqlite3.connect(INDEX_PATH)
    connection.row_factory = sqlite3.Row
    try:
        results: list[dict[str, Any]] = []
        if scope in {"all", "knowledge", "personal", "professional", "enterprise", "authoritative"}:
            results.extend(_search_core(connection, terms, limit, namespaces))
        if scope == "knowledge":
            return results[:limit]
        if scope in {"personal", "professional", "enterprise", "authoritative"}:
            return results[:limit]
        if scope == "all" and len(results) >= limit:
            return results[:limit]
        archive_limit = limit if scope == "archive" else max(limit * 3, 30)
        archive_namespaces = (
            (knowledge_schema.SOURCE_ARCHIVE,) if scope == "archive" else None
        )
        archive_results = _search_archive(
            connection,
            terms,
            archive_limit,
            archive_namespaces,
        )
        if scope == "archive":
            return archive_results[:limit]
        seen_paths = {str(item.get("path") or "") for item in results}
        seen_urls = {
            canonical_source_url(str(item.get("source_url") or ""))
            for item in results
            if item.get("source_url")
        }
        for item in archive_results:
            path = str(item.get("path") or "")
            source_url = canonical_source_url(str(item.get("source_url") or ""))
            if path in seen_paths or (source_url and source_url in seen_urls):
                continue
            results.append(item)
            seen_paths.add(path)
            if source_url:
                seen_urls.add(source_url)
            if len(results) >= limit:
                break
        return results
    finally:
        connection.close()


def _recall_intent(query: str) -> str:
    value = query.casefold()
    enterprise_cues = (
        "nex",
        "企业",
        "公司",
        "内部",
        "项目事实",
        "制度",
        "流程",
    )
    research_cues = (
        "研究显示",
        "行业",
        "论文",
        "报告",
        "外部证据",
        "腾讯研究院",
        "资料怎么说",
    )
    personal_cues = (
        "我过去",
        "我以前",
        "我之前",
        "我当时",
        "我的想法",
        "我的判断",
        "我怎么想",
        "我为什么",
        "什么时候想到",
        "曾经",
    )
    if any(cue in value for cue in personal_cues):
        return "personal_recall"
    if any(cue in value for cue in enterprise_cues):
        return "enterprise_lookup"
    if any(cue in value for cue in research_cues):
        return "research_lookup"
    return "personal_recall"


def _recall_terms(query: str) -> str:
    value = query
    for cue in (
        "我过去",
        "我以前",
        "我之前",
        "我当时",
        "我的想法",
        "我的判断",
        "我怎么想",
        "我为什么",
        "什么时候想到",
        "曾经",
        "资料怎么说",
    ):
        value = value.replace(cue, " ")
    return re.sub(r"\s+", " ", value).strip() or query


def recall(
    query: str,
    limit: int = 8,
    include_evidence: bool = False,
) -> dict[str, Any]:
    """Route a memory question without letting external text impersonate the user."""

    query = query.strip()
    bounded_limit = max(1, min(int(limit or 8), 20))
    intent = _recall_intent(query)
    retrieval_query = _recall_terms(query)
    memories: list[dict[str, Any]] = []
    enterprise_facts: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    route: list[str] = []

    if intent == "personal_recall":
        route.append(knowledge_schema.PERSONAL_MEMORY)
        memories = [
            item
            for item in search(
                retrieval_query,
                limit=max(bounded_limit * 2, 10),
                scope="personal",
            )
            if item.get("identity", {}).get("represents_user")
        ][:bounded_limit]
        if include_evidence:
            route.extend(
                (
                    knowledge_schema.PROFESSIONAL_REFERENCE,
                    knowledge_schema.AUTHORITATIVE_EXTERNAL,
                )
            )
            evidence = search(
                retrieval_query,
                limit=max(1, bounded_limit // 2),
                scope="professional",
            )
    elif intent == "enterprise_lookup":
        route.append(knowledge_schema.ENTERPRISE_INTERNAL)
        enterprise_facts = search(
            retrieval_query,
            limit=bounded_limit,
            scope="enterprise",
        )
        if include_evidence:
            route.extend(
                (
                    knowledge_schema.PROFESSIONAL_REFERENCE,
                    knowledge_schema.AUTHORITATIVE_EXTERNAL,
                )
            )
            evidence = search(
                retrieval_query,
                limit=max(1, bounded_limit // 2),
                scope="professional",
            )
    else:
        route.extend(
            (
                knowledge_schema.PROFESSIONAL_REFERENCE,
                knowledge_schema.AUTHORITATIVE_EXTERNAL,
            )
        )
        evidence = search(
            retrieval_query,
            limit=bounded_limit,
            scope="professional",
        )

    found_personal = bool(memories)
    boundary = (
        "Personal memories are user-authored records. External evidence is returned "
        "in a separate field and never represents the user's view."
    )
    if intent == "personal_recall" and not found_personal:
        boundary = (
            "No matching user-authored memory was found. The system will not replace "
            "a missing personal memory with an external article."
        )
    return {
        "query": query,
        "retrieval_query": retrieval_query,
        "intent": intent,
        "route": route,
        "memories": memories,
        "enterprise_facts": enterprise_facts,
        "evidence": evidence,
        "boundary": boundary,
        "context_budget": {
            "max_primary_results": bounded_limit,
            "max_evidence_results": (
                max(1, bounded_limit // 2) if include_evidence else 0
            ),
            "full_articles_loaded": 0,
            "archive_fallback": False,
        },
    }


def build(vault: Path = DEFAULT_VAULT, preference_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    article_roots = [
        vault / "10_Sources" / "WeChat" / "Articles",
        vault / "10_Sources" / "Local" / "Articles",
        vault / "10_Sources" / "Enterprise" / "Articles",
        vault / "10_Sources" / "Xiaohongshu",
        vault / "10_Sources" / "Feishu",
    ]
    all_processed_notes = read_notes(article_roots)
    notes = [
        note
        for note in all_processed_notes
        if note.corpus_tier in GRAPH_TIERS | {"brief"}
    ]
    evidence_notes = [
        note for note in all_processed_notes if note.corpus_tier == "evidence"
    ]
    links = related_notes(notes)
    link_count = write_note_links(notes, links, vault)
    topic_count = write_topic_maps(notes, vault)
    concept_count = write_concept_maps(notes, vault)
    digest = write_news_digest(notes, vault)
    context_json, profile = personal_context.write_context(
        notes,
        vault,
        preference_summary or {},
    )
    raw_paths = raw_wechat_notes(vault)
    stripped_raw_link_count = strip_raw_generated_links(raw_paths)
    graph_config = configure_obsidian_graph(vault)
    maturity_path, maturity = write_maturity_report(notes, raw_paths, links, vault)
    index, archive_count = write_sqlite_index(
        notes,
        links,
        evidence_notes,
        raw_paths,
    )
    overview = write_overview(notes, links, vault, archive_count)
    return {
        "generated_at": now_text(),
        "article_count": len(notes),
        "core_article_count": sum(
            note.corpus_tier in GRAPH_TIERS for note in notes
        ),
        "brief_article_count": sum(note.corpus_tier == "brief" for note in notes),
        "link_count": link_count,
        "topic_count": topic_count,
        "concept_count": concept_count,
        "overview_path": str(overview),
        "digest_path": str(digest),
        "profile_path": str(profile),
        "context_json_path": str(context_json),
        "index_path": str(index),
        "archive_count": archive_count,
        "maturity_path": str(maturity_path),
        "maturity_stage": maturity["stage"],
        "stop_recommended": maturity["stop_recommended"],
        "obsidian_graph_config": str(graph_config),
        "stripped_raw_link_count": stripped_raw_link_count,
    }
