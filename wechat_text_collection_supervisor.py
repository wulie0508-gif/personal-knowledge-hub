#!/usr/bin/env python3
"""Build the compact text-only WeChat collection and deep-read Tencent Research.

The four ordinary accounts that do not yet have enough local notes receive
only enough list pages to produce roughly thirty candidates. Tencent Research
Institute is the sole uncapped account and continues until its accessible
history is complete. No image download or OCR is performed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import knowledge_pipeline
import runtime_config


ROOT = Path(__file__).resolve().parent
ARCHIVER_ROOT = Path(
    os.environ.get(
        "WECHAT_MP_ARCHIVER_ROOT",
        str(Path.home() / ".codex" / "skills" / "wechat-mp-obsidian-archiver"),
    )
)
ARCHIVER = ARCHIVER_ROOT / "scripts" / "wechat_subscriptions.py"
ARCHIVER_HOME = Path(
    os.environ.get(
        "WECHAT_MP_ARCHIVER_HOME",
        str(Path.home() / ".config" / "wechat-mp-obsidian-archiver"),
    )
)
CONFIG = ARCHIVER_HOME / "subscriptions.json"
VAULT_WECHAT = runtime_config.configured_vault() / "10_Sources" / "WeChat"
SELECTOR = ROOT / "wechat_text_selection.py"
CURATION = ROOT / "codex_curation_queue.py"
STATUS = runtime_config.private_path("wechat-text-selection", "collection-status.json")
LOG = runtime_config.private_path("wechat-text-selection", "collection.log")
ORDINARY_NEEDING_SAMPLE = ["优设网", "孤独大脑", "字节跳动Seed", "谷雨数据"]
TENCENT = "腾讯研究院"


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_status(state: str, **extra: Any) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": now_text(), "state": state, **extra}
    temporary = STATUS.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(STATUS)


def append_log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_text()}] {message.rstrip()}\n")


def run(command: list[str], timeout: int | None = None) -> tuple[int, str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=False,
        timeout=timeout,
    )
    output = (
        (result.stdout or b"").decode("utf-8", errors="replace")
        + "\n"
        + (result.stderr or b"").decode("utf-8", errors="replace")
    ).strip()
    append_log(f"exit={result.returncode} command={command[1:]}\n{output[-12000:]}")
    return result.returncode, output


def note_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    if not VAULT_WECHAT.is_dir():
        return counts
    for path in VAULT_WECHAT.rglob("*.md"):
        if path.name.startswith("_") or "_assets" in path.parts:
            continue
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = "".join(handle.readline() for _ in range(100))
        account = str(knowledge_pipeline.frontmatter_field(head, "account") or "")
        source = str(
            knowledge_pipeline.frontmatter_field(head, "source_url")
            or knowledge_pipeline.frontmatter_field(head, "sourceUrl")
            or ""
        )
        if account and source:
            counts[account] = counts.get(account, 0) + 1
    return counts


def subscription_state(account: str) -> dict[str, Any]:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    for sub in config.get("subscriptions", []):
        if sub.get("accountName") == account:
            return dict(sub.get("state", {}).get("backfill") or {})
    raise RuntimeError(f"subscription not found: {account}")


def session_problem(output: str) -> bool:
    lowered = output.lower()
    return (
        "session expired" in lowered
        or "200003" in lowered
        or "需要重新登录" in output
    )


def backfill(account: str, pages: int) -> tuple[int, str]:
    return run(
        [
            sys.executable,
            str(ARCHIVER),
            "backfill",
            account,
            "--pages-per-run",
            str(pages),
            "--rounds",
            "1",
        ]
    )


def refresh_selection() -> None:
    run([sys.executable, str(SELECTOR), "--limit", "30"])


def main() -> None:
    write_status("starting", policy="text_only", ordinary_limit=30)
    sampled_ordinary = False
    for account in ORDINARY_NEEDING_SAMPLE:
        attempts = 0
        while note_counts().get(account, 0) < 30 and attempts < 3:
            sampled_ordinary = True
            write_status(
                "sampling_ordinary",
                account=account,
                attempt=attempts + 1,
                note_counts=note_counts(),
            )
            code, output = backfill(account, 2)
            if session_problem(output):
                write_status("needs_login", account=account, detail="公众号平台登录过期")
                return
            if code:
                append_log(f"ordinary sampling failed account={account}")
                time.sleep(120)
            attempts += 1
    if sampled_ordinary:
        refresh_selection()

    tencent_cycles = 0
    while True:
        state = subscription_state(TENCENT)
        if state.get("completed"):
            break
        write_status(
            "tencent_backfill",
            cycle=tencent_cycles + 1,
            next_page=int(state.get("nextPage") or 0),
            discovered=int(state.get("articlesDiscovered") or 0),
            note_counts=note_counts(),
        )
        code, output = backfill(TENCENT, 5)
        if session_problem(output):
            write_status("needs_login", account=TENCENT, detail="公众号平台登录过期")
            return
        if code:
            write_status(
                "tencent_retry",
                cycle=tencent_cycles + 1,
                detail=output[-1000:],
            )
            time.sleep(300)
        else:
            tencent_cycles += 1
            refresh_selection()
            time.sleep(90)

    refresh_selection()
    run([sys.executable, str(CURATION), "sync"])
    write_status(
        "collection_complete_curation_pending",
        note_counts=note_counts(),
        tencent_backfill=subscription_state(TENCENT),
    )


if __name__ == "__main__":
    main()
