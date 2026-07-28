"""Explicit corpus identity rules shared by ingestion, graph and retrieval.

The system must never infer that an external article represents the user just
because it was imported locally. These helpers supply conservative defaults
for legacy notes while allowing new notes to declare their own identity.
"""

from __future__ import annotations

from typing import Any


PERSONAL_MEMORY = "personal_memory"
PROFESSIONAL_REFERENCE = "professional_reference"
ENTERPRISE_INTERNAL = "enterprise_internal"
AUTHORITATIVE_EXTERNAL = "authoritative_external"
SOURCE_ARCHIVE = "source_archive"

NAMESPACES = {
    PERSONAL_MEMORY,
    PROFESSIONAL_REFERENCE,
    ENTERPRISE_INTERNAL,
    AUTHORITATIVE_EXTERNAL,
    SOURCE_ARCHIVE,
}

DEFAULTS: dict[str, dict[str, Any]] = {
    PERSONAL_MEMORY: {
        "authorship": "self",
        "confidentiality": "private",
        "engagement_status": "read",
        "stance": "unreviewed",
        "persona_influence": 0.75,
    },
    PROFESSIONAL_REFERENCE: {
        "authorship": "external",
        "confidentiality": "public_external",
        "engagement_status": "unread",
        "stance": "unreviewed",
        "persona_influence": 0.0,
    },
    ENTERPRISE_INTERNAL: {
        "authorship": "enterprise",
        "confidentiality": "enterprise_internal",
        "engagement_status": "read",
        "stance": "unreviewed",
        "persona_influence": 0.0,
    },
    AUTHORITATIVE_EXTERNAL: {
        "authorship": "external",
        "confidentiality": "public_external",
        "engagement_status": "unread",
        "stance": "unreviewed",
        "persona_influence": 0.0,
    },
    SOURCE_ARCHIVE: {
        "authorship": "external",
        "confidentiality": "public_external",
        "engagement_status": "unread",
        "stance": "unreviewed",
        "persona_influence": 0.0,
    },
}


def normalize_namespace(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in NAMESPACES else ""


def infer_namespace(
    *,
    platform: str,
    path_text: str = "",
    explicit: Any = "",
    is_raw_evidence: bool = False,
) -> str:
    """Choose a safe default for legacy notes without claiming user ownership."""

    resolved = normalize_namespace(explicit)
    if resolved:
        return resolved
    normalized_path = path_text.replace("\\", "/").casefold()
    if is_raw_evidence:
        return SOURCE_ARCHIVE
    if "/enterprise/" in normalized_path or "/nex/" in normalized_path:
        return ENTERPRISE_INTERNAL
    if platform == "local":
        # Legacy local imports might be personal or external. Preserve current
        # behavior only as a migration default and require a later review.
        return PERSONAL_MEMORY
    if platform in {"wechat_mp", "xiaohongshu", "feishu"}:
        return PROFESSIONAL_REFERENCE
    return SOURCE_ARCHIVE


def identity_metadata(
    *,
    namespace: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    defaults = DEFAULTS[namespace]
    result: dict[str, Any] = {"corpus_namespace": namespace}
    for key, fallback in defaults.items():
        value = fields.get(key)
        result[key] = fallback if value in (None, "") else value
    try:
        result["persona_influence"] = max(0.0, min(1.0, float(result["persona_influence"])))
    except (TypeError, ValueError):
        result["persona_influence"] = float(defaults["persona_influence"])
    # External content may become a source for a personal note, but it is not
    # itself a personality signal until the user creates that personal note.
    if namespace != PERSONAL_MEMORY:
        result["persona_influence"] = 0.0
    return result


def namespace_scope(scope: str) -> tuple[str, ...] | None:
    """Map public API scopes to corpus namespaces; ``None`` means all."""

    value = str(scope or "all").strip().lower()
    aliases = {
        "personal": (PERSONAL_MEMORY,),
        "professional": (PROFESSIONAL_REFERENCE, AUTHORITATIVE_EXTERNAL),
        "enterprise": (ENTERPRISE_INTERNAL,),
        "archive": (SOURCE_ARCHIVE,),
        "authoritative": (AUTHORITATIVE_EXTERNAL,),
        "all": None,
        "knowledge": None,
    }
    if value not in aliases:
        raise ValueError("scope must be all, knowledge, archive, personal, professional, enterprise, or authoritative")
    return aliases[value]
