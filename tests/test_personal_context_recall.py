from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import app
import knowledge_graph
import personal_context
import wechat_history_watcher


def write_note(path: Path, frontmatter: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\n{frontmatter.strip()}\n---\n\n{body.strip()}\n",
        encoding="utf-8",
    )


class PersonalContextRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.originals = {
            "index": knowledge_graph.INDEX_PATH,
            "maturity": knowledge_graph.MATURITY_PATH,
            "context": personal_context.CONTEXT_PATH,
            "watcher": personal_context.WATCHER_STATE_PATH,
            "preference": personal_context.PREFERENCE_PATH,
        }
        knowledge_graph.INDEX_PATH = self.root / "knowledge.sqlite3"
        knowledge_graph.MATURITY_PATH = self.root / "maturity.json"
        personal_context.CONTEXT_PATH = self.root / "context" / "ai-context.json"
        personal_context.WATCHER_STATE_PATH = self.root / "wechat-history-state.json"
        personal_context.PREFERENCE_PATH = self.root / "quality-preferences.json"

    def tearDown(self) -> None:
        knowledge_graph.INDEX_PATH = self.originals["index"]
        knowledge_graph.MATURITY_PATH = self.originals["maturity"]
        personal_context.CONTEXT_PATH = self.originals["context"]
        personal_context.WATCHER_STATE_PATH = self.originals["watcher"]
        personal_context.PREFERENCE_PATH = self.originals["preference"]
        self.temporary.cleanup()

    def seed_notes(self) -> None:
        write_note(
            self.vault
            / "10_Sources"
            / "Local"
            / "Articles"
            / "personal.md",
            """
title: 我对第二大脑的判断
platform: local
corpus_namespace: personal_memory
authorship: self
persona_influence: 1.0
category: 个人知识管理
knowledge_type: 观点见解
knowledge_priority: 重点
curation_status: complete
knowledge_value_score: 94
curated_at: 2026-07-28T09:00:00+08:00
            """,
            "第二大脑应该先让 AI 懂我，再按需检索外部证据，而不是堆积通用文章。",
        )
        write_note(
            self.vault
            / "10_Sources"
            / "Local"
            / "Articles"
            / "legacy-downloaded-report.md",
            """
title: 未声明作者的下载报告
platform: local
category: 个人知识管理
knowledge_type: 参考资料
knowledge_priority: 参考
curation_status: complete
knowledge_value_score: 82
curated_at: 2026-07-27T09:00:00+08:00
            """,
            "这份第二大脑报告是本地下载资料，不能因为文件在本机就代表用户。",
        )
        for index in range(12):
            write_note(
                self.vault
                / "10_Sources"
                / "WeChat"
                / "Articles"
                / f"research-{index}.md",
                f"""
title: 通用研究文章 {index}
platform: wechat_mp
corpus_namespace: professional_reference
authorship: external
persona_influence: 1.0
account: 外部研究机构
source_url: https://example.com/research-{index}
category: 行业研究
knowledge_type: 参考资料
knowledge_priority: 参考
curation_status: complete
knowledge_value_score: 80
publish_date: 2026-07-{index + 1:02d}
                """,
                "外部研究讨论第二大脑、知识管理和检索增强，但不代表用户本人观点。",
            )

    def test_hot_context_excludes_external_corpus_and_respects_budget(self) -> None:
        self.seed_notes()
        personal_context.WATCHER_STATE_PATH.write_text(
            json.dumps(
                {
                    "recent_observations": [
                        {
                            "url_hash": "synthetic",
                            "title": "我刚浏览的 Agent 记忆文章",
                            "observed_at": "2026-07-28T10:00:00+08:00",
                            "source": "wechat_history",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        report = knowledge_graph.build(self.vault)
        context = personal_context.get_agent_context(max_chars=2_600)
        rendered = json.dumps(context, ensure_ascii=False)

        self.assertIn("我对第二大脑的判断", rendered)
        self.assertNotIn("通用研究文章", rendered)
        self.assertNotIn("未声明作者的下载报告", rendered)
        self.assertIn("我刚浏览的 Agent 记忆文章", rendered)
        self.assertIn("never agreement", rendered)
        self.assertLessEqual(len(rendered), 2_600)
        self.assertNotIn(str(self.root), rendered)
        self.assertEqual(context["confirmed_self"]["note_count"], 1)
        self.assertTrue(Path(report["context_json_path"]).is_file())
        tiny = personal_context.get_agent_context(max_chars=1_500)
        self.assertLessEqual(
            len(json.dumps(tiny, ensure_ascii=False)),
            1_500,
        )

    def test_recall_is_personal_first_and_external_evidence_is_opt_in(self) -> None:
        self.seed_notes()
        knowledge_graph.build(self.vault)

        personal_only = knowledge_graph.recall(
            "我过去对第二大脑有什么判断",
            limit=4,
        )
        self.assertEqual(personal_only["intent"], "personal_recall")
        self.assertEqual(len(personal_only["memories"]), 1)
        self.assertNotEqual(
            personal_only["memories"][0]["title"],
            "未声明作者的下载报告",
        )
        self.assertEqual(personal_only["evidence"], [])
        self.assertTrue(
            personal_only["memories"][0]["identity"]["represents_user"]
        )
        self.assertEqual(
            personal_only["memories"][0]["citation"]["date"],
            "2026-07-28T09:00:00+08:00",
        )

        with_evidence = knowledge_graph.recall(
            "我过去对第二大脑有什么判断",
            limit=4,
            include_evidence=True,
        )
        self.assertTrue(with_evidence["evidence"])
        self.assertTrue(
            all(
                not item["identity"]["represents_user"]
                for item in with_evidence["evidence"]
            )
        )
        self.assertFalse(with_evidence["context_budget"]["archive_fallback"])

    def test_local_http_context_and_recall_contract(self) -> None:
        self.seed_notes()
        knowledge_graph.build(self.vault)
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(
                f"{origin}/api/context?max_chars=1800",
                timeout=5,
            ) as response:
                context = json.loads(response.read().decode("utf-8"))
            with urllib.request.urlopen(
                f"{origin}/api/recall?q=second%20brain&include_evidence=true",
                timeout=5,
            ) as response:
                recall = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

        self.assertEqual(context["schema_version"], 1)
        self.assertEqual(
            context["context_budget"]["applied_max_chars"],
            1800,
        )
        self.assertEqual(recall["intent"], "personal_recall")
        self.assertFalse(recall["context_budget"]["archive_fallback"])

    def test_browser_observation_does_not_store_url_or_imply_agreement(self) -> None:
        state = wechat_history_watcher.default_state()
        wechat_history_watcher.remember_observations(
            state,
            [
                {
                    "url": "https://mp.weixin.qq.com/s/private-example",
                    "title": "只代表打开过的页面",
                    "last_visit_time": 13_400_000_000_000_000,
                }
            ],
        )
        observation = state["recent_observations"][0]
        self.assertNotIn("url", observation)
        self.assertIn("url_hash", observation)
        self.assertEqual(observation["source"], "wechat_history")

        response = mock.MagicMock()
        response.status = 202
        response.__enter__.return_value = response
        with mock.patch.object(
            wechat_history_watcher.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            wechat_history_watcher.submit(
                "https://mp.weixin.qq.com/s/private-example",
                "http://127.0.0.1:8765/api/submit",
                "只代表打开过的页面",
                "2026-07-28T10:00:00+08:00",
            )
        submitted = json.loads(
            urlopen.call_args.args[0].data.decode("utf-8")
        )
        self.assertEqual(
            submitted["observed_at"],
            "2026-07-28T10:00:00+08:00",
        )
        self.assertEqual(submitted["source"], "wechat_history")

    def test_first_history_scan_only_establishes_a_baseline(self) -> None:
        state = wechat_history_watcher.default_state()
        fake_history = self.root / "profile" / "History"
        with (
            mock.patch.object(
                wechat_history_watcher,
                "history_candidates",
                return_value=[fake_history],
            ),
            mock.patch.object(
                wechat_history_watcher,
                "max_visit_time",
                return_value=123456,
            ),
            mock.patch.object(wechat_history_watcher, "atomic_json"),
        ):
            result = wechat_history_watcher.scan_once(
                state,
                "http://127.0.0.1:8765/api/submit",
            )
        self.assertTrue(result["initialized"])
        self.assertEqual(result["last_visit_time"], 123456)
        self.assertEqual(result["recent_observations"], [])
        self.assertEqual(result["detected_count"], 0)


if __name__ == "__main__":
    unittest.main()
