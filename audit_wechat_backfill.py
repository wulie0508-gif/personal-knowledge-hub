#!/usr/bin/env python3
"""Audit full WeChat history coverage against manifests and Obsidian files."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import runtime_config

ARCHIVER_HOME = Path(
    os.environ.get(
        "WECHAT_MP_ARCHIVER_HOME",
        str(Path.home() / ".config" / "wechat-mp-obsidian-archiver"),
    )
)
CONFIG = ARCHIVER_HOME / "subscriptions.json"
WORK_DIR = CONFIG.parent / "work"
VAULT = runtime_config.configured_vault()
WECHAT_DIR = VAULT / "10_Sources" / "WeChat"
REPORT = (
    runtime_config.private_path("backfill-supervisor")
    / "coverage-audit.json"
)

SOURCE_RE = re.compile(r'^source_url:\s*"?([^"\r\n]+)"?\s*$', re.MULTILINE)
ACCOUNT_RE = re.compile(r'^account:\s*"?([^"\r\n]+)"?\s*$', re.MULTILINE)
LOCAL_IMAGE_RE = re.compile(r"!\[\[([^\]]+)\]\]")
REMOTE_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)]+)\)")


def sub_key(sub: dict) -> str:
    return sub.get("mpId") or sub.get("fakeid") or sub.get("accountName", "")


def read_notes() -> tuple[dict[str, list[Path]], dict[str, int], dict[Path, dict]]:
    by_url: dict[str, list[Path]] = defaultdict(list)
    by_account: Counter[str] = Counter()
    details: dict[Path, dict] = {}
    for note_path in WECHAT_DIR.rglob("*.md"):
        if note_path.name.startswith("_"):
            continue
        text = note_path.read_text(encoding="utf-8", errors="replace")
        source_match = SOURCE_RE.search(text)
        account_match = ACCOUNT_RE.search(text)
        source_url = source_match.group(1).strip() if source_match else ""
        account = account_match.group(1).strip() if account_match else ""
        if source_url:
            by_url[source_url].append(note_path)
        if account:
            by_account[account] += 1

        missing_assets = []
        for relative in LOCAL_IMAGE_RE.findall(text):
            asset_path = WECHAT_DIR / relative.replace("/", "\\")
            if not asset_path.exists():
                missing_assets.append(relative)
        remote_images = REMOTE_IMAGE_RE.findall(text)
        details[note_path] = {
            "sourceUrl": source_url,
            "account": account,
            "missingAssets": missing_assets,
            "remoteImages": remote_images,
        }
    return by_url, dict(by_account), details


def load_error_count(account: str) -> int:
    path = WECHAT_DIR / f"_errors-{account}.json"
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return len(payload) if isinstance(payload, list) else 1
    except (OSError, json.JSONDecodeError):
        return 1


def audit() -> dict:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    notes_by_url, note_counts, note_details = read_notes()
    accounts = []

    for sub in config.get("subscriptions", []):
        account = sub.get("accountName", "")
        manifest_path = WORK_DIR / sub_key(sub) / "manifest.json"
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists()
            else {"articles": []}
        )
        articles = manifest.get("articles", [])
        urls = [article.get("sourceUrl", "") for article in articles if article.get("sourceUrl")]
        missing_notes = [url for url in urls if url not in notes_by_url]
        duplicate_urls = [url for url in urls if len(notes_by_url.get(url, [])) > 1]

        missing_asset_refs = []
        remote_image_refs = []
        for url in urls:
            for note_path in notes_by_url.get(url, []):
                detail = note_details[note_path]
                missing_asset_refs.extend(
                    {"note": note_path.name, "asset": item}
                    for item in detail["missingAssets"]
                )
                remote_image_refs.extend(
                    {"note": note_path.name, "url": item}
                    for item in detail["remoteImages"]
                )

        backfill = sub.get("state", {}).get("backfill", {})
        last_export = backfill.get("lastExport") or {}
        account_result = {
            "account": account,
            "metadataComplete": bool(backfill.get("completed")),
            "nextPage": int(backfill.get("nextPage", 0) or 0),
            "totalPublishRecords": int(backfill.get("totalCount", 0) or 0),
            "manifestArticles": len(articles),
            "vaultNotesByAccount": int(note_counts.get(account, 0)),
            "coveredSourceUrls": len(urls) - len(missing_notes),
            "missingNotes": len(missing_notes),
            "missingNoteSamples": missing_notes[:20],
            "duplicateSourceUrls": len(duplicate_urls),
            "duplicateSamples": duplicate_urls[:20],
            "missingAssets": len(missing_asset_refs),
            "missingAssetSamples": missing_asset_refs[:20],
            "remoteImageRefs": len(remote_image_refs),
            "remoteImageSamples": remote_image_refs[:20],
            "exportErrors": int(last_export.get("errors", 0) or 0),
            "imageFailures": int(last_export.get("imageFailures", 0) or 0),
            "errorReportEntries": load_error_count(account),
        }
        account_result["passed"] = all(
            (
                account_result["metadataComplete"],
                account_result["manifestArticles"] > 0,
                account_result["missingNotes"] == 0,
                account_result["duplicateSourceUrls"] == 0,
                account_result["missingAssets"] == 0,
                account_result["remoteImageRefs"] == 0,
                account_result["exportErrors"] == 0,
                account_result["imageFailures"] == 0,
                account_result["errorReportEntries"] == 0,
            )
        )
        accounts.append(account_result)

    passed = len(accounts) == 12 and all(item["passed"] for item in accounts)
    return {
        "auditedAt": datetime.now().isoformat(timespec="seconds"),
        "passed": passed,
        "accountCount": len(accounts),
        "passedAccounts": sum(1 for item in accounts if item["passed"]),
        "totalManifestArticles": sum(item["manifestArticles"] for item in accounts),
        "totalCoveredSourceUrls": sum(item["coveredSourceUrls"] for item in accounts),
        "totalMissingNotes": sum(item["missingNotes"] for item in accounts),
        "totalMissingAssets": sum(item["missingAssets"] for item in accounts),
        "totalRemoteImageRefs": sum(item["remoteImageRefs"] for item in accounts),
        "accounts": accounts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=str(REPORT))
    args = parser.parse_args()
    result = audit()
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
