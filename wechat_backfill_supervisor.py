#!/usr/bin/env python3
"""Keep the WeChat historical archive moving until every account is complete.

The underlying archiver commits its list cursor after every page. This
supervisor only adds safe retries, cooldowns, a single-instance lock, and
durable logs so a large personal backfill can run unattended.
"""

from __future__ import annotations

import argparse
import json
import os
import msvcrt
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import runtime_config

ARCHIVER_ROOT = Path(
    os.environ.get(
        "WECHAT_MP_ARCHIVER_ROOT",
        str(Path.home() / ".codex" / "skills" / "wechat-mp-obsidian-archiver"),
    )
)
ARCHIVER = ARCHIVER_ROOT / "scripts" / "wechat_subscriptions.py"
AUDITOR = Path(__file__).resolve().parent / "audit_wechat_backfill.py"
PROJECT_ROOT = Path(__file__).resolve().parent
CURATION_QUEUE = PROJECT_ROOT / "codex_curation_queue.py"
AUTO_TRIAGE = PROJECT_ROOT / "auto_triage_curation.py"
ARCHIVER_HOME = Path(
    os.environ.get(
        "WECHAT_MP_ARCHIVER_HOME",
        str(Path.home() / ".config" / "wechat-mp-obsidian-archiver"),
    )
)
CONFIG = ARCHIVER_HOME / "subscriptions.json"
DATA_DIR = runtime_config.private_path("backfill-supervisor")
LOG_PATH = DATA_DIR / "supervisor.log"
STATUS_PATH = DATA_DIR / "status.json"
LOCK_PATH = DATA_DIR / "supervisor.lock"
STORAGE_ROOT = Path(r"C:\\")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def append_log(message: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message.rstrip()}\n")


def read_progress() -> dict:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    accounts = []
    for sub in config.get("subscriptions", []):
        backfill = sub.get("state", {}).get("backfill", {})
        accounts.append(
            {
                "account": sub.get("accountName", ""),
                "nextPage": int(backfill.get("nextPage", 0) or 0),
                "totalCount": int(backfill.get("totalCount", 0) or 0),
                "articlesAdded": int(backfill.get("articlesAdded", 0) or 0),
                "completed": bool(backfill.get("completed")),
                "lastError": backfill.get("lastError"),
                "lastExport": backfill.get("lastExport"),
            }
        )
    disk = shutil.disk_usage(STORAGE_ROOT)
    return {
        "updatedAt": now_iso(),
        "completed": sum(1 for row in accounts if row["completed"]),
        "total": len(accounts),
        "allCompleted": bool(accounts) and all(row["completed"] for row in accounts),
        "storage": {
            "freeGB": round(disk.free / (1024**3), 2),
            "usedGB": round(disk.used / (1024**3), 2),
        },
        "accounts": accounts,
    }


def save_status(progress: dict, state: str, detail: str = "") -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {**progress, "state": state, "detail": detail}
    STATUS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def acquire_lock():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+b")
    handle.seek(0)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as error:
        raise SystemExit("another WeChat backfill supervisor is already running") from error
    return handle


def run_cycle(pages_per_run: int) -> tuple[int, str]:
    command = [
        sys.executable,
        str(ARCHIVER),
        "backfill",
        "--pages-per-run",
        str(pages_per_run),
        "--rounds",
        "1",
    ]
    result = subprocess.run(command, capture_output=True, text=False)
    output = (
        (result.stdout or b"").decode("utf-8", errors="replace")
        + "\n"
        + (result.stderr or b"").decode("utf-8", errors="replace")
    ).strip()
    append_log(f"cycle exit={result.returncode}\n{output[-12000:]}")
    return result.returncode, output


def run_audit() -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, str(AUDITOR)],
        capture_output=True,
        text=False,
    )
    output = (
        (result.stdout or b"").decode("utf-8", errors="replace")
        + "\n"
        + (result.stderr or b"").decode("utf-8", errors="replace")
    ).strip()
    append_log(f"coverage audit exit={result.returncode}\n{output[-12000:]}")
    return result.returncode == 0, output


