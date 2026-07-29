#!/usr/bin/env python3
"""Build a lean, validated Obsidian knowledge base from WeChat history."""

from __future__ import annotations

import argparse
import hashlib
import html as html_std
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from lxml import etree, html
from PIL import Image, UnidentifiedImageError

import quality_feedback
import ocr_provider
import codex_ocr_queue
import knowledge_schema
import knowledge_value
import wechat_history_watcher as history_source
import runtime_config


ROOT = Path(__file__).resolve().parent
DATA_DIR = runtime_config.runtime_home()
VAULT = runtime_config.configured_vault()
WECHAT_ROOT = VAULT / "10_Sources" / "WeChat"
ARTICLE_ROOT = WECHAT_ROOT / "Articles"
ASSET_ROOT = WECHAT_ROOT / "_assets"
SYSTEM_ROOT = WECHAT_ROOT / "_系统"
INDEX_PATH = WECHAT_ROOT / "微信知识库索引.md"
REPORT_PATH = SYSTEM_ROOT / "知识库验收报告.md"
REPORT_JSON = DATA_DIR / "reports" / "knowledge-validation.json"
OCR_SCRIPT_DIR = (
    Path.home()
    / ".codex"
    / "skills"
    / "wechat-content-router-windows"
    / "scripts"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137 Safari/537.36"
)
SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://mp.weixin.qq.com/",
    }
)
OCR_ENGINE: Any = None
OCR_LOAD_ERROR = ""
# CPU 实时路径只处理一张最有代表性的图；其余图片保留 pending 状态，
# 由后续后台补扫，避免单篇文章阻塞整个历史库。
MAX_OCR_IMAGES = 1


@dataclass
class ImageResult:
    source_url: str
    local_path: str = ""
    width: int = 0
    height: int = 0
    original_bytes: int = 0
    compressed_bytes: int = 0
    ocr_text: str = ""
    ocr_status: str = "not_run"
    kept: bool = False
    error: str = ""


@dataclass
class ArticleResult:
    source_url: str
    title: str
    account: str = ""
    category: str = "其他"
    quality_score: int = 0
    quality_tier: str = "低"
    quality_flags: list[str] = field(default_factory=list)
    knowledge_type: str = "参考资料"
    knowledge_value_score: int = 0
    knowledge_priority: str = "参考"
    value_reasons: list[str] = field(default_factory=list)
    value_summary: str = ""
    key_insights: list[str] = field(default_factory=list)
    mastery_status: str = "未学习"
    preference_adjustment: int = 0
    preference_reasons: list[str] = field(default_factory=list)
    auto_remove_recommended: bool = False
    auto_remove_confidence: float = 0.0
    content_status: str = "metadata_only"
    recovery_source: str = ""
    text_length: int = 0
    ocr_length: int = 0
    ocr_status: str = "not_run"
    image_count: int = 0
    kept_image_count: int = 0
    original_image_bytes: int = 0
    compressed_image_bytes: int = 0
    note_path: str = ""
    validation: str = "pending"
    errors: list[str] = field(default_factory=list)


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_name(value: str, limit: int = 80) -> str:
    value = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", value or "")
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or "微信公众号文章")[:limit]


def article_id(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    clean = re.sub(r"[^A-Za-z0-9_-]", "", slug)
    return (clean[:12] or hashlib.sha1(url.encode()).hexdigest()[:12])


def fetch_html(url: str, attempts: int = 2) -> tuple[str, str]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = SESSION.get(url, timeout=35)
            response.raise_for_status()
            return response.content.decode("utf-8", errors="replace"), response.url
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5)
    raise RuntimeError(str(last_error or "无法读取文章"))


def first_xpath(doc: html.HtmlElement, expressions: list[str]) -> str:
    for expression in expressions:
        value = doc.xpath(expression)
        if isinstance(value, list):
            value = value[0] if value else ""
        text = str(value or "").strip()
        if text:
            return re.sub(r"\s+", " ", html_std.unescape(text)).strip()
    return ""


def extract_metadata(doc: html.HtmlElement, title_hint: str) -> tuple[str, str, str]:
    title = first_xpath(
        doc,
        [
            'string(//meta[@property="og:title"]/@content)',
            'string(//meta[@name="twitter:title"]/@content)',
            "string(//title)",
        ],
    )
    title = re.sub(r"\s*[_-]\s*微信公众平台.*$", "", title).strip()
    account = first_xpath(
        doc,
        [
            'string(//a[@id="js_name"])',
            'string(//meta[@property="og:site_name"]/@content)',
        ],
    )
    published = first_xpath(
        doc,
        [
            'string(//meta[@property="article:published_time"]/@content)',
            'string(//em[@id="publish_time"])',
            'string(//span[@id="publish_time"])',
        ],
    )
    return title or title_hint or "微信公众号文章", account, published


def content_root(doc: html.HtmlElement) -> html.HtmlElement | None:
    matches = doc.xpath('//*[@id="js_content"]')
    return matches[0] if matches else None


