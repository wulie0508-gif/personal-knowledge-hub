#!/usr/bin/env python3
"""Watch filehelper for the command phrase and import only new links."""

from __future__ import annotations

import argparse
import json
import msvcrt
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import runtime_config

ROOT = Path(__file__).resolve().parent
DATA_DIR = runtime_config.runtime_home()
LOG_PATH = DATA_DIR / "command-watcher.log"
LOCK_PATH = DATA_DIR / "command-watcher.lock"
STATE_PATH = DATA_DIR / "command-watcher-state.json"

ROUTER = Path(
    os.environ.get(
        "WECHAT_CONTENT_ROUTER_ROOT",
        str(Path.home() / ".codex" / "skills" / "wechat-content-router-windows"),
    )
)
SCRIPTS = ROUTER / "scripts"
FRIDA_DIR = SCRIPTS / "frida_route"
SCANNER = FRIDA_DIR / "run_frida_scan.py"
IMPORTER = FRIDA_DIR / "import_frida_links.py"
OUTPUT_DIR = FRIDA_DIR / "output"
CONFIG_PATH = SCRIPTS / "config.json"
BATCH_PATH = OUTPUT_DIR / "command_batch.json"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def log(message: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def update_monitor_health(status: str, error: str = "") -> None:
    state = load_json(STATE_PATH, {"initialized": False})
    state["monitor_status"] = status
    state["health_updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if error:
        state["last_error"] = error
    else:
        state.pop("last_error", None)
    save_json(STATE_PATH, state)


def acquire_lock():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def categorize(urls: set[str]) -> dict[str, list[str]]:
    result = {
        "xiaohongshu": [],
        "mp.weixin": [],
        "feishu": [],
        "kdocs": [],
        "other_interesting": [],
        "all_unique": sorted(urls),
    }
    for url in sorted(urls):
        low = url.lower()
        if "xiaohongshu" in low or "xhslink" in low:
            result["xiaohongshu"].append(url)
        elif "mp.weixin" in low:
            result["mp.weixin"].append(url)
        elif "feishu" in low:
            result["feishu"].append(url)
        elif "kdocs" in low:
            result["kdocs"].append(url)
        else:
            result["other_interesting"].append(url)
    return result


def run_once() -> int:
    config = load_json(CONFIG_PATH, {})
    workflow = config.get("workflow") or {}
    wechat = config.get("wechat") or {}
    phrase = workflow.get("trigger_phrase") or "存入知识库"
    chat = wechat.get("chat_username") or "filehelper"

    command = [
        sys.executable,
        str(SCANNER),
        "--seconds",
        "150",
        "--chat-username",
        chat,
        "--trigger-phrase",
        phrase,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=210,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        message = "微信扫描超过 210 秒，未读取到本轮消息。"
        log(f"{message} 下轮会自动重试。")
        update_monitor_health("error", message)
        return 1
    if completed.returncode != 0:
        message = f"微信扫描失败：{(completed.stderr or completed.stdout)[-800:]}"
        log(message)
        update_monitor_health("error", message)
        return 1

    filter_info = load_json(OUTPUT_DIR / "filter_info.json", {})
    if not filter_info.get("applied"):
        message = "无法严格确认 filehelper 会话，未读取其他聊天。"
        log(f"跳过导入：{message}")
        update_monitor_health("error", message)
        return 2
    update_monitor_health("ready")

    categorized = load_json(OUTPUT_DIR / "categorized_urls.json", {})
    current_urls = set(categorized.get("all_unique") or [])
    trigger_hits = load_json(OUTPUT_DIR / "trigger_hits.json", [])
    trigger_ids = {item.get("id") for item in trigger_hits if item.get("id")}

    state = load_json(
        STATE_PATH,
        {
            "initialized": False,
            "baseline_urls": [],
            "pending_urls": [],
            "processed_trigger_ids": [],
        },
    )
    if not state.get("initialized"):
        state.update(
            initialized=True,
            baseline_urls=sorted(current_urls),
            pending_urls=[],
            processed_trigger_ids=sorted(trigger_ids)[-500:],
        )
        save_json(STATE_PATH, state)
        log(f"基线已建立：忽略现有 {len(current_urls)} 条链接。口令={phrase}")
        return 0

    baseline = set(state.get("baseline_urls") or [])
    pending = set(state.get("pending_urls") or [])
    processed_triggers = set(state.get("processed_trigger_ids") or [])
    pending.update(current_urls - baseline)
    new_triggers = trigger_ids - processed_triggers

    if new_triggers and pending:
        save_json(BATCH_PATH, categorize(pending))
        imported = subprocess.run(
            [sys.executable, str(IMPORTER), "--input", str(BATCH_PATH), "--config", str(CONFIG_PATH)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if imported.returncode == 0:
            log(f"收到口令“{phrase}”：已处理 {len(pending)} 条新链接。")
            pending.clear()
        else:
            log(f"口令导入失败，保留待处理链接：{(imported.stderr or imported.stdout)[-800:]}")
    elif new_triggers:
        log(f"收到口令“{phrase}”，没有新链接。")

    state.update(
        baseline_urls=sorted(baseline | current_urls),
        pending_urls=sorted(pending),
        processed_trigger_ids=sorted(processed_triggers | trigger_ids)[-500:],
        last_scan=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    save_json(STATE_PATH, state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    lock = acquire_lock()
    if lock is None:
        return 0
    if args.once:
        return run_once()
    log("口令监控已启动：只扫描 filehelper，口令=存入知识库。")
    while True:
        started = time.monotonic()
        try:
            run_once()
        except Exception as error:
            log(f"监控异常：{error}")
        elapsed = time.monotonic() - started
        time.sleep(max(5, args.interval - elapsed))


if __name__ == "__main__":
    raise SystemExit(main())
