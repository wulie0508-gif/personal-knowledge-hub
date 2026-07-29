#!/usr/bin/env python3
"""Local, private link inbox for an Obsidian knowledge base."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import quality_feedback
import runtime_config


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = runtime_config.runtime_home()
CONFIG_PATH = runtime_config.config_path()
JOBS_PATH = runtime_config.private_path("jobs.json")
TRASH_DIR = runtime_config.private_path("trash", "knowledge")
TRASH_LEARNING_LOCK = threading.RLock()
TRASH_LEARNING_STATE: dict[str, Any] = {
    "last_scan_monotonic": 0.0,
    "last_result": {},
}

DEFAULT_VAULT = runtime_config.configured_vault()
ROUTER_DIR = Path(
    os.environ.get(
        "WECHAT_CONTENT_ROUTER_ROOT",
        str(Path.home() / ".codex" / "skills" / "wechat-content-router-windows"),
    )
)
ARCHIVER_DIR = Path(
    os.environ.get(
        "WECHAT_MP_ARCHIVER_ROOT",
        str(Path.home() / ".codex" / "skills" / "wechat-mp-obsidian-archiver"),
    )
)
ARCHIVER_HOME = Path(
    os.environ.get(
        "WECHAT_MP_ARCHIVER_HOME",
        str(Path.home() / ".config" / "wechat-mp-obsidian-archiver"),
    )
)

URL_RE = re.compile(r"https?://[^\s<>\]）)\"']+", re.IGNORECASE)
STORE_LOCK = threading.RLock()
WORK_QUEUE: queue.Queue[str] = queue.Queue()
OCR_QUEUE: queue.Queue[tuple[str, str]] = queue.Queue()
OCR_LOCK = threading.RLock()
OCR_QUEUED: set[str] = set()
OCR_STATE: dict[str, Any] = {
    "status": "idle",
    "current_title": "",
    "completed": 0,
    "failed": 0,
    "last_error": "",
    "updated_at": "",
}
LOGIN_LOCK = threading.RLock()
LOGIN_PROCESS: subprocess.Popen[str] | None = None
LOGIN_STATE: dict[str, Any] = {
    "status": "idle",
    "message": "尚未开始扫码登录",
    "qr_available": False,
    "updated_at": "",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def default_config() -> dict[str, Any]:
    return {
        "vault_path": str(DEFAULT_VAULT),
        "share_to_save_output": "intermediate/share-to-save",
        "invasive_router_enabled": False,
        "invasive_confirmed_at": "",
        "port": 8765,
    }


def load_config() -> dict[str, Any]:
    config = default_config()
    config.update(load_json(CONFIG_PATH, {}))
    return config


def save_config(config: dict[str, Any]) -> None:
    atomic_json(CONFIG_PATH, config)


def vault_path() -> Path:
    configured = str(load_config().get("vault_path") or "").strip()
    return Path(configured).expanduser() if configured else runtime_config.configured_vault()


def load_jobs() -> list[dict[str, Any]]:
    return load_json(JOBS_PATH, [])


def save_jobs(jobs: list[dict[str, Any]]) -> None:
    atomic_json(JOBS_PATH, jobs[-300:])


def find_job(job_id: str) -> dict[str, Any] | None:
    with STORE_LOCK:
        return next((job for job in load_jobs() if job["id"] == job_id), None)


def update_job(job_id: str, **changes: Any) -> dict[str, Any] | None:
    with STORE_LOCK:
        jobs = load_jobs()
        result = None
        for job in jobs:
            if job["id"] == job_id:
                job.update(changes)
                job["updated_at"] = now_iso()
                result = job.copy()
                break
        save_jobs(jobs)
        return result


def clean_url(value: str) -> str:
    return value.rstrip(".,;:!?，。；：！？")


def extract_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in URL_RE.findall(text or ""):
        url = clean_url(match)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def classify(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    if host.endswith("mp.weixin.qq.com"):
        return "wechat_mp"
    if "xiaohongshu.com" in host or "xhslink.com" in host:
        return "xiaohongshu"
    if "feishu.cn" in host or "larkoffice.com" in host:
        return "feishu"
    return "web"


def queue_log(job: dict[str, Any]) -> None:
    target = vault_path() / "00_Inbox" / "WeChat-Queue" / "微信链接提交.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text("# 微信链接提交\n\n", encoding="utf-8")
    block = (
        f"\n- [ ] {job['url']}\n"
        f"  - 提交时间：{job['created_at']}\n"
        f"  - 路由：{job['route']}\n"
        f"  - 任务：`{job['id']}`\n"
    )
    with target.open("a", encoding="utf-8") as handle:
        handle.write(block)


def enqueue_share_to_save(url: str) -> Path:
    output = DATA_DIR / "intermediate" / "share-to-save"
    output.mkdir(parents=True, exist_ok=True)
    entry_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc)
    compact = created.strftime("%Y%m%dT%H%M%S")
    entry = {
        "id": entry_id,
        "url": url,
        "source": "desktop",
        "createdAt": created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    path = output / f"toBeSaved_{compact}_{entry_id}.json"
    atomic_json(path, entry)
    return path


def run_command(command: list[str], timeout: int = 120, env: dict[str, str] | None = None) -> tuple[int, str]:
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})}
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=child_env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    return completed.returncode, output[-6000:]


def parse_json_output(output: str) -> dict[str, Any]:
    start = output.find("{")
    end = output.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(output[start : end + 1])
        except json.JSONDecodeError:
            pass
    return {}


def import_with_router(
    url: str,
    kind: str,
    title_hint: str = "",
) -> dict[str, Any]:
    if kind == "wechat_mp":
        script = ROOT / "knowledge_pipeline.py"
        if not script.exists():
            raise RuntimeError("微信知识流水线未安装")
        code, output = run_command(
            [
                sys.executable,
                str(script),
                "--url",
                url,
                "--title",
                title_hint,
                "--no-ocr",
            ],
            timeout=240,
        )
        if code != 0:
            raise RuntimeError(output or f"微信知识流水线退出码 {code}")
        payload = parse_json_output(output)
        article = ((payload.get("articles") or [{}])[0]) or {}
        return {
            "message": (
                "已按你的历史偏好标记为自动清理"
                if article.get("auto_remove_recommended")
                else "正文已写入 Obsidian，图片已进入 Codex OCR 队列"
            ),
            "output_file": article.get("note_path") or "",
            "title": article.get("title") or "",
            "details": {
                **article,
                "body_length": article.get("text_length", 0),
            },
        }

    scripts = ROUTER_DIR / "scripts"
    script_by_kind = {
        "wechat_mp": scripts / "import_wechat_mp_article.py",
        "xiaohongshu": scripts / "import_xhs_note.py",
        "feishu": scripts / "import_feishu_page.py",
    }
    script = script_by_kind.get(kind)
    if not script or not script.exists():
        raise RuntimeError("对应的内容路由器脚本未安装")
    code, output = run_command([sys.executable, str(script), url], timeout=180)
    if code != 0:
        raise RuntimeError(output or f"内容路由器退出码 {code}")
    result = parse_json_output(output)
    return {
        "message": "正文已导入 Obsidian",
        "output_file": result.get("note_path") or result.get("preview_path") or "",
        "title": result.get("title") or "",
        "details": result,
    }


def enqueue_ocr(url: str, title: str = "") -> None:
    with OCR_LOCK:
        if url in OCR_QUEUED:
            return
        OCR_QUEUED.add(url)
        OCR_QUEUE.put((url, title))


def ocr_worker_loop() -> None:
    import knowledge_pipeline

    while True:
        url, title = OCR_QUEUE.get()
        with OCR_LOCK:
            OCR_STATE.update(
                status="running",
                current_title=title or url,
                last_error="",
                updated_at=now_iso(),
        )
        try:
            result = knowledge_pipeline.import_one(url, title, run_ocr=True)
            regenerate_knowledge_views()
            if result.validation != "passed":
                raise RuntimeError(
                    "; ".join(result.errors) or result.validation
                )
            with OCR_LOCK:
                OCR_STATE["completed"] += 1
                OCR_STATE.update(
                    status="idle",
                    current_title="",
                    updated_at=now_iso(),
                )
        except Exception as exc:
            with OCR_LOCK:
                OCR_STATE["failed"] += 1
                OCR_STATE.update(
                    status="error",
                    last_error=str(exc),
                    updated_at=now_iso(),
                )
        finally:
            with OCR_LOCK:
                OCR_QUEUED.discard(url)
            OCR_QUEUE.task_done()


def resume_ocr_jobs() -> None:
    try:
        import codex_ocr_queue

        codex_ocr_queue.sync()
        provider = codex_ocr_queue.status()
        with OCR_LOCK:
            OCR_STATE.update(
                status=(
                    "waiting_for_codex"
                    if provider.get("pending_count")
                    else "idle"
                ),
                current_title="",
                last_error="",
                provider=provider,
                updated_at=now_iso(),
            )
    except Exception as exc:
        with OCR_LOCK:
            OCR_STATE.update(
                status="error",
                last_error=f"OCR 队列恢复失败：{exc}",
                updated_at=now_iso(),
            )


def add_archiver_subscription(url: str, interval: int, since: str) -> dict[str, Any]:
    script = ARCHIVER_DIR / "scripts" / "wechat_subscriptions.py"
    if not script.exists():
        raise RuntimeError("公众号归档器未安装")
    command = [
        sys.executable,
        str(script),
        "add",
        "--article-url",
        url,
        "--vault-dir",
        str(vault_path()),
        "--subdir",
        "10_Sources/WeChat",
        "--image-mode",
        "local",
        "--interval",
        str(interval),
        "--since",
        since,
    ]
    code, output = run_command(command, timeout=180)
    if code != 0:
        raise RuntimeError(output or f"公众号归档器退出码 {code}")
    return {"message": "公众号已加入追更", "details": output[-2000:]}


def process_job(job_id: str) -> None:
    job = find_job(job_id)
    if not job:
        return
    update_job(job_id, status="running", message="正在提取正文")
    url = job["url"]
    kind = job["kind"]
    route = job["route"]
    result: dict[str, Any] = {}
    warnings: list[str] = []
    try:
        if kind == "local":
            import local_importer

            result = local_importer.import_path(
                str(job.get("path") or url),
                str(job.get("corpus_namespace") or "personal_memory"),
            )
            report = regenerate_knowledge_views()
            update_job(
                job_id,
                status="complete",
                message=result.get("message", "本地资料导入完成"),
                output_file=result.get("output_file", ""),
                title=result.get("title", ""),
                warnings=[],
                details={
                    **(result.get("details") or {}),
                    "knowledge_status": report.get("status"),
                },
            )
            return
        if route == "share" or (route == "auto" and kind == "web"):
            queued = enqueue_share_to_save(url)
            result = {
                "message": "已交给 Share to Save",
                "output_file": str(queued),
            }
        elif route == "archive":
            if kind != "wechat_mp":
                raise RuntimeError("历史归档只适用于微信公众号文章")
            result = add_archiver_subscription(
                url,
                int(job.get("interval", 0)),
                job.get("since") or datetime.now().date().isoformat(),
            )
        else:
            try:
                result = import_with_router(
                    url,
                    kind,
                    str(job.get("title") or ""),
                )
            except Exception as exc:
                queued = enqueue_share_to_save(url)
                warnings.append(f"正文直取失败，已自动降级到 Share to Save：{exc}")
                result = {
                    "message": "直取失败，但链接已进入安全队列",
                    "output_file": str(queued),
                }

            if job.get("subscribe") and kind == "wechat_mp":
                try:
                    sub = add_archiver_subscription(
                        url,
                        int(job.get("interval", 360)),
                        job.get("since") or datetime.now().date().isoformat(),
                    )
                    result["subscription"] = sub
                except Exception as exc:
                    warnings.append(f"正文已保存；追更尚需完成一次扫码登录：{exc}")

            if kind in {"xiaohongshu", "feishu"}:
                note_path = Path(str(result.get("output_file") or ""))
                if note_path.is_file() and note_path.suffix.lower() == ".md":
                    import codex_curation_queue
                    import codex_ocr_queue

                    curation_job = codex_curation_queue.enqueue(note_path)
                    ocr_job = codex_ocr_queue.enqueue_note(note_path)
                    report = regenerate_knowledge_views()
                    result.setdefault("details", {}).update(
                        {
                            "codex_curation_queued": bool(curation_job),
                            "codex_ocr_queued": bool(ocr_job),
                            "knowledge_status": report.get("status"),
                            "graph_article_count": (
                                (report.get("graph") or {}).get("article_count")
                            ),
                        }
                    )
                    if ocr_job:
                        result["message"] = (
                            "正文与图片已导入 Obsidian，图片已进入 Codex OCR 队列"
                        )

            if (
                kind == "wechat_mp"
                and (result.get("details") or {}).get(
                    "auto_remove_recommended"
                )
            ):
                removal = move_job_article_to_trash(
                    {**job, "output_file": result.get("output_file", "")},
                    record_user_feedback=False,
                )
                update_job(
                    job_id,
                    status="removed",
                    feedback="auto_remove",
                    message="依据你反复确认的偏好，已自动移入本地回收区",
                    title=result.get("title", ""),
                    output_file="",
                    trash_path=removal["trash_path"],
                    warnings=warnings,
                    details=result.get("details", {}),
                )
                return

        if (
            kind == "wechat_mp"
            and route != "archive"
            and int((result.get("details") or {}).get("body_length") or 0) == 0
        ):
            warnings.append("已保存标题和原文链接，但微信未返回正文；可能是旧链接或访问受限。")

        update_job(
            job_id,
            status="complete" if not warnings else "attention",
            message=result.get("message", "处理完成"),
            output_file=result.get("output_file", ""),
            title=result.get("title", ""),
            warnings=warnings,
            details=result.get("details", {}),
        )
    except Exception as exc:
        update_job(
            job_id,
            status="failed",
            message=str(exc),
            error=traceback.format_exc(limit=4),
        )


def worker_loop() -> None:
    while True:
        job_id = WORK_QUEUE.get()
        try:
            process_job(job_id)
        finally:
            WORK_QUEUE.task_done()


def resume_jobs() -> None:
    with STORE_LOCK:
        jobs = load_jobs()
        changed = False
        for job in jobs:
            if job.get("status") in {"pending", "running"}:
                job["status"] = "pending"
                WORK_QUEUE.put(job["id"])
                changed = True
        if changed:
            save_jobs(jobs)


def reconcile_empty_wechat_jobs() -> None:
    import knowledge_pipeline

    items = {
        knowledge_pipeline.history_source.canonical_article_url(item.source_url): item
        for item in knowledge_pipeline.current_results_from_notes()
        if item.source_url
    }
    with STORE_LOCK:
        jobs = load_jobs()
        changed = False
        for job in jobs:
            details = job.get("details") or {}
            canonical = knowledge_pipeline.history_source.canonical_article_url(
                str(job.get("url") or "")
            )
            item = items.get(canonical)
            if (
                job.get("kind") == "wechat_mp"
                and item
                and item.content_status == "complete"
                and item.text_length > 0
            ):
                was_incomplete = (
                    job.get("status") == "attention"
                    or int(details.get("body_length") or 0) == 0
                    or "未返回正文" in str(job.get("message") or "")
                    or "元数据" in str(job.get("message") or "")
                )
                job["status"] = "complete"
                job["title"] = item.title or job.get("title")
                job["output_file"] = item.note_path or job.get("output_file")
                job["details"] = {
                    **details,
                    "body_length": item.text_length,
                    "text_length": item.text_length,
                    "content_status": item.content_status,
                    "recovery_source": item.recovery_source,
                    "image_count": item.image_count,
                    "kept_image_count": item.kept_image_count,
                    "note_path": item.note_path,
                }
                job["warnings"] = [
                    warning
                    for warning in list(job.get("warnings") or [])
                    if "微信未返回正文" not in str(warning)
                    and "仅保存元数据" not in str(warning)
                ]
                if was_incomplete:
                    job["message"] = "正文已恢复并写入 Obsidian"
                    job["updated_at"] = now_iso()
                changed = True
                continue
            if (
                job.get("kind") == "wechat_mp"
                and job.get("status") == "complete"
                and "body_length" in details
                and int(details.get("body_length") or 0) == 0
            ):
                warning = "已保存标题和原文链接，但微信未返回正文；可能是旧链接或访问受限。"
                warnings = list(job.get("warnings") or [])
                if warning not in warnings:
                    warnings.append(warning)
                job["status"] = "attention"
                job["message"] = "仅保存元数据，正文未获取"
                job["warnings"] = warnings
                job["updated_at"] = now_iso()
                changed = True
        if changed:
            save_jobs(jobs)


def repair_wechat_job_titles() -> None:
    try:
        import knowledge_pipeline

        titles = {
            knowledge_pipeline.history_source.canonical_article_url(item.source_url): item.title
            for item in knowledge_pipeline.current_results_from_notes()
            if item.source_url and item.title
        }
        with STORE_LOCK:
            jobs = load_jobs()
            changed = False
            for job in jobs:
                if job.get("kind") != "wechat_mp":
                    continue
                canonical = knowledge_pipeline.history_source.canonical_article_url(
                    str(job.get("url") or "")
                )
                correct_title = titles.get(canonical)
                if correct_title and job.get("title") != correct_title:
                    job["title"] = correct_title
                    changed = True
            if changed:
                save_jobs(jobs)
    except Exception:
        return


def regenerate_knowledge_views() -> dict[str, Any]:
    import knowledge_graph
    import knowledge_pipeline

    current = knowledge_pipeline.current_results_from_notes()
    graph = knowledge_graph.build(vault_path(), quality_feedback.summary())
    knowledge_pipeline.write_index(current)
    report = knowledge_pipeline.scan_vault()
    report["graph"] = graph
    knowledge_pipeline.write_report(report, current)
    return report


def refresh_trash_learning(force: bool = False) -> dict[str, Any]:
    with TRASH_LEARNING_LOCK:
        elapsed = time.monotonic() - float(
            TRASH_LEARNING_STATE.get("last_scan_monotonic") or 0
        )
        if not force and elapsed < 60:
            return dict(TRASH_LEARNING_STATE.get("last_result") or {})
        paths = [TRASH_DIR, vault_path() / ".trash"]
        result = quality_feedback.ingest_trash_history(paths)
        TRASH_LEARNING_STATE.update(
            last_scan_monotonic=time.monotonic(),
            last_result=result,
        )
        return result


def knowledge_item_for_job(job: dict[str, Any]) -> Any:
    import knowledge_pipeline

    canonical = knowledge_pipeline.history_source.canonical_article_url(
        str(job.get("url") or "")
    )
    for item in knowledge_pipeline.current_results_from_notes():
        if (
            knowledge_pipeline.history_source.canonical_article_url(
                item.source_url
            )
            == canonical
        ):
            return item
    return None


def enrich_jobs_with_knowledge(
    jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    import knowledge_pipeline

    items = {
        knowledge_pipeline.history_source.canonical_article_url(item.source_url): item
        for item in knowledge_pipeline.current_results_from_notes()
        if item.source_url
    }
    enriched: list[dict[str, Any]] = []
    for original in jobs:
        job = original.copy()
        canonical = knowledge_pipeline.history_source.canonical_article_url(
            str(job.get("url") or "")
        )
        item = items.get(canonical)
        if item:
            job["quality"] = {
                "score": item.quality_score,
                "tier": item.quality_tier,
                "category": item.category,
                "account": item.account,
                "flags": item.quality_flags,
                "preference_adjustment": item.preference_adjustment,
                "preference_reasons": item.preference_reasons,
                "knowledge_type": item.knowledge_type,
                "knowledge_value_score": item.knowledge_value_score,
                "knowledge_priority": item.knowledge_priority,
                "mastery_status": item.mastery_status,
            }
        else:
            note_path = Path(str(job.get("output_file") or ""))
            if note_path.is_file() and note_path.suffix.lower() == ".md":
                text = note_path.read_text(encoding="utf-8", errors="replace")
                value_score = int(
                    knowledge_pipeline.frontmatter_field(
                        text, "knowledge_value_score"
                    )
                    or 0
                )
                quality_score = int(
                    knowledge_pipeline.frontmatter_field(text, "quality_score")
                    or value_score
                )
                tier = (
                    "高"
                    if quality_score >= 75
                    else "中"
                    if quality_score >= 50
                    else "低"
                )
                job["quality"] = {
                    "score": quality_score,
                    "tier": tier,
                    "category": (
                        knowledge_pipeline.frontmatter_field(text, "category")
                        or "其他"
                    ),
                    "account": (
                        knowledge_pipeline.frontmatter_field(text, "account")
                        or knowledge_pipeline.frontmatter_field(text, "author")
                    ),
                    "flags": [],
                    "preference_adjustment": 0,
                    "preference_reasons": [],
                    "knowledge_type": knowledge_pipeline.frontmatter_field(
                        text, "knowledge_type"
                    ),
                    "knowledge_value_score": value_score,
                    "knowledge_priority": knowledge_pipeline.frontmatter_field(
                        text, "knowledge_priority"
                    ),
                    "mastery_status": (
                        knowledge_pipeline.frontmatter_field(
                            text, "mastery_status"
                        )
                        or "未学习"
                    ),
                }
        enriched.append(job)
    return enriched


def move_job_article_to_trash(
    job: dict[str, Any],
    *,
    record_user_feedback: bool,
) -> dict[str, Any]:
    import knowledge_pipeline

    item = knowledge_item_for_job(job)
    title = str(getattr(item, "title", "") or job.get("title") or "未命名文章")
    account = str(getattr(item, "account", "") or "")
    category = str(getattr(item, "category", "") or "其他")
    source_url = str(getattr(item, "source_url", "") or job.get("url") or "")
    if record_user_feedback:
        quality_feedback.record_feedback(
            url=source_url,
            title=title,
            account=account,
            category=category,
            label="remove",
        )

    item_id = knowledge_pipeline.article_id(source_url)
    batch = (
        TRASH_DIR
        / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        / item_id
    )
    moved: list[str] = []
    note_candidates: list[Path] = []
    if item and item.note_path:
        note_candidates.append(Path(item.note_path))
    output_file = Path(str(job.get("output_file") or ""))
    if str(output_file) and output_file.is_file():
        note_candidates.append(output_file)

    wechat_root = knowledge_pipeline.WECHAT_ROOT.resolve()
    for note in dict.fromkeys(note_candidates):
        resolved = note.resolve()
        if not resolved.is_file() or wechat_root not in resolved.parents:
            continue
        relative = resolved.relative_to(wechat_root)
        destination = batch / "notes" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(resolved), str(destination))
        moved.append(str(destination))

    asset_folder = (knowledge_pipeline.ASSET_ROOT / item_id).resolve()
    if asset_folder.is_dir() and knowledge_pipeline.ASSET_ROOT.resolve() in asset_folder.parents:
        asset_destination = batch / "assets" / item_id
        asset_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(asset_folder), str(asset_destination))
        moved.append(str(asset_destination))

    report = regenerate_knowledge_views()
    return {
        "title": title,
        "trash_path": str(batch),
        "moved": moved,
        "validation": report.get("status"),
    }


def update_jobs_for_url(url: str, **changes: Any) -> dict[str, Any] | None:
    import knowledge_pipeline

    canonical = knowledge_pipeline.history_source.canonical_article_url(url)
    latest: dict[str, Any] | None = None
    with STORE_LOCK:
        jobs = load_jobs()
        for job in jobs:
            job_canonical = (
                knowledge_pipeline.history_source.canonical_article_url(
                    str(job.get("url") or "")
                )
            )
            if canonical and job_canonical == canonical:
                job.update(changes)
                job["updated_at"] = now_iso()
                latest = job.copy()
        save_jobs(jobs)
    return latest


def mark_knowledge_state(job: dict[str, Any], label: str) -> dict[str, Any]:
    import knowledge_pipeline
    import refresh_knowledge_value

    item = knowledge_item_for_job(job)
    if not item or not item.note_path:
        raise ValueError("文章笔记不存在")
    note = Path(item.note_path)
    text = note.read_text(encoding="utf-8", errors="replace")
    text = re.sub(
        r"\n?<!-- user-knowledge-state:start -->.*?<!-- user-knowledge-state:end -->\n?",
        "\n",
        text,
        flags=re.DOTALL,
    )
    if label == "focus":
        text = refresh_knowledge_value.replace_frontmatter(
            text,
            "knowledge_priority",
            "重点",
        )
        text = refresh_knowledge_value.replace_frontmatter(
            text,
            "priority_override",
            True,
        )
        state_block = "\n".join(
            [
                "<!-- user-knowledge-state:start -->",
                "> [!important] 我标记的重点",
                "> 这篇内容值得长期保留，并优先用于后续 AI 回答和知识关联。",
                "<!-- user-knowledge-state:end -->",
            ]
        )
        target = (
            knowledge_pipeline.ARTICLE_ROOT
            / "重点知识"
            / knowledge_pipeline.safe_name(item.category, 30)
            / note.name
        )
    else:
        text = refresh_knowledge_value.replace_frontmatter(
            text,
            "mastery_status",
            "已学会",
        )
        text = refresh_knowledge_value.replace_frontmatter(
            text,
            "reviewed_at",
            now_iso(),
        )
        state_block = "\n".join(
            [
                "<!-- user-knowledge-state:start -->",
                "> [!success] 已学会",
                "> 已完成阅读和吸收；保留为可检索的长期知识，不再作为待处理内容。",
                "<!-- user-knowledge-state:end -->",
            ]
        )
        target = note
    if "## 正文" in text:
        text = text.replace("## 正文", state_block + "\n\n## 正文", 1)
    else:
        text += "\n\n" + state_block
    target.parent.mkdir(parents=True, exist_ok=True)
    text = refresh_knowledge_value.rewrite_image_links(
        text,
        note.parent,
        target.parent,
    )
    target.write_text(text.rstrip() + "\n", encoding="utf-8")
    if target.resolve() != note.resolve() and note.is_file():
        note.unlink()
    quality_feedback.record_feedback(
        url=item.source_url,
        title=item.title,
        account=item.account,
        category=item.category,
        label="keep",
    )
    report = regenerate_knowledge_views()
    return {
        "title": item.title,
        "note_path": str(target),
        "validation": report.get("status"),
    }


def apply_job_feedback(job_id: str, label: str) -> dict[str, Any]:
    if label not in {"keep", "focus", "mastered", "remove"}:
        raise ValueError("无效的知识反馈")
    job = find_job(job_id)
    if not job:
        raise ValueError("任务不存在")
    if job.get("kind") != "wechat_mp":
        raise ValueError("当前只对微信公众号文章学习质量偏好")
    if job.get("feedback") == label:
        return {"ok": True, "job": job, "unchanged": True}

    item = knowledge_item_for_job(job)
    title = str(getattr(item, "title", "") or job.get("title") or "")
    account = str(getattr(item, "account", "") or "")
    category = str(getattr(item, "category", "") or "其他")
    source_url = str(getattr(item, "source_url", "") or job.get("url") or "")
    if label in {"focus", "mastered"}:
        state = mark_knowledge_state(job, label)
        updated = update_jobs_for_url(
            source_url,
            feedback=label,
            message=(
                "已标记为重点，并用于学习你的偏好"
                if label == "focus"
                else "已标记为学会；保留为长期检索知识"
            ),
            output_file=state["note_path"],
        )
        return {
            "ok": True,
            "job": updated,
            "state": state,
            "preferences": quality_feedback.summary(),
        }
    if label == "keep":
        preferences = quality_feedback.record_feedback(
            url=source_url,
            title=title,
            account=account,
            category=category,
            label="keep",
        )
        updated = update_jobs_for_url(
            source_url,
            feedback="keep",
            message="已保留；这次选择已用于学习你的偏好",
        )
        return {"ok": True, "job": updated, "preferences": preferences}

    removal = move_job_article_to_trash(job, record_user_feedback=True)
    updated = update_jobs_for_url(
        source_url,
        feedback="remove",
        status="removed",
        message="已移入本地回收区，并学习这次选择",
        output_file="",
        trash_path=removal["trash_path"],
    )
    return {
        "ok": True,
        "job": updated,
        "removal": removal,
        "preferences": quality_feedback.summary(),
    }


def archiver_configured() -> bool:
    config = load_json(ARCHIVER_HOME / "subscriptions.json", {})
    creds = config.get("credentials") or {}
    mp_auth = config.get("mpAuth") or {}
    return bool(
        (config.get("baseUrl") and creds.get("xid") and creds.get("token"))
        or (mp_auth.get("cookie") and mp_auth.get("token"))
    )


def archiver_qr_path() -> Path:
    return ARCHIVER_HOME / "mp-login-qr.png"


def monitor_archiver_login(process: subprocess.Popen[str]) -> None:
    global LOGIN_PROCESS
    last_line = ""
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue
        last_line = line
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            event = {"hint": line}
        with LOGIN_LOCK:
            if event.get("qrPng"):
                LOGIN_STATE.update(
                    status="waiting",
                    message=event.get("hint") or "请用微信扫描二维码",
                    qr_available=archiver_qr_path().exists(),
                    updated_at=now_iso(),
                )
            elif event.get("loggedIn"):
                LOGIN_STATE.update(
                    status="success",
                    message="登录成功，公众号历史归档与追更已经可用",
                    qr_available=False,
                    updated_at=now_iso(),
                )
            elif event.get("hint"):
                LOGIN_STATE.update(message=event["hint"], updated_at=now_iso())
    return_code = process.wait()
    with LOGIN_LOCK:
        if LOGIN_STATE["status"] != "success":
            LOGIN_STATE.update(
                status="error",
                message=last_line or f"扫码登录进程已结束（退出码 {return_code}）",
                qr_available=False,
                updated_at=now_iso(),
            )
        LOGIN_PROCESS = None


def start_archiver_login() -> dict[str, Any]:
    global LOGIN_PROCESS
    script = ARCHIVER_DIR / "scripts" / "wechat_subscriptions.py"
    if not script.exists():
        raise RuntimeError("公众号归档器未安装")
    with LOGIN_LOCK:
        if LOGIN_PROCESS and LOGIN_PROCESS.poll() is None:
            LOGIN_STATE["qr_available"] = archiver_qr_path().exists()
            return LOGIN_STATE.copy()
        qr_path = archiver_qr_path()
        if qr_path.exists():
            qr_path.unlink()
        LOGIN_STATE.update(
            status="starting",
            message="正在向微信公众平台申请二维码…",
            qr_available=False,
            updated_at=now_iso(),
        )
        LOGIN_PROCESS = subprocess.Popen(
            [sys.executable, str(script), "mp-login"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(
            target=monitor_archiver_login,
            args=(LOGIN_PROCESS,),
            name="archiver-login",
            daemon=True,
        ).start()
        return LOGIN_STATE.copy()


def archiver_login_status() -> dict[str, Any]:
    with LOGIN_LOCK:
        if archiver_configured():
            LOGIN_STATE.update(
                status="success",
                message="登录成功，公众号历史归档与追更已经可用",
                qr_available=False,
                updated_at=LOGIN_STATE.get("updated_at") or now_iso(),
            )
        elif LOGIN_STATE["status"] in {"starting", "waiting"}:
            LOGIN_STATE["qr_available"] = archiver_qr_path().exists()
        return LOGIN_STATE.copy()


def community_plugins() -> list[str]:
    value = load_json(vault_path() / ".obsidian" / "community-plugins.json", [])
    return value if isinstance(value, list) else []


def system_status() -> dict[str, Any]:
    import codex_ocr_queue
    import codex_curation_queue
    import personal_context

    refresh_trash_learning()
    config = load_config()
    vault = vault_path()
    plugin_dir = vault / ".obsidian" / "plugins" / "share-to-save"
    sts_output = DATA_DIR / "intermediate" / "share-to-save"
    watcher_state = load_json(DATA_DIR / "wechat-history-state.json", {})
    knowledge_report = load_json(
        DATA_DIR / "reports" / "knowledge-validation.json", {}
    ).get("validation", {})
    graph = knowledge_report.get("graph") or {}
    monitor_error = str(watcher_state.get("last_error") or "")
    monitor_status = watcher_state.get("monitor_status") or (
        "ready" if watcher_state.get("initialized") else "starting"
    )
    codex_ocr = codex_ocr_queue.status()
    curation = codex_curation_queue.status()
    with OCR_LOCK:
        OCR_STATE.update(
            status=(
                "waiting_for_codex"
                if codex_ocr.get("pending_count")
                else "idle"
            ),
            provider=codex_ocr,
        )
    return {
        "runtime": runtime_config.runtime_summary(),
        "vault": {
            "ready": vault.exists() and (vault / ".obsidian").exists(),
            "path": str(vault),
        },
        "share_to_save": {
            "installed": (plugin_dir / "main.js").exists() and (plugin_dir / "manifest.json").exists(),
            "enabled": "share-to-save" in community_plugins(),
            "queue_count": len(list(sts_output.glob("toBeSaved_*.json"))) if sts_output.exists() else 0,
            "output": str(sts_output),
        },
        "archiver": {
            "installed": (ARCHIVER_DIR / "SKILL.md").exists(),
            "configured": archiver_configured(),
            "home": str(ARCHIVER_HOME),
        },
        "router": {
            "installed": (ROUTER_DIR / "SKILL.md").exists(),
            "configured": (ROOT / "wechat_history_watcher.py").exists(),
            "invasive_enabled": False,
            "monitor_enabled": (ROOT / "wechat_history_watcher.py").exists(),
            "monitor_ready": bool(watcher_state.get("initialized") and monitor_status == "ready"),
            "monitor_status": monitor_status,
            "monitor_error": monitor_error,
            "chat": "微信内置浏览器",
            "trigger_phrase": "",
            "interval_seconds": 15,
            "pending_count": len(watcher_state.get("pending") or []),
            "detected_count": int(watcher_state.get("detected_count") or 0),
            "submitted_count": int(watcher_state.get("submitted_count") or 0),
            "profile": watcher_state.get("profile") or "",
            "mode": "公众号浏览历史监控",
        },
        "knowledge": {
            "status": knowledge_report.get("status", "unknown"),
            "note_count": int(knowledge_report.get("note_count") or 0),
            "passed_count": int(knowledge_report.get("passed_count") or 0),
            "failed_count": int(knowledge_report.get("failed_count") or 0),
            "metadata_only_count": int(
                knowledge_report.get("metadata_only_count") or 0
            ),
            "low_quality_count": int(
                knowledge_report.get("low_quality_count") or 0
            ),
            "ocr_pending_count": int(
                knowledge_report.get("ocr_pending_count") or 0
            ),
            "priority_counts": knowledge_report.get("priority_counts") or {},
            "type_counts": knowledge_report.get("type_counts") or {},
            "json_in_vault_count": int(
                knowledge_report.get("json_in_vault_count") or 0
            ),
            "asset_count": int(knowledge_report.get("asset_count") or 0),
            "asset_bytes": int(knowledge_report.get("asset_bytes") or 0),
            "ocr_worker": {
                **OCR_STATE,
                "queue_count": codex_ocr["pending_count"],
            },
            "preferences": quality_feedback.summary(),
            "trash_path": str(TRASH_DIR),
            "graph": graph,
            "curation": curation,
            "personal_context": personal_context.status(),
        },
    }


def launch_terminal(command: list[str], title: str) -> None:
    quoted_parts = []
    for part in command:
        escaped = part.replace("'", "''")
        quoted_parts.append(f"'{escaped}'")
    quoted = " ".join(quoted_parts)
    ps_command = f"$host.UI.RawUI.WindowTitle='{title}'; & {quoted}"
    subprocess.Popen(
        ["powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
        cwd=str(ROOT),
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "KnowledgeHub/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 128_000:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/status":
            self.send_json(system_status())
            return
        if parsed.path == "/api/archiver-login":
            self.send_json(archiver_login_status())
            return
        if parsed.path == "/api/archiver-qr":
            path = archiver_qr_path()
            state = archiver_login_status()
            if not state.get("qr_available") or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = path.read_bytes()
            content_type = "image/jpeg" if body.startswith(b"\xff\xd8\xff") else "image/png"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/jobs":
            jobs = sorted(load_jobs(), key=lambda item: item.get("created_at", ""), reverse=True)
            self.send_json({"jobs": enrich_jobs_with_knowledge(jobs[:50])})
            return
        if parsed.path == "/api/search":
            import knowledge_graph

            params = urllib.parse.parse_qs(parsed.query)
            query = params.get("q", [""])[0]
            scope = params.get("scope", ["all"])[0]
            self.send_json(
                {
                    "query": query,
                    "scope": scope,
                    "results": knowledge_graph.search(query, scope=scope),
                }
            )
            return
        if parsed.path == "/api/context":
            import personal_context

            params = urllib.parse.parse_qs(parsed.query)
            try:
                max_chars = int(
                    params.get(
                        "max_chars",
                        [str(personal_context.DEFAULT_MAX_CHARS)],
                    )[0]
                )
            except ValueError:
                self.send_json({"error": "max_chars must be an integer"}, 400)
                return
            self.send_json(
                personal_context.get_agent_context(max_chars=max_chars)
            )
            return
        if parsed.path == "/api/recall":
            import knowledge_graph

            params = urllib.parse.parse_qs(parsed.query)
            query = str(params.get("q", [""])[0]).strip()
            if not query:
                self.send_json({"error": "q is required"}, 400)
                return
            try:
                limit = int(params.get("limit", ["8"])[0])
            except ValueError:
                self.send_json({"error": "limit must be an integer"}, 400)
                return
            include_evidence = str(
                params.get("include_evidence", ["false"])[0]
            ).casefold() in {"1", "true", "yes", "on"}
            self.send_json(
                knowledge_graph.recall(
                    query,
                    limit=limit,
                    include_evidence=include_evidence,
                )
            )
            return
        path = parsed.path if parsed.path != "/" else "/index.html"
        candidate = (STATIC_DIR / path.lstrip("/")).resolve()
        if STATIC_DIR.resolve() not in candidate.parents or not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            payload = self.read_json()
            request_path = urllib.parse.urlparse(self.path).path.rstrip("/")
            if request_path == "/api/submit":
                self.handle_submit(payload)
                return
            if request_path == "/api/local-import":
                self.handle_local_import(payload)
                return
            if request_path == "/api/actions/open-vault":
                os.startfile(vault_path())  # type: ignore[attr-defined]
                self.send_json({"ok": True})
                return
            if request_path == "/api/actions/archiver-login":
                state = start_archiver_login()
                self.send_json({"ok": True, **state})
                return
            feedback_match = re.fullmatch(
                r"/api/jobs/([A-Za-z0-9_-]+)/feedback",
                urllib.parse.urlparse(self.path).path,
            )
            if feedback_match:
                result = apply_job_feedback(
                    feedback_match.group(1),
                    str(payload.get("label") or ""),
                )
                self.send_json(result)
                return
            if request_path == "/api/actions/invasive/set":
                enabled = bool(payload.get("enabled"))
                if enabled and payload.get("confirmation") != "我理解风险":
                    self.send_json({"error": "请输入“我理解风险”后再开启"}, 400)
                    return
                config = load_config()
                config["invasive_router_enabled"] = enabled
                config["invasive_confirmed_at"] = now_iso() if enabled else ""
                save_config(config)
                self.send_json({"ok": True, "enabled": enabled})
                return
            if request_path == "/api/actions/invasive/scan":
                config = load_config()
                if not config.get("invasive_router_enabled"):
                    self.send_json({"error": "侵入式路由尚未明确开启"}, 403)
                    return
                scan = ROUTER_DIR / "scripts" / "frida_route" / "run_frida_scan.py"
                launch_terminal([sys.executable, str(scan), "--seconds", "60"], "微信内容路由：60 秒只读扫描")
                self.send_json({"ok": True, "message": "60 秒扫描窗口已打开；若提示权限不足，请以管理员身份重试"})
                return
            self.send_json(
                {
                    "error": "未知接口",
                    "request_path": request_path,
                },
                404,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def handle_submit(self, payload: dict[str, Any]) -> None:
        urls = extract_urls(str(payload.get("text") or ""))
        if not urls:
            self.send_json({"error": "没有识别到 http 或 https 链接"}, 400)
            return
        route = str(payload.get("route") or "auto")
        if route not in {"auto", "share", "router", "archive"}:
            self.send_json({"error": "无效的处理方式"}, 400)
            return
        created: list[dict[str, Any]] = []
        with STORE_LOCK:
            jobs = load_jobs()
            for url in urls[:20]:
                job = {
                    "id": uuid.uuid4().hex[:12],
                    "url": url,
                    "kind": classify(url),
                    "route": route,
                    "source": str(payload.get("source") or "manual"),
                    "observed_at": str(payload.get("observed_at") or ""),
                    "subscribe": bool(payload.get("subscribe")),
                    "interval": int(payload.get("interval") or 360),
                    "since": str(payload.get("since") or datetime.now().date().isoformat()),
                    "status": "pending",
                    "message": "已进入处理队列",
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "output_file": "",
                    "warnings": [],
                }
                title_hint = str(payload.get("title") or "").strip()
                if title_hint and len(urls) == 1:
                    job["title"] = title_hint
                jobs.append(job)
                created.append(job)
                queue_log(job)
                WORK_QUEUE.put(job["id"])
            save_jobs(jobs)
        self.send_json({"ok": True, "jobs": created}, 202)

    def handle_local_import(self, payload: dict[str, Any]) -> None:
        raw_path = str(payload.get("path") or "").strip().strip('"')
        if not raw_path:
            self.send_json({"error": "请填写本地文件或文件夹路径"}, 400)
            return
        source = Path(raw_path).expanduser()
        if not source.is_absolute():
            self.send_json({"error": "请使用完整的绝对路径"}, 400)
            return
        if not source.exists():
            self.send_json({"error": f"路径不存在：{source}"}, 400)
            return
        import knowledge_schema

        corpus_namespace = str(
            payload.get("corpus_namespace") or knowledge_schema.PERSONAL_MEMORY
        ).strip()
        if corpus_namespace not in knowledge_schema.NAMESPACES - {
            knowledge_schema.SOURCE_ARCHIVE
        }:
            self.send_json({"error": "无效的语料域"}, 400)
            return
        job = {
            "id": uuid.uuid4().hex[:12],
            "url": str(source.resolve()),
            "path": str(source.resolve()),
            "kind": "local",
            "corpus_namespace": corpus_namespace,
            "route": "local",
            "subscribe": False,
            "interval": 0,
            "since": "",
            "status": "pending",
            "message": "本地资料已进入处理队列",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "output_file": "",
            "warnings": [],
            "title": f"本地导入：{source.name or source}",
        }
        with STORE_LOCK:
            jobs = load_jobs()
            jobs.append(job)
            save_jobs(jobs)
            WORK_QUEUE.put(job["id"])
        self.send_json({"ok": True, "job": job}, 202)


def validate_local_host(host: str) -> str:
    value = str(host or "").strip().casefold()
    if value not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "Personal Knowledge Hub is local-only; bind it to 127.0.0.1, "
            "localhost, or ::1."
        )
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="本地 Obsidian 个人知识入口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=load_config()["port"])
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        validate_local_host(args.host)
    except ValueError as exc:
        parser.error(str(exc))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(default_config())
    if not JOBS_PATH.exists():
        save_jobs([])

    threading.Thread(target=worker_loop, name="knowledge-worker", daemon=True).start()
    resume_jobs()
    resume_ocr_jobs()
    refresh_trash_learning(force=True)
    reconcile_empty_wechat_jobs()
    repair_wechat_job_titles()
    regenerate_knowledge_views()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"个人知识中枢已启动：{url}")
    print(f"Obsidian Vault：{vault_path()}")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