def extract_plain_text(root: html.HtmlElement | None) -> tuple[str, int]:
    if root is None:
        return "", 0
    for node in root.xpath(".//script|.//style|.//noscript"):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    blocks: list[str] = []
    seen: set[str] = set()
    for node in root.xpath(".//h1|.//h2|.//h3|.//h4|.//p|.//li|.//blockquote"):
        text = re.sub(r"\s+", " ", node.text_content() or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        prefix = ""
        tag = str(node.tag).lower()
        if tag in {"h1", "h2", "h3", "h4"}:
            prefix = "#" * min(int(tag[1]), 4) + " "
        elif tag == "li":
            prefix = "- "
        elif tag == "blockquote":
            prefix = "> "
        blocks.append(prefix + text)
    text = "\n\n".join(blocks)
    return text, len(re.sub(r"\s+", "", text))


def decode_embedded_text(value: str) -> str:
    """Decode long-form text embedded in WeChat verification-page metadata."""
    value = re.sub(
        r"\\x([0-9a-fA-F]{2})",
        lambda match: chr(int(match.group(1), 16)),
        value or "",
    )
    value = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        value,
    )
    value = value.replace("\\n", "\n").replace("\\r", "\n").replace("\\t", " ")
    value = value.replace("\\'", "'").replace('\\"', '"').replace("\\/", "/")
    value = html_std.unescape(html_std.unescape(value))
    if "<" in value and ">" in value:
        try:
            value = html.fromstring(f"<div>{value}</div>").text_content()
        except (etree.ParserError, ValueError):
            value = re.sub(r"<[^>]+>", " ", value)
    paragraphs: list[str] = []
    for paragraph in re.split(r"\n\s*\n|\r\n\s*\r\n", value):
        clean = re.sub(r"[ \t]+", " ", paragraph).strip()
        clean = re.sub(r"\n{2,}", "\n", clean)
        if clean:
            paragraphs.append(clean)
    return "\n\n".join(paragraphs).strip()


def extract_embedded_text(
    doc: html.HtmlElement,
    html_text: str,
) -> tuple[str, int, str]:
    """Recover full text hidden in metadata when #js_content is gated."""
    candidates: list[tuple[str, str]] = []
    for expression, source in (
        ('string(//meta[@name="description"]/@content)', "meta_description"),
        ('string(//meta[@property="og:description"]/@content)', "og_description"),
        ('string(//meta[@name="twitter:description"]/@content)', "twitter_description"),
    ):
        raw = doc.xpath(expression)
        if raw:
            candidates.append((decode_embedded_text(str(raw)), source))
    for pattern, source in (
        (
            r"content_noencode\s*:\s*'((?:\\.|[^'])*)'",
            "content_noencode",
        ),
        (
            r'\bmsg_desc\s*=\s*"((?:\\.|[^"])*)"',
            "msg_desc",
        ),
    ):
        match = re.search(pattern, html_text, re.DOTALL)
        if match:
            candidates.append((decode_embedded_text(match.group(1)), source))
    blocked_phrases = {
        "环境异常",
        "访问过于频繁",
        "完成验证",
        "当前环境存在异常",
    }
    valid = [
        (text, source)
        for text, source in candidates
        if len(re.sub(r"\s+", "", text)) >= 180
        and not any(phrase in text[:160] for phrase in blocked_phrases)
    ]
    if not valid:
        return "", 0, ""
    text, source = max(valid, key=lambda item: len(item[0]))
    return text, len(re.sub(r"\s+", "", text)), source


def image_urls(root: html.HtmlElement | None) -> list[str]:
    if root is None:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for node in root.xpath(".//img"):
        value = (
            node.get("data-src")
            or node.get("data-original")
            or node.get("src")
            or ""
        )
        value = html_std.unescape(value).replace("&amp;", "&")
        if not value.startswith("http"):
            continue
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def article_image_urls(
    doc: html.HtmlElement,
    root: html.HtmlElement | None,
) -> list[str]:
    urls = image_urls(root)
    if urls:
        return urls
    for expression in (
        'string(//meta[@property="og:image"]/@content)',
        'string(//meta[@name="twitter:image"]/@content)',
    ):
        value = html_std.unescape(str(doc.xpath(expression) or "")).replace(
            "&amp;", "&"
        )
        if value.startswith("http") and value not in urls:
            urls.append(value)
    return urls


def get_ocr_engine() -> Any:
    global OCR_ENGINE, OCR_LOAD_ERROR
    if OCR_ENGINE is not None:
        return OCR_ENGINE
    if OCR_LOAD_ERROR:
        raise RuntimeError(OCR_LOAD_ERROR)
    try:
        sys.path.insert(0, str(OCR_SCRIPT_DIR))
        import ocr_paddle

        OCR_ENGINE = ocr_paddle.load_ocr()
        return OCR_ENGINE
    except Exception as exc:
        OCR_LOAD_ERROR = str(exc)
        raise RuntimeError(OCR_LOAD_ERROR) from exc


