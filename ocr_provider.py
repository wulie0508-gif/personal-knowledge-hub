"""Codex-first OCR marker with PaddleOCR as an explicit fallback."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


OCR_SCRIPT_DIR = (
    Path.home()
    / ".codex"
    / "skills"
    / "wechat-content-router-windows"
    / "scripts"
)
LOCAL_ENGINE: Any = None


@dataclass
class OCRResult:
    text: str = ""
    status: str = "pending_codex"
    provider: str = "codex_vision"
    error: str = ""


def provider_status() -> dict[str, Any]:
    local_fallback = os.getenv("KNOWLEDGE_OCR_LOCAL_FALLBACK", "1").lower() in {
        "1",
        "true",
        "yes",
    }
    return {
        "mode": "codex_first",
        "provider": "codex_vision",
        "model_available": True,
        "local_fallback_enabled": local_fallback,
        "ready": True,
        "message": "图片先进入 Codex OCR 队列；本地 PaddleOCR 仅作失败兜底",
    }


def recognize(paths: list[Path]) -> list[OCRResult]:
    """Mark images for the Codex heartbeat; do not run local OCR inline."""
    return [OCRResult() for _ in paths]


def local_recognize(paths: list[Path]) -> list[OCRResult]:
    """Explicit fallback used only after Codex cannot process a queued image."""
    global LOCAL_ENGINE
    sys.path.insert(0, str(OCR_SCRIPT_DIR))
    import ocr_paddle

    if LOCAL_ENGINE is None:
        LOCAL_ENGINE = ocr_paddle.load_ocr()
    predictions = list(LOCAL_ENGINE.predict([str(path) for path in paths]))
    results: list[OCRResult] = []
    for prediction in predictions:
        lines: list[str] = []
        values = prediction if isinstance(prediction, list) else [prediction]
        for item in values:
            parsed = ocr_paddle.normalize_result_item(item)
            value = str(parsed.get("text") or "").strip()
            if value:
                lines.append(value)
        text = "\n".join(lines).strip()
        results.append(
            OCRResult(
                text=text,
                status="success" if text else "empty",
                provider="paddle",
            )
        )
    return results
