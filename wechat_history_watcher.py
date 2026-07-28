#!/usr/bin/env python3
"""Watch WeChat's Chromium history for newly viewed Official Account articles."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import runtime_config

ROOT = Path(__file__).resolve().parent
DATA_DIR = runtime_config.runtime_home()
STATE_PATH = DATA_DIR / "wechat-history-state.json"
JOBS_PATH = DATA_DIR / "jobs.json"
RADIUM_PROFILES = (
    Path.home()
    / "AppData"
    / "Roaming"
    / "Tencent"
    / "xwechat"
    / "radium"
    / "web"
    / "profiles"
)
DEFAULT_API = "http://127.0.0.1:8765/api/submit"


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


def canonical_article_url(value: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc.lower() != "mp.weixin.qq.com":
        return None
    path = parsed.path.rstrip("/")
    if not path.startswith("/s/") or len(path) <= 3:
        return None
    return urllib.parse.urlunsplit(("https", "mp.weixin.qq.com", path, "", ""))


def history_candidates() -> list[Path]:
    if not RADIUM_PROFILES.is_dir():
        return []
    candidates = [
        path / "History"
        for path in RADIUM_PROFILES.glob("multitab_*")
        if (path / "History").is_file()
    ]
    return sorted(candidates, key=lambda path: path.stat().st_mtime_ns, reverse=True)


def snapshot_history(source: Path) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="wx-history-"))
    target = temp_dir / "History"
    shutil.copy2(source, target)
    journal = source.with_name("History-journal")
    if journal.is_file():
        shutil.copy2(journal, temp_dir / "History-journal")
    return target


def read_history(source: Path, after: int) -> list[dict[str, Any]]:
    snapshot = snapshot_history(source)
    try:
        connection = sqlite3.connect(snapshot)
        try:
            rows = connection.execute(
                """
                SELECT url, title, last_visit_time
                FROM urls
                WHERE last_visit_time > ?
                  AND url LIKE 'https://mp.weixin.qq.com/s/%'
                ORDER BY last_visit_time ASC
                """,
                (after,),
            ).fetchall()
        finally:
            connection.close()
    finally:
        shutil.rmtree(snapshot.parent, ignore_errors=True)

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_url, title, visit_time in rows:
        url = canonical_article_url(str(raw_url))
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(
            {
                "url": url,
                "title": str(title or ""),
                "last_visit_time": int(visit_time),
            }
        )
    return result


def max_visit_time(source: Path) -> int:
    snapshot = snapshot_history(source)
    try:
        connection = sqlite3.connect(snapshot)
        try:
            row = connection.execute(
                "SELECT COALESCE(MAX(last_visit_time), 0) FROM urls"
            ).fetchone()
            return int(row[0] or 0)
        finally:
            connection.close()
    finally:
        shutil.rmtree(snapshot.parent, ignore_errors=True)


def existing_job_urls() -> set[str]:
    jobs = load_json(JOBS_PATH, [])
    result: set[str] = set()
    if not isinstance(jobs, list):
        return result
    for job in jobs:
        url = canonical_article_url(str((job or {}).get("url") or ""))
        if url:
            result.add(url)
    return result


def submit(url: str, api_url: str, title: str = "") -> None:
    payload = json.dumps(
        {
            "text": url,
            "route": "router",
            "source": "wechat_history",
            "title": title,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status not in {200, 202}:
            raise RuntimeError(f"knowledge hub returned HTTP {response.status}")


def default_state() -> dict[str, Any]:
    return {
        "initialized": False,
        "monitor_status": "starting",
        "last_error": "",
        "profile": "",
        "last_visit_time": 0,
        "last_scan_at": "",
        "detected_count": 0,
        "submitted_count": 0,
        "pending": [],
    }


def scan_once(state: dict[str, Any], api_url: str) -> dict[str, Any]:
    candidates = history_candidates()
    if not candidates:
        raise RuntimeError("没有找到微信内置浏览器的 History 文件")
    history = candidates[0]
    state["profile"] = str(history.parent)

    if not state.get("initialized"):
        state["last_visit_time"] = max_visit_time(history)
        state["initialized"] = True
        state["monitor_status"] = "ready"
        state["last_error"] = ""
        state["last_scan_at"] = now_iso()
        atomic_json(STATE_PATH, state)
        return state

    after = int(state.get("last_visit_time") or 0)
    records = read_history(history, after)
    if records:
        state["last_visit_time"] = max(
            int(item["last_visit_time"]) for item in records
        )
        state["detected_count"] = int(state.get("detected_count") or 0) + len(records)

    known = existing_job_urls()
    pending_by_url = {
        str(item.get("url")): item
        for item in state.get("pending") or []
        if item.get("url")
    }
    for record in records:
        if record["url"] not in known:
            pending_by_url[record["url"]] = record

    still_pending: list[dict[str, Any]] = []
    for record in pending_by_url.values():
        try:
            submit(record["url"], api_url, str(record.get("title") or ""))
            known.add(record["url"])
            state["submitted_count"] = int(state.get("submitted_count") or 0) + 1
        except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as exc:
            record["error"] = str(exc)
            still_pending.append(record)

    state["pending"] = still_pending
    state["monitor_status"] = "ready" if not still_pending else "attention"
    state["last_error"] = (
        str(still_pending[0].get("error") or "") if still_pending else ""
    )
    state["last_scan_at"] = now_iso()
    atomic_json(STATE_PATH, state)
    return state


def run(interval: int, api_url: str, once: bool) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state = default_state()
    state.update(load_json(STATE_PATH, {}))
    while True:
        try:
            state = scan_once(state, api_url)
        except Exception as exc:
            state["monitor_status"] = "error"
            state["last_error"] = str(exc)
            state["last_scan_at"] = now_iso()
            atomic_json(STATE_PATH, state)
        if once:
            return 0 if state.get("monitor_status") != "error" else 1
        time.sleep(max(5, interval))


def backfill_existing(limit: int, api_url: str) -> dict[str, Any]:
    candidates = history_candidates()
    if not candidates:
        raise RuntimeError("没有找到微信内置浏览器的 History 文件")
    history = candidates[0]
    records = list(reversed(read_history(history, 0)))
    known = existing_job_urls()
    initially_known = sum(1 for item in records if item["url"] in known)
    selected = [item for item in records if item["url"] not in known][:limit]
    submitted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for record in selected:
        try:
            submit(record["url"], api_url, str(record.get("title") or ""))
            submitted.append(record)
            known.add(record["url"])
        except Exception as exc:
            failed.append({**record, "error": str(exc)})
    return {
        "profile": str(history.parent),
        "article_count": len(records),
        "already_known": initially_known,
        "selected_count": len(selected),
        "submitted_count": len(submitted),
        "failed_count": len(failed),
        "submitted": submitted,
        "failed": failed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只读监控微信内置浏览器中新打开的公众号文章"
    )
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--api-url", default=DEFAULT_API)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--backfill",
        type=int,
        nargs="?",
        const=100,
        default=0,
        metavar="LIMIT",
        help="把现有公众号浏览历史中尚未归档的文章送入知识库",
    )
    args = parser.parse_args()
    if args.backfill:
        print(
            json.dumps(
                backfill_existing(max(1, args.backfill), args.api_url),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    return run(args.interval, args.api_url, args.once)


if __name__ == "__main__":
    raise SystemExit(main())