def extract_ocr_text(prediction: Any) -> str:
    sys.path.insert(0, str(OCR_SCRIPT_DIR))
    import ocr_paddle

    lines: list[str] = []
    for item in prediction:
        parsed = ocr_paddle.normalize_result_item(item)
        value = str(parsed.get("text") or "").strip()
        if value:
            lines.append(value)
    return "\n".join(lines).strip()


def download_image(url: str, target: Path) -> int:
    response = SESSION.get(url, timeout=35)
    response.raise_for_status()
    target.write_bytes(response.content)
    return len(response.content)


def compress_image(source: Path, target: Path) -> tuple[int, int, int]:
    with Image.open(source) as image:
        image.load()
        width, height = image.size
        if max(width, height) > 1280:
            image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGB")
        if image.mode == "RGBA":
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            image = background
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, "WEBP", quality=58, method=6)
        return width, height, target.stat().st_size


def prepare_ocr_image(source: Path, target: Path) -> Path:
    with Image.open(source) as image:
        image.load()
        if max(image.size) > 1600:
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.save(target, "JPEG", quality=86, optimize=True)
    return target


def process_images(
    urls: list[str],
    item_id: str,
    note_folder: Path,
    run_ocr: bool,
) -> list[ImageResult]:
    results: list[ImageResult] = []
    if not urls:
        return results
    asset_folder = ASSET_ROOT / item_id
    temp_folder = Path(tempfile.mkdtemp(prefix=f"wechat-{item_id}-", dir=DATA_DIR))
    staged_assets = temp_folder / "assets"
    downloaded: list[tuple[int, ImageResult, Path, Path, bool]] = []
    try:
        for index, url in enumerate(urls, 1):
            result = ImageResult(source_url=url)
            raw_path = temp_folder / f"{index:03d}.png"
            staged_webp = staged_assets / f"{index:03d}.webp"
            try:
                result.original_bytes = download_image(url, raw_path)
                with Image.open(raw_path) as probe:
                    result.width, result.height = probe.size
                eligible = (
                    result.width >= 260
                    and result.height >= 120
                    and result.width * result.height >= 55_000
                )
                downloaded.append((index, result, raw_path, staged_webp, eligible))
            except (requests.RequestException, OSError, UnidentifiedImageError) as exc:
                result.error = str(exc)
                result.ocr_status = "error"
            results.append(result)

        eligible_images = sorted(
            (item for item in downloaded if item[4]),
            key=lambda item: (
                0
                if item[1].height / max(item[1].width, 1) > 4
                else 1,
                min(item[1].height / max(item[1].width, 1), 4),
                item[1].width * item[1].height,
            ),
            reverse=True,
        )
        ocr_candidates = eligible_images[:MAX_OCR_IMAGES] if run_ocr else []
        if ocr_candidates:
            try:
                paths = [
                    prepare_ocr_image(
                        item[2],
                        temp_folder / f"{item[0]:03d}.ocr.jpg",
                    )
                    for item in ocr_candidates
                ]
                predictions = ocr_provider.recognize(paths)
                if len(predictions) != len(ocr_candidates):
                    raise RuntimeError(
                        f"OCR 返回 {len(predictions)} 项，但输入了 {len(ocr_candidates)} 张图片"
                    )
                for candidate, prediction in zip(ocr_candidates, predictions):
                    result = candidate[1]
                    result.ocr_text = prediction.text
                    result.ocr_status = prediction.status
                    result.error = prediction.error
            except Exception as exc:
                for candidate in ocr_candidates:
                    result = candidate[1]
                    result.ocr_status = "error"
                    result.error = f"OCR: {exc}"

        candidate_ids = {id(item[1]) for item in ocr_candidates}
        for index, result, raw_path, staged_webp, eligible in downloaded:
            if id(result) not in candidate_ids:
                if run_ocr and eligible:
                    result.ocr_status = "skipped_limit"
                elif run_ocr:
                    result.ocr_status = "skipped_small"
                else:
                    result.ocr_status = "pending" if eligible else "skipped_small"

            has_text = len(re.sub(r"\s+", "", result.ocr_text)) >= 12
            informative_visual = (
                eligible
                and result.width * result.height >= 220_000
                and index <= 8
            )
            if has_text or informative_visual:
                width, height, compressed = compress_image(raw_path, staged_webp)
                result.width, result.height = width, height
                result.compressed_bytes = compressed
                final_webp = asset_folder / staged_webp.name
                result.local_path = os.path.relpath(final_webp, note_folder).replace(
                    "\\", "/"
                )
                result.kept = True

        backup_folder = asset_folder.with_name(f"{asset_folder.name}.previous")
        if backup_folder.exists():
            shutil.rmtree(backup_folder)
        if asset_folder.exists():
            asset_folder.rename(backup_folder)
        try:
            if staged_assets.exists():
                asset_folder.parent.mkdir(parents=True, exist_ok=True)
                staged_assets.rename(asset_folder)
            if backup_folder.exists():
                shutil.rmtree(backup_folder)
        except Exception:
            if asset_folder.exists():
                shutil.rmtree(asset_folder)
            if backup_folder.exists():
                backup_folder.rename(asset_folder)
            raise
    finally:
        shutil.rmtree(temp_folder, ignore_errors=True)
    return results


