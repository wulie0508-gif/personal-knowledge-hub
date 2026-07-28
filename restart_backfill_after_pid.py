#!/usr/bin/env python3
"""Start the optimized backfill supervisor after an in-flight cycle exits."""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import runtime_config

ROOT = Path(__file__).resolve().parent
DATA_DIR = runtime_config.private_path("backfill-supervisor")
SUPERVISOR = ROOT / "wechat_backfill_supervisor.py"
TAKEOVER_STATUS = DATA_DIR / "takeover-status.json"
TAKEOVER_LOG = DATA_DIR / "takeover.log"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def process_alive(pid: int) -> bool:
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def write_status(state: str, **extra: object) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"updatedAt": now_text(), "state": state, **extra}
    temporary = TAKEOVER_STATUS.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(TAKEOVER_STATUS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait-pid", type=int, required=True)
    parser.add_argument("--pages-per-run", type=int, default=40)
    parser.add_argument("--poll-seconds", type=int, default=5)
    args = parser.parse_args()

    write_status(
        "waiting",
        waitPid=args.wait_pid,
        pagesPerRun=args.pages_per_run,
    )
    while process_alive(args.wait_pid):
        time.sleep(max(1, args.poll_seconds))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = TAKEOVER_LOG.open("ab")
    creation_flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NO_WINDOW
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(SUPERVISOR),
            "--pages-per-run",
            str(args.pages_per_run),
            "--min-free-gb",
            "60",
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        close_fds=True,
        creationflags=creation_flags,
    )
    write_status(
        "started",
        waitPid=args.wait_pid,
        supervisorPid=process.pid,
        pagesPerRun=args.pages_per_run,
    )


if __name__ == "__main__":
    main()
