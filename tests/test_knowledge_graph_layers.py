from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import codex_curation_queue
import knowledge_graph


def write_note(path: Path, frontmatter: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\n{frontmatter.strip()}\n---\n\n{body.strip()}\n",
        encoding="utf-8",
    )


class KnowledgeGraphLayerTests(unittest.TestCase):
    def test_unavailable_placeholder_is_not_curatable_content(self) -> None:
        self.assertTrue(codex_curation_queue.unavailable_body("未抓取到正文内容。"))
        self.assertTrue(codex_curation_queue.unavailable_body(" 正文内容暂不可用. "))
        self.assertTrue(
            codex_curation_queue.unavailable_body(
                "# 旧文章\n\n> [!info]\n> 只有元数据\n\n---\n\n未抓取到正文内容。"
            )
        )
        self.assertFalse(
            codex_curation_queue.unavailable_body(
                "文章讨论平台责任，但原始案例仍需进一步核验。"
            )
        )

    def test_multi_term_coverage_beats_single_term_tier_boost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            old_index = knowledge_graph.INDEX_PATH
            old_maturity = knowledge_graph.MATURITY_PATH
            knowledge_graph.INDEX_PATH = root / "knowledge.sqlite3"
            knowledge_graph.MATURITY_PATH = root / "knowledge-maturity.json"
            try:
                core = vault / "10_Sources" / "Local" / "Articles" / "核心但泛化.md"
                reference = vault / "10_Sources" / "Local" / "Articles" / "完整匹配.md"
                write_note(
                    core,
                    """
title: 海外投资概览
platform: local
knowledge_type: 观点见解
knowledge_priority: 重点
curation_status: complete
knowledge_value_score: 90
curated_at: 2026-07-26T10:00:00+08:00
                    """,
                    "海外投资需要长期积累。",
                )
                write_note(
                    reference,
                    """
title: 东道国审查与正当程序
platform: local
knowledge_type: 参考资料
knowledge_priority: 参考
curation_status: complete
knowledge_value_score: 72
curated_at: 2026-07-26T11:00:00+08:00
                    """,
                    "海外投资应识别东道国审查机制，并把正当程序纳入争议策略。",
                )

                knowledge_graph.build(vault)
                results = knowledge_graph.search(
                    "海外投资 东道国审查 正当程序",
                    limit=2,
                    scope="knowledge",
                )

                self.assertEqual(results[0]["title"], "东道国审查与正当程序")
                self.assertGreaterEqual(results[0]["matched_term_count"], 5)
            finally:
                knowledge_graph.INDEX_PATH = old_index
                knowledge_graph.MATURITY_PATH = old_maturity

    def test_maturity_gate_pauses_only_existing_tencent_backlog(self) -> None:
        cutoff = "2026-07-26T17:00:00+08:00"
        self.assertTrue(
            codex_curation_queue.paused_by_maturity(
                {
                    "account": "腾讯研究院",
                    "created_at": "2026-07-26T12:00:00+08:00",
                },
                cutoff,
            )
        )
        self.assertFalse(
            codex_curation_queue.paused_by_maturity(
                {
                    "account": "腾讯研究院",
                    "created_at": "2026-07-27T12:00:00+08:00",
                },
                cutoff,
            )
        )
        self.assertFalse(
            codex_curation_queue.paused_by_maturity(
                {
                    "account": "其他公众号",
                    "created_at": "2026-07-26T12:00:00+08:00",
                },
                cutoff,
            )
        )

    def test_reference_library_mode_has_a_finite_bulk_read_limit(self) -> None:
        self.assertTrue(knowledge_graph.REFERENCE_LIBRARY_MODE)
        self.assertEqual(knowledge_graph.MIN_DEEP_CURATED_FOR_STOP, 300)

    def test_raw_evidence_stays_out_of_graph_but_remains_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            old_index = knowledge_graph.INDEX_PATH
            old_maturity = knowledge_graph.MATURITY_PATH
            knowledge_graph.INDEX_PATH = root / "knowledge.sqlite3"
            knowledge_graph.MATURITY_PATH = root / "knowledge-maturity.json"
            try:
                personal = vault / "10_Sources" / "Local" / "Articles" / "个人判断.md"
                core = vault / "10_Sources" / "WeChat" / "Articles" / "重点方法.md"
                raw = vault / "10_Sources" / "WeChat" / "原始证据.md"
                write_note(
                    personal,
                    """
title: 个人的智能体记忆判断
platform: local
category: AI与知识管理
knowledge_type: 观点见解
knowledge_priority: 重点
curation_status: complete
knowledge_value_score: 92
curated_at: 2026-07-26T10:00:00+08:00
                    """,
                    "智能体记忆应先服从个人项目目标，再调用外部证据。",
                )
                write_note(
                    core,
                    """
title: 智能体记忆的分层方法
platform: wechat_mp
account: 测试研究院
source_url: https://mp.weixin.qq.com/s/core-example
category: AI与知识管理
knowledge_type: 方法工具
knowledge_priority: 重点
curation_status: complete
knowledge_value_score: 84
curated_at: 2026-07-26T11:00:00+08:00
                    """,
                    """
<!-- knowledge-value:start -->
## Codex 知识整理
- 摘要：把智能体记忆拆成个人目标、核心方法与原文证据三层。
- 关键点：
  - 图谱只承载可复用结论。
  - 原文通过检索按需回溯。
<!-- knowledge-value:end -->

智能体记忆需要分层检索。
                    """,
                )
                write_note(
                    raw,
                    """
title: 智能体记忆原始长文
platform: wechat_mp
account: 测试研究院
source_url: https://mp.weixin.qq.com/s/raw-example
article_slug: raw-example
knowledge_scope: selected
curation_status: pending
publish_date: 2024-01-01
                    """,
                    """
智能体记忆的原始证据正文。

<!-- knowledge-links:start -->
## 知识关联
- [[过期关系]]
<!-- knowledge-links:end -->
                    """,
                )

                report = knowledge_graph.build(vault)

                self.assertEqual(report["article_count"], 2)
                self.assertEqual(report["core_article_count"], 2)
                self.assertEqual(report["archive_count"], 3)
                self.assertNotIn(
                    knowledge_graph.START_MARKER,
                    raw.read_text(encoding="utf-8"),
                )

                graph_config = json.loads(
                    (vault / ".obsidian" / "graph.json").read_text(encoding="utf-8")
                )
                self.assertTrue(graph_config["search"])
                self.assertFalse(graph_config["showOrphans"])

                results = knowledge_graph.search("智能体记忆", limit=3)
                self.assertEqual([item["tier"] for item in results[:2]], ["personal", "core"])
                self.assertEqual(results[2]["tier"], "evidence")
                self.assertTrue(knowledge_graph.MATURITY_PATH.is_file())
                self.assertTrue(
                    (
                        vault
                        / "20_Knowledge"
                        / "AI上下文"
                        / "语料熔炼与停采标准.md"
                    ).is_file()
                )
            finally:
                knowledge_graph.INDEX_PATH = old_index
                knowledge_graph.MATURITY_PATH = old_maturity


if __name__ == "__main__":
    unittest.main()