def categorize(title: str, text: str) -> str:
    haystack = f"{title}\n{text}".lower()
    groups = [
        ("清洁能源与产业", ["储能", "电力", "算电", "绿色算力", "新能源", "充电桩", "光伏", "风电", "数据中心", "冷却"]),
        ("AI与技术", ["ai", "agent", "codex", "claude", "openai", "模型", "算力", "软件", "开发", "markdown"]),
        ("职业与组织", ["职场", "岗位", "招聘", "实习", "fde", "咨询", "外包", "团队", "薪"]),
        ("商业与财经", ["财经", "企业", "商业", "会员", "股", "融资", "市场", "投资"]),
        ("教育与成长", ["课程", "招生", "学校", "大学", "学习", "训练营", "学霸"]),
        ("社会与生活", ["暴雨", "咖啡", "生活", "天气", "穿搭", "白宫"]),
    ]
    scores = [(name, sum(haystack.count(word) for word in words)) for name, words in groups]
    name, score = max(scores, key=lambda item: item[1])
    return name if score else "其他"


def assess_quality(
    title: str,
    account: str,
    text: str,
    ocr_text: str,
    body_available: bool,
) -> tuple[int, str, list[str]]:
    compact = re.sub(r"\s+", "", text)
    combined = f"{title}\n{text}\n{ocr_text}"
    score = 42
    flags: list[str] = []
    if body_available:
        score += 12
    else:
        score -= 28
        flags.append("正文缺失")
    score += min(22, len(compact) // 180)
    if len(compact) >= 1200:
        score += 7
    if len(ocr_text.strip()) >= 120:
        score += 6
    if account and account != "微信公众平台":
        score += 4
    marketing_words = [
        "扫码",
        "加微信",
        "立即报名",
        "限时",
        "优惠",
        "点击下方",
        "领取",
        "名额有限",
        "咨询客服",
        "转发朋友圈",
    ]
    marketing_hits = sum(combined.count(word) for word in marketing_words)
    if marketing_hits >= 3:
        score -= min(24, 8 + marketing_hits * 2)
        flags.append("营销信号较强")
    elif marketing_hits:
        score -= 4
        flags.append("含推广引导")
    clickbait_words = ["震惊", "刚刚", "终于", "一定要看", "重磅", "内幕", "暴涨", "疯了"]
    if sum(title.count(word) for word in clickbait_words) >= 2:
        score -= 10
        flags.append("标题党风险")
    if len(compact) < 250 and len(ocr_text.strip()) < 80:
        score -= 14
        flags.append("信息量较低")
    score = max(0, min(100, score))
    tier = "高" if score >= 75 else "中" if score >= 50 else "低"
    return score, tier, flags


def frontmatter_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_note(
    result: ArticleResult,
    published: str,
    text: str,
    images: list[ImageResult],
) -> str:
    lines = [
        "---",
        f"title: {frontmatter_value(result.title)}",
        f"sourceUrl: {frontmatter_value(result.source_url)}",
        'platform: "wechat_mp"',
        f"corpus_namespace: {frontmatter_value(knowledge_schema.PROFESSIONAL_REFERENCE)}",
        'authorship: "external"',
        'confidentiality: "public_external"',
        'engagement_status: "unread"',
        'stance: "unreviewed"',
        "persona_influence: 0.0",
        f"account: {frontmatter_value(result.account)}",
        f"category: {frontmatter_value(result.category)}",
        f"quality_score: {result.quality_score}",
        f"quality_tier: {frontmatter_value(result.quality_tier)}",
        f"quality_flags: {frontmatter_value(result.quality_flags)}",
        f"knowledge_type: {frontmatter_value(result.knowledge_type)}",
        f"knowledge_value_score: {result.knowledge_value_score}",
        f"knowledge_priority: {frontmatter_value(result.knowledge_priority)}",
        f"value_reasons: {frontmatter_value(result.value_reasons)}",
        f"mastery_status: {frontmatter_value(result.mastery_status)}",
        f"preference_adjustment: {result.preference_adjustment}",
        f"preference_reasons: {frontmatter_value(result.preference_reasons)}",
        f"auto_remove_recommended: {str(result.auto_remove_recommended).lower()}",
        f"content_status: {frontmatter_value(result.content_status)}",
        f"recovery_source: {frontmatter_value(result.recovery_source)}",
        f"text_length: {result.text_length}",
        f"ocr_length: {result.ocr_length}",
        f"ocr_status: {frontmatter_value(result.ocr_status)}",
        f"image_count: {result.image_count}",
        f"kept_image_count: {result.kept_image_count}",
        f"imported_at: {frontmatter_value(now_text())}",
    ]
    if published:
        lines.append(f"published_at: {frontmatter_value(published)}")
    lines += [
        "---",
        "",
        f"# {result.title}",
        "",
        f"- 公众号：{result.account or '未知'}",
        f"- 分类：{result.category}",
        f"- 质量：{result.quality_tier}（{result.quality_score}/100）",
        f"- 知识价值：{result.knowledge_type} · {result.knowledge_priority}（{result.knowledge_value_score}/100）",
        f"- 原文：{result.source_url}",
        "",
    ]
    if (
        result.knowledge_priority == "重点"
        or result.value_summary
        or result.key_insights
    ):
        lines += ["<!-- knowledge-value:start -->"]
    if result.knowledge_priority == "重点":
        lines += [
            "> [!important] 重点知识",
            "> 这篇包含可复用的见解、方法或案例，优先进入你的长期知识网络。",
            "",
        ]
    if result.value_summary or result.key_insights:
        lines += ["## 知识提炼", ""]
        if result.value_summary:
            lines += [result.value_summary, ""]
        if result.key_insights:
            lines += ["### 关键点", ""]
            lines += [f"- {item}" for item in result.key_insights]
            lines.append("")
    if (
        result.knowledge_priority == "重点"
        or result.value_summary
        or result.key_insights
    ):
        lines += ["<!-- knowledge-value:end -->", ""]
    lines += [
        "## 正文",
        "",
        text.strip() or "> 正文未能取得，已保留标题与原文链接，等待后续补抓。",
        "",
    ]
    kept = [item for item in images if item.kept]
    ocr_items = [item for item in images if item.ocr_text.strip()]
    if kept:
        lines += ["## 精简图片", ""]
        for index, item in enumerate(kept, 1):
            lines += [
                f"### 图 {index}",
                "",
                f"![]({urllib.parse.quote(item.local_path, safe='/._-')})",
                "",
            ]
    if ocr_items:
        lines += ["## 图片 OCR", ""]
        for index, item in enumerate(ocr_items, 1):
            lines += [f"### OCR {index}", "", item.ocr_text.strip(), ""]
    if result.quality_flags:
        lines += ["## 质量提示", "", "- " + "\n- ".join(result.quality_flags), ""]
    return "\n".join(lines).strip() + "\n"


def output_folder(result: ArticleResult) -> Path:
    if result.knowledge_priority == "重点":
        return ARTICLE_ROOT / "重点知识" / safe_name(result.category, 30)
    if result.knowledge_priority == "速览":
        return ARTICLE_ROOT / "资讯速览"
    if result.knowledge_priority == "回收建议":
        return ARTICLE_ROOT / "低价值待清理"
    return ARTICLE_ROOT / safe_name(result.category, 30)


def import_one(
    url: str,
    title_hint: str = "",
    run_ocr: bool = True,
) -> ArticleResult:
    result = ArticleResult(source_url=url, title=title_hint or "微信公众号文章")
    try:
        html_text, final_url = fetch_html(url)
        result.source_url = history_source.canonical_article_url(final_url) or url
        existing_notes = list(
            ARTICLE_ROOT.rglob(f"*--{article_id(result.source_url)}.md")
        )
        existing_text = (
            existing_notes[0].read_text(encoding="utf-8", errors="replace")
            if existing_notes
            else ""
        )
        doc = html.fromstring(html_text)
        title, account, published = extract_metadata(doc, title_hint)
        if account in {"", "微信公众平台"}:
            account = frontmatter_field(existing_text, "account") or account
        if not published:
            published = frontmatter_field(existing_text, "published_at")
        result.title = title
        result.account = account
        root = content_root(doc)
        text, text_length = extract_plain_text(root)
        if not text_length:
            text, text_length, embedded_source = extract_embedded_text(
                doc,
                html_text,
            )
            if text_length:
                result.recovery_source = embedded_source
        result.text_length = text_length
        result.content_status = "complete" if text_length else "metadata_only"
        result.category = categorize(title, text)

        provisional_folder = ARTICLE_ROOT / safe_name(result.category, 30)
        provisional_folder.mkdir(parents=True, exist_ok=True)
        images = process_images(
            article_image_urls(doc, root),
            article_id(result.source_url),
            provisional_folder,
            run_ocr,
        )
        ocr_text = "\n".join(item.ocr_text for item in images if item.ocr_text)
        result.ocr_length = len(re.sub(r"\s+", "", ocr_text))
        attempted_ocr = [
            item for item in images if item.ocr_status in {"success", "empty"}
        ]
        result.ocr_status = (
            "complete"
            if run_ocr and attempted_ocr and result.ocr_length > 0
            else "empty"
            if run_ocr and attempted_ocr
            else "error"
            if run_ocr and any(item.ocr_status == "error" for item in images)
            else "pending_model"
            if run_ocr and any(
                item.ocr_status == "pending_model" for item in images
            )
            else "pending_codex"
            if run_ocr and any(
                item.ocr_status == "pending_codex" for item in images
            )
            else "pending"
            if not run_ocr and any(
                item.ocr_status == "pending" for item in images
            )
            else "not_applicable"
        )
        result.image_count = len(images)
        result.kept_image_count = sum(1 for item in images if item.kept)
        result.original_image_bytes = sum(item.original_bytes for item in images)
        result.compressed_image_bytes = sum(item.compressed_bytes for item in images)
        result.quality_score, result.quality_tier, result.quality_flags = assess_quality(
            title,
            account,
            text,
            ocr_text,
            bool(text_length),
        )
        (
            result.preference_adjustment,
            result.preference_reasons,
        ) = quality_feedback.score_adjustment(title, account, result.category)
        result.quality_score = max(
            0,
            min(100, result.quality_score + result.preference_adjustment),
        )
        result.quality_tier = (
            "高"
            if result.quality_score >= 75
            else "中"
            if result.quality_score >= 50
            else "低"
        )
        if result.preference_reasons:
            result.quality_flags.append(
                "个人偏好：" + "；".join(result.preference_reasons)
            )
        value = knowledge_value.assess(
            title=title,
            body=text,
            quality_score=result.quality_score,
            content_status=result.content_status,
        )
        result.knowledge_type = value.knowledge_type
        result.knowledge_value_score = value.value_score
        result.knowledge_priority = (
            frontmatter_field(existing_text, "knowledge_priority")
            if frontmatter_field(existing_text, "priority_override").lower()
            == "true"
            else value.priority
        ) or value.priority
        result.value_reasons = value.reasons
        result.value_summary = value.summary
        result.key_insights = value.highlights
        result.mastery_status = (
            frontmatter_field(existing_text, "mastery_status") or "未学习"
        )
        (
            result.auto_remove_recommended,
            result.auto_remove_confidence,
            auto_remove_reasons,
        ) = quality_feedback.auto_remove_decision(
            account=account,
            category=result.category,
            score=result.quality_score,
        )
        if result.auto_remove_recommended:
            result.quality_flags.append(
                "自动清理建议：" + "；".join(auto_remove_reasons)
            )
        if (
            result.knowledge_type == "营销活动"
            and result.knowledge_value_score < 30
        ):
            result.auto_remove_recommended = True
            result.auto_remove_confidence = max(
                result.auto_remove_confidence,
                0.9,
            )
            result.quality_flags.append(
                "自动清理建议：营销活动且长期知识价值低于 30"
            )
        folder = output_folder(result)
        folder.mkdir(parents=True, exist_ok=True)
        if folder != provisional_folder:
            for item in images:
                if item.local_path:
                    absolute = (provisional_folder / item.local_path).resolve()
                    item.local_path = os.path.relpath(absolute, folder).replace("\\", "/")
        note_path = folder / f"{safe_name(title)}--{article_id(result.source_url)}.md"
        note_path.write_text(
            build_note(result, published, text, images),
            encoding="utf-8",
        )
        for previous in existing_notes:
            if previous.resolve() != note_path.resolve() and previous.is_file():
                previous.unlink()
        codex_ocr_queue.enqueue_note(note_path)
        import codex_curation_queue

        codex_curation_queue.enqueue(note_path)
        result.note_path = str(note_path)
        result.validation = validate_note(note_path, result)
    except Exception as exc:
        result.errors.append(str(exc))
        result.validation = "failed"
    return result


def parse_source_url(text: str) -> str:
    match = re.search(r"^sourceUrl:\s*[\"']?([^\"'\r\n]+)", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def frontmatter_field(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", text or "", re.MULTILINE)
    if not match:
        return ""
    raw = match.group(1).strip()
    try:
        value = json.loads(raw)
        return str(value) if value is not None else ""
    except json.JSONDecodeError:
        return raw.strip("\"'")


def validate_note(path: Path, expected: ArticleResult | None = None) -> str:
    if not path.is_file():
        return "failed:missing_note"
    text = path.read_text(encoding="utf-8", errors="replace")
    failures: list[str] = []
    for field_name in ("title:", "sourceUrl:", "category:", "quality_score:", "content_status:"):
        if field_name not in text:
            failures.append(f"missing_{field_name.rstrip(':')}")
    if "mmbiz.qpic.cn" in text:
        failures.append("remote_wechat_image")
    for match in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        decoded = urllib.parse.unquote(match)
        if decoded.startswith("http"):
            failures.append("remote_image")
        elif not (path.parent / decoded).resolve().is_file():
            failures.append("missing_asset")
    if expected and expected.content_status == "complete" and expected.text_length <= 0:
        failures.append("empty_complete")
    image_match = re.search(r"^image_count:\s*(\d+)", text, re.MULTILINE)
    ocr_length_match = re.search(r"^ocr_length:\s*(\d+)", text, re.MULTILINE)
    ocr_status_match = re.search(
        r'^ocr_status:\s*["\']?([^"\'\r\n]+)', text, re.MULTILINE
    )
    image_count = int(image_match.group(1)) if image_match else 0
    ocr_length = int(ocr_length_match.group(1)) if ocr_length_match else 0
    ocr_status = ocr_status_match.group(1).strip() if ocr_status_match else ""
    content_match = re.search(
        r'^content_status:\s*["\']?([^"\'\r\n]+)', text, re.MULTILINE
    )
    content_status = content_match.group(1).strip() if content_match else ""
    if (
        image_count >= 5
        and ocr_length == 0
        and ocr_status not in {
            "not_applicable",
            "disabled",
            "pending",
            "pending_model",
            "pending_codex",
        }
        and content_status == "metadata_only"
    ):
        failures.append("ocr_empty")
    return "passed" if not failures else "failed:" + ",".join(sorted(set(failures)))


def scan_vault() -> dict[str, Any]:
    notes = list(ARTICLE_ROOT.rglob("*.md")) if ARTICLE_ROOT.exists() else []
    json_files = list(WECHAT_ROOT.rglob("*.json")) if WECHAT_ROOT.exists() else []
    passed = 0
    failed: list[dict[str, str]] = []
    metadata_only = 0
    low_quality = 0
    ocr_pending = 0
    priority_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    total_bytes = 0
    for note in notes:
        text = note.read_text(encoding="utf-8", errors="replace")
        status = validate_note(note)
        total_bytes += note.stat().st_size
        if status == "passed":
            passed += 1
        else:
            failed.append({"path": str(note), "reason": status})
        if re.search(r'^content_status:\s*["\']?metadata_only', text, re.MULTILINE):
            metadata_only += 1
        if re.search(r'^quality_tier:\s*["\']?低', text, re.MULTILINE):
            low_quality += 1
        if re.search(
            r'^ocr_status:\s*["\']?pending(?:_(?:model|codex))?',
            text,
            re.MULTILINE,
        ):
            ocr_pending += 1
        priority = frontmatter_field(text, "knowledge_priority") or "未分类"
        knowledge_type = frontmatter_field(text, "knowledge_type") or "未分类"
        priority_counts[priority] = priority_counts.get(priority, 0) + 1
        type_counts[knowledge_type] = type_counts.get(knowledge_type, 0) + 1
    asset_files = list(ASSET_ROOT.rglob("*.webp")) if ASSET_ROOT.exists() else []
    asset_bytes = sum(path.stat().st_size for path in asset_files)
    return {
        "generated_at": now_text(),
        "note_count": len(notes),
        "passed_count": passed,
        "failed_count": len(failed),
        "metadata_only_count": metadata_only,
        "low_quality_count": low_quality,
        "ocr_pending_count": ocr_pending,
        "priority_counts": priority_counts,
        "type_counts": type_counts,
        "json_in_vault_count": len(json_files),
        "asset_count": len(asset_files),
        "asset_bytes": asset_bytes,
        "markdown_bytes": total_bytes,
        "failures": failed,
        "status": "passed" if not failed and not json_files else "failed",
    }


def write_index(results: list[ArticleResult]) -> None:
    rows = [
        "# 微信知识库索引",
        "",
        f"> 更新时间：{now_text()}",
        "",
        "| 文章 | 分类 | 质量 | 状态 | 公众号 |",
        "|---|---|---:|---|---|",
    ]
    for item in sorted(results, key=lambda value: (-value.quality_score, value.title)):
        if not item.note_path:
            continue
        relative = os.path.relpath(item.note_path, WECHAT_ROOT).replace("\\", "/")
        rows.append(
            f"| [[{relative[:-3]}\\|{item.title}]] | {item.category} | "
            f"{item.quality_tier} {item.quality_score} | {item.content_status} | "
            f"{item.account or '未知'} |"
        )
    INDEX_PATH.write_text("\n".join(rows) + "\n", encoding="utf-8")


def write_report(report: dict[str, Any], results: list[ArticleResult]) -> None:
    SYSTEM_ROOT.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(
        json.dumps({"validation": report, "articles": [asdict(item) for item in results]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    compression = 0.0
    original = sum(item.original_image_bytes for item in results)
    compressed = sum(item.compressed_image_bytes for item in results)
    if original:
        compression = (1 - compressed / original) * 100
    lines = [
        "# 知识库验收报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 总状态：{'通过' if report['status'] == 'passed' else '未通过'}",
        f"- Markdown：{report['note_count']} 篇",
        f"- 校验通过：{report['passed_count']} 篇",
        f"- 校验失败：{report['failed_count']} 篇",
        f"- 仅元数据：{report['metadata_only_count']} 篇",
        f"- 低质量待审：{report['low_quality_count']} 篇",
        f"- OCR 待处理：{report['ocr_pending_count']} 篇",
        f"- Vault 内 JSON：{report['json_in_vault_count']} 个",
        f"- 压缩图片：{report['asset_count']} 张，{report['asset_bytes'] / 1024 / 1024:.2f} MB",
        f"- 本轮原图压缩率：{compression:.1f}%",
        "",
        "## 验收规则",
        "",
        "- 每篇必须是 Markdown，并包含来源、分类、质量分和内容状态。",
        "- 微信远程图片必须清零；保留图片必须是本地压缩 WebP。",
        "- OCR 文本直接写入 Markdown，JSON 只允许留在程序中间目录。",
        "- 低质量内容不删除，进入“低质量待审”以便后续复核。",
    ]
    if report["failures"]:
        lines += ["", "## 失败项", ""]
        for item in report["failures"]:
            lines.append(f"- `{item['path']}`：{item['reason']}")
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def current_results_from_notes() -> list[ArticleResult]:
    results: list[ArticleResult] = []
    if not ARTICLE_ROOT.exists():
        return results
    for path in ARTICLE_ROOT.rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        def parsed_value(name: str, default: Any = "") -> Any:
            match = re.search(rf"^{re.escape(name)}:\s*(.+)$", text, re.MULTILINE)
            if not match:
                return default
            raw = match.group(1).strip()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw.strip("\"'")

        def value(name: str) -> str:
            return str(parsed_value(name, ""))

        quality_flags = parsed_value("quality_flags", [])
        preference_reasons = parsed_value("preference_reasons", [])
        body_match = re.search(
            r"^## 正文\s*\n(.*?)(?=^## |\Z)",
            text,
            re.MULTILINE | re.DOTALL,
        )
        derived_text_length = len(
            re.sub(r"\s+", "", body_match.group(1) if body_match else "")
        )
        results.append(
            ArticleResult(
                source_url=value("sourceUrl"),
                title=value("title") or path.stem,
                account=value("account"),
                category=value("category") or "其他",
                quality_score=int(value("quality_score") or 0),
                quality_tier=value("quality_tier") or "低",
                quality_flags=quality_flags if isinstance(quality_flags, list) else [],
                knowledge_type=value("knowledge_type") or "参考资料",
                knowledge_value_score=int(value("knowledge_value_score") or 0),
                knowledge_priority=value("knowledge_priority") or "参考",
                value_reasons=(
                    parsed_value("value_reasons", [])
                    if isinstance(parsed_value("value_reasons", []), list)
                    else []
                ),
                mastery_status=value("mastery_status") or "未学习",
                preference_adjustment=int(value("preference_adjustment") or 0),
                preference_reasons=(
                    preference_reasons
                    if isinstance(preference_reasons, list)
                    else []
                ),
                auto_remove_recommended=(
                    value("auto_remove_recommended").lower() == "true"
                ),
                content_status=value("content_status") or "metadata_only",
                recovery_source=value("recovery_source"),
                text_length=int(
                    value("text_length") or derived_text_length
                ),
                ocr_length=int(value("ocr_length") or 0),
                ocr_status=value("ocr_status") or "not_run",
                image_count=int(value("image_count") or 0),
                kept_image_count=int(value("kept_image_count") or 0),
                note_path=str(path),
                validation=validate_note(path),
            )
        )
    return results


def run_history(limit: int, run_ocr: bool) -> list[ArticleResult]:
    candidates = history_source.history_candidates()
    if not candidates:
        raise RuntimeError("没有找到微信浏览历史")
    records = list(reversed(history_source.read_history(candidates[0], 0)))
    if limit:
        records = records[:limit]
    results: list[ArticleResult] = []
    existing_by_url = {
        history_source.canonical_article_url(item.source_url): item
        for item in current_results_from_notes()
        if item.source_url
    }
    for index, record in enumerate(records, 1):
        print(f"[{index}/{len(records)}] {record['title']}", flush=True)
        canonical = history_source.canonical_article_url(record["url"])
        existing = existing_by_url.get(canonical)
        if not run_ocr and existing and existing.validation == "passed":
            existing_text = Path(existing.note_path).read_text(
                encoding="utf-8", errors="replace"
            )
            if re.search(r'^ocr_status:\s*["\']?complete', existing_text, re.MULTILINE):
                results.append(existing)
                print(
                    json.dumps(
                        {
                            "title": existing.title,
                            "status": existing.validation,
                            "reused": "ocr_complete",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                continue
        result = import_one(record["url"], record["title"], run_ocr=run_ocr)
        results.append(result)
        print(
            json.dumps(
                {
                    "title": result.title,
                    "status": result.validation,
                    "quality": result.quality_score,
                    "images": result.image_count,
                    "ocr_length": result.ocr_length,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    write_index(results)
    report = scan_vault()
    write_report(report, results)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="微信历史到精简 Obsidian 知识库")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--url")
    source.add_argument("--history", action="store_true")
    parser.add_argument("--title", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    WECHAT_ROOT.mkdir(parents=True, exist_ok=True)
    if args.validate_only:
        results = current_results_from_notes()
        write_index(results)
        report = scan_vault()
        write_report(report, results)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "passed" else 1

    if args.url:
        results = [import_one(args.url, args.title, run_ocr=not args.no_ocr)]
        write_index(results)
        report = scan_vault()
        write_report(report, results)
    else:
        results = run_history(args.limit, run_ocr=not args.no_ocr)
        report = scan_vault()
    print(
        json.dumps(
            {"validation": report, "articles": [asdict(item) for item in results]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