def run_post_cycle() -> None:
    commands = [
        [sys.executable, str(CURATION_QUEUE), "sync"],
        [
            sys.executable,
            str(AUTO_TRIAGE),
            "--limit",
            "5000",
            "--summary-only",
        ],
        [
            sys.executable,
            "-c",
            "import app; app.regenerate_knowledge_views()",
        ],
    ]
    for command in commands:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=False,
        )
        output = (
            (result.stdout or b"").decode("utf-8", errors="replace")
            + "\n"
            + (result.stderr or b"").decode("utf-8", errors="replace")
        ).strip()
        append_log(
            f"post-cycle exit={result.returncode} command={command[1:]}\n"
            f"{output[-8000:]}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pages-per-run",
        type=int,
        default=40,
        help=(
            "list pages committed per account before export; partial exports "
            "use a chunk manifest and the final pass verifies the full manifest"
        ),
    )
    parser.add_argument("--normal-cooldown", type=int, default=60)
    parser.add_argument("--rate-limit-cooldown", type=int, default=1800)
    parser.add_argument("--error-cooldown", type=int, default=300)
    parser.add_argument("--min-free-gb", type=float, default=60.0)
    parser.add_argument("--max-cycles", type=int, default=0, help="0 means unlimited")
    args = parser.parse_args()
    if args.pages_per_run < 1:
        raise SystemExit("--pages-per-run must be at least 1")

    lock_handle = acquire_lock()
    append_log(f"supervisor started pages_per_run={args.pages_per_run}")
    cycles = 0
    try:
        while not args.max_cycles or cycles < args.max_cycles:
            progress = read_progress()
            if progress["storage"]["freeGB"] < args.min_free_gb:
                save_status(
                    progress,
                    "storage_low",
                    f"free space is below {args.min_free_gb:.1f} GB",
                )
                append_log("stopped: storage safety threshold reached")
                raise SystemExit(3)
            if progress["allCompleted"]:
                audit_passed, _ = run_audit()
                if audit_passed:
                    save_status(
                        progress,
                        "complete",
                        "all histories and Obsidian coverage checks passed",
                    )
                    append_log("all accounts and coverage checks complete")
                    return
                save_status(
                    progress,
                    "repairing_coverage",
                    "metadata is complete but vault coverage still needs repair",
                )

            save_status(progress, "running", f"starting cycle {cycles + 1}")
            returncode, output = run_cycle(args.pages_per_run)
            run_post_cycle()
            cycles += 1
            progress = read_progress()

            lowered = output.lower()
            session_expired = "session expired" in lowered or "200003" in lowered
            rate_limited = "rate limit" in lowered or "freq control" in lowered or "200013" in lowered
            if session_expired:
                save_status(progress, "needs_login", "mp.weixin.qq.com session expired")
                append_log("stopped: platform login expired")
                raise SystemExit(2)
            if progress["allCompleted"]:
                audit_passed, _ = run_audit()
                if audit_passed:
                    save_status(
                        progress,
                        "complete",
                        "all histories and Obsidian coverage checks passed",
                    )
                    append_log("all accounts and coverage checks complete")
                    return
                save_status(
                    progress,
                    "repairing_coverage",
                    "metadata is complete but vault coverage still needs repair",
                )

            if rate_limited:
                cooldown = args.rate_limit_cooldown
                state = "cooldown_rate_limit"
            elif returncode:
                cooldown = args.error_cooldown
                state = "cooldown_error"
            else:
                cooldown = args.normal_cooldown
                state = "cooldown"
            save_status(progress, state, f"next retry in {cooldown} seconds")
            time.sleep(max(1, cooldown))
    except KeyboardInterrupt:
        progress = read_progress()
        save_status(progress, "stopped", "interrupted")
        append_log("interrupted")
    finally:
        lock_handle.close()


if __name__ == "__main__":
    main()
