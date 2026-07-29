#!/usr/bin/env python3
"""Local-only entry point for computer agents to use the personal knowledge base."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import app
import knowledge_graph
import local_importer
import personal_context


def emit(value: Any) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def command_search(query: str, limit: int, scope: str) -> None:
    emit(
        {
            "query": query,
            "scope": scope,
            "results": knowledge_graph.search(
                query,
                limit=max(1, min(limit, 50)),
                scope=scope,
            ),
        }
    )


def command_context(max_chars: int) -> None:
    emit(personal_context.get_agent_context(max_chars=max_chars))


def command_recall(
    query: str,
    limit: int,
    include_evidence: bool,
) -> None:
    emit(
        knowledge_graph.recall(
            query,
            limit=limit,
            include_evidence=include_evidence,
        )
    )


def command_import(source: str) -> None:
    path = Path(source).expanduser()
    if not path.is_absolute():
        raise ValueError("请使用本地绝对路径")
    result = local_importer.import_path(str(path))
    result["knowledge"] = app.regenerate_knowledge_views()
    emit(result)


def command_rebuild() -> None:
    emit(app.regenerate_knowledge_views())


def command_status() -> None:
    status = app.system_status()
    emit(
        {
            "vault": status["vault"],
            "knowledge": status["knowledge"],
            "local_only": True,
            "network_api_required": False,
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="本机 Agent 调用个人知识库；不需要外部 API。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="搜索本地知识索引")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument(
        "--scope",
        choices=(
            "all",
            "knowledge",
            "archive",
            "personal",
            "professional",
            "enterprise",
            "authoritative",
        ),
        default="all",
        help="all=知识优先并在不足时回溯原文；archive=直接搜索全部来源库",
    )

    context_parser = subparsers.add_parser(
        "context",
        help="Return the compact, local-only AI personal context packet",
    )
    context_parser.add_argument(
        "--max-chars",
        type=int,
        default=personal_context.DEFAULT_MAX_CHARS,
    )

    recall_parser = subparsers.add_parser(
        "recall",
        help="Recall user-authored memory first, with optional external evidence",
    )
    recall_parser.add_argument("query")
    recall_parser.add_argument("--limit", type=int, default=8)
    recall_parser.add_argument(
        "--include-evidence",
        action="store_true",
        help="Retrieve separately-labelled external evidence after personal memory",
    )

    import_parser = subparsers.add_parser("import", help="导入本地文件或文件夹")
    import_parser.add_argument("path")

    subparsers.add_parser("rebuild", help="重建双链、主题页与本地索引")
    subparsers.add_parser("status", help="返回本地知识库状态")

    args = parser.parse_args()
    if args.command == "search":
        command_search(args.query, args.limit, args.scope)
    elif args.command == "context":
        command_context(args.max_chars)
    elif args.command == "recall":
        command_recall(
            args.query,
            args.limit,
            args.include_evidence,
        )
    elif args.command == "import":
        command_import(args.path)
    elif args.command == "rebuild":
        command_rebuild()
    else:
        command_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
