from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app
import knowledge_graph
import knowledge_schema
import personal_context
import runtime_config


def write_note(path: Path, frontmatter: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\n{frontmatter.strip()}\n---\n\n{body.strip()}\n",
        encoding="utf-8",
    )


class RuntimeArchitectureTests(unittest.TestCase):
    def test_http_service_rejects_non_loopback_bindings(self) -> None:
        self.assertEqual(app.validate_local_host("127.0.0.1"), "127.0.0.1")
        with self.assertRaises(ValueError):
            app.validate_local_host("0.0.0.0")

    def test_external_content_cannot_influence_persona(self) -> None:
        result = knowledge_schema.identity_metadata(
            namespace=knowledge_schema.PROFESSIONAL_REFERENCE,
            fields={"persona_influence": 1.0},
        )
        self.assertEqual(result["authorship"], "external")
        self.assertEqual(result["persona_influence"], 0.0)

    def test_runtime_home_uses_explicit_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            original = os.environ.get(runtime_config.RUNTIME_HOME_ENV)
            try:
                os.environ[runtime_config.RUNTIME_HOME_ENV] = temporary
                self.assertEqual(runtime_config.runtime_home(), Path(temporary))
                layout = runtime_config.ensure_runtime_layout()
                self.assertTrue(layout["indexes"].is_dir())
                self.assertTrue(layout["secrets"].is_dir())
            finally:
                if original is None:
                    os.environ.pop(runtime_config.RUNTIME_HOME_ENV, None)
                else:
                    os.environ[runtime_config.RUNTIME_HOME_ENV] = original

    def test_search_isolated_by_explicit_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            old_index = knowledge_graph.INDEX_PATH
            old_maturity = knowledge_graph.MATURITY_PATH
            knowledge_graph.INDEX_PATH = root / "knowledge.sqlite3"
            knowledge_graph.MATURITY_PATH = root / "maturity.json"
            try:
                write_note(
                    vault / "10_Sources" / "Local" / "Articles" / "personal.md",
                    """
title: 我的机器人产品判断
platform: local
corpus_namespace: personal_memory
authorship: self
knowledge_type: 观点见解
knowledge_priority: 重点
curation_status: complete
knowledge_value_score: 90
                    """,
                    "我倾向于先验证机器人产品的可维护性和真实用户价值。",
                )
                write_note(
                    vault / "10_Sources" / "WeChat" / "Articles" / "research.md",
                    """
title: 机器人产品研究框架
platform: wechat_mp
corpus_namespace: professional_reference
authorship: external
account: 外部研究机构
knowledge_type: 方法工具
knowledge_priority: 重点
curation_status: complete
knowledge_value_score: 88
                    """,
                    "外部研究提出机器人产品应验证维护成本、使用频率和用户价值。",
                )
                with (
                    mock.patch.object(
                        personal_context,
                        "CONTEXT_PATH",
                        root / "context" / "ai-context.json",
                    ),
                    mock.patch.object(
                        personal_context,
                        "WATCHER_STATE_PATH",
                        root / "wechat-history-state.json",
                    ),
                    mock.patch.object(
                        personal_context,
                        "PREFERENCE_PATH",
                        root / "quality-preferences.json",
                    ),
                ):
                    knowledge_graph.build(vault)
                personal = knowledge_graph.search("机器人 产品", scope="personal")
                professional = knowledge_graph.search("机器人 产品", scope="professional")
                self.assertEqual(personal[0]["identity"]["namespace"], "personal_memory")
                self.assertTrue(personal[0]["identity"]["represents_user"])
                self.assertEqual(
                    professional[0]["identity"]["namespace"],
                    "professional_reference",
                )
                self.assertFalse(professional[0]["identity"]["represents_user"])
            finally:
                knowledge_graph.INDEX_PATH = old_index
                knowledge_graph.MATURITY_PATH = old_maturity


if __name__ == "__main__":
    unittest.main()
