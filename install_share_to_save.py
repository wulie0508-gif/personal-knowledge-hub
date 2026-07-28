#!/usr/bin/env python3
"""Install the pinned Share to Save release into the configured Obsidian vault."""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests
import runtime_config


VERSION = "5.4.4"
BASE = f"https://github.com/chenxiccc/Obsidian-Share-to-Save/releases/download/{VERSION}"
VAULT = runtime_config.configured_vault()
PLUGIN_DIR = VAULT / ".obsidian" / "plugins" / "share-to-save"


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(content)
    os.replace(temp, path)


def main() -> None:
    for name in ("main.js", "manifest.json", "styles.css"):
        response = requests.get(f"{BASE}/{name}", timeout=45)
        response.raise_for_status()
        atomic_write(PLUGIN_DIR / name, response.content)

    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("id") != "share-to-save" or manifest.get("version") != VERSION:
        raise RuntimeError("下载的插件清单校验失败")

    data_path = PLUGIN_DIR / "data.json"
    if not data_path.exists():
        data = {
            "outputFolder": "10_Sources/WeChat",
            "pollIntervalValue": 15,
            "pollIntervalUnit": "seconds",
            "timestampFormat": "h1",
            "timestampEnabled": True,
        }
        atomic_write(data_path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    enabled_path = VAULT / ".obsidian" / "community-plugins.json"
    enabled = []
    if enabled_path.exists():
        enabled = json.loads(enabled_path.read_text(encoding="utf-8"))
    if "share-to-save" not in enabled:
        enabled.append("share-to-save")
    atomic_write(enabled_path, (json.dumps(enabled, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(f"Share to Save {VERSION} 已安装并启用：{PLUGIN_DIR}")


if __name__ == "__main__":
    main()
