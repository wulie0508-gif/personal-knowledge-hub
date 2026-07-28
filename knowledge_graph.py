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
            corpus_namespace = knowledge_schema.infer_namespace(
                platform=platform,
                path_text=str(path),
                explicit=_field(text, "corpus_namespace"),
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
    …10081 tokens truncated…                continue
            if canonical:
                archived_urls.add(canonical)
            body = _without_generated_links(text)
            title = str(_field(text, "title", path.stem))
            has_usable_body = _raw_note_has_usable_body(path)
            indexed_body = body if has_usable_body else ""
            search_terms = " ".join(document_tokens(title, indexed_body))
            connection.execute(
                "INSERT OR REPLACE INTO source_archive(path,title,source_url,account,category,quality,priority,curation_status,content_status,corpus_namespace,authorship,confidentiality,corpus_tier,search_terms,body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
    rows: list[sqlite3.Row] = []
    try:
        rows = connection.execute(
            f"""
            SELECT a.title,a.path,a.source_url,a.account,a.category,a.quality,a.concepts,
                   a.priority,a.corpus_namespace,a.authorship,a.confidentiality,
                   a.engagement_status,a.stance,a.persona_influence,
                   a.corpus_tier,a.retrieval_weight,a.value_score,
                   a.summary,a.highlights,
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
                   stance,persona_influence,corpus_tier,retrieval_weight,value_score,summary,highlights,
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
            == knowledge_schema.PERSONAL_MEMORY,
        }
        item["retrieval_reason"] = {
            knowledge_schema.PERSONAL_MEMORY: "个人第二大脑",
            knowledge_schema.PROFESSIONAL_REFERENCE: "专业研究资料",
            knowledge_schema.ENTERPRISE_INTERNAL: "企业内部资料",
            knowledge_schema.AUTHORITATIVE_EXTERNAL: "权威外部资料",
        }.get(str(item.get("corpus_namespace")), "知识索引")
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
    rows: list[sqlite3.Row] = []
    try:
        rows = connection.execute(
            f"""
            SELECT a.title,a.path,a.source_url,a.account,a.category,a.quality,
                   a.priority,a.content_status,a.corpus_namespace,a.authorship,
                   a.confidentiality,a.corpus_tier,
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
    profile = write_ai_profile(notes, vault, preference_summary or {})
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
        "index_path": str(index),
        "archive_count": archive_count,
        "maturity_path": str(maturity_path),
        "maturity_stage": maturity["stage"],
        "stop_recommended": maturity["stop_recommended"],
        "obsidian_graph_config": str(graph_config),
        "stripped_raw_link_count": stripped_raw_link_count,
    }
