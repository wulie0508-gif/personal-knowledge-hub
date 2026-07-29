from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import corpus_identity_migration
import knowledge_graph
import personal_identity_migration
import runtime_config


def write_note(path: Path, source_path: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        'title: "Reviewed personal work"\n'
        'platform: "local"\n'
        f'source_path: "{source_path}"\n'
        'knowledge_priority: "重点"\n'
        'curation_status: "complete"\n'
        "---\n\n"
        "A user-authored project reflection.\n",
        encoding="utf-8",
    )


class PersonalIdentityMigrationTests(unittest.TestCase):
    def test_migration_is_dry_run_by_default_and_backs_up_before_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            note = (
                vault
                / "10_Sources"
                / "Local"
                / "Articles"
                / "personal.md"
            )
            source_root = root / "approved-personal"
            write_note(note, str(source_root / "portfolio.md"))
            original_runtime = os.environ.get(runtime_config.RUNTIME_HOME_ENV)
            os.environ[runtime_config.RUNTIME_HOME_ENV] = str(root / "runtime")
            try:
                dry_run = personal_identity_migration.migrate(
                    vault=vault,
                    source_roots=[str(source_root)],
                    source_prefixes=[],
                )
                self.assertEqual(dry_run["candidate_count"], 1)
                self.assertEqual(dry_run["changed_count"], 0)
                self.assertFalse(
                    knowledge_graph._field(
                        note.read_text(encoding="utf-8"),
                        "corpus_namespace",
                    )
                )

                applied = personal_identity_migration.migrate(
                    vault=vault,
                    source_roots=[str(source_root)],
                    source_prefixes=[],
                    apply=True,
                )
                text = note.read_text(encoding="utf-8")
                self.assertEqual(applied["changed_count"], 1)
                self.assertEqual(
                    knowledge_graph._field(text, "corpus_namespace"),
                    "personal_memory",
                )
                self.assertEqual(
                    knowledge_graph._field(text, "authorship"),
                    "self",
                )
                backups = list(Path(applied["backup_root"]).rglob("*.md"))
                self.assertEqual(len(backups), 1)
            finally:
                if original_runtime is None:
                    os.environ.pop(runtime_config.RUNTIME_HOME_ENV, None)
                else:
                    os.environ[runtime_config.RUNTIME_HOME_ENV] = original_runtime

    def test_ordered_rules_keep_external_reports_out_of_persona(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            article_root = vault / "10_Sources" / "Local" / "Articles"
            project_root = root / "project"
            external = article_root / "external.md"
            internal = article_root / "internal.md"
            write_note(external, str(project_root / "third-party.pdf"))
            write_note(internal, str(project_root / "reflection.md"))
            original_runtime = os.environ.get(runtime_config.RUNTIME_HOME_ENV)
            os.environ[runtime_config.RUNTIME_HOME_ENV] = str(root / "runtime")
            rules = [
                {
                    "id": "external_first",
                    "namespace": "professional_reference",
                    "source_suffixes": ["/third-party.pdf"],
                },
                {
                    "id": "personal_project",
                    "namespace": "personal_memory",
                    "source_prefixes": [str(project_root)],
                    "authorship": "self",
                    "persona_influence": 0.9,
                },
            ]
            try:
                result = corpus_identity_migration.migrate(
                    vault=vault,
                    rules=rules,
                    apply=True,
                )
                self.assertEqual(result["changed_count"], 2)
                self.assertEqual(
                    knowledge_graph._field(
                        external.read_text(encoding="utf-8"),
                        "corpus_namespace",
                    ),
                    "professional_reference",
                )
                self.assertEqual(
                    knowledge_graph._field(
                        internal.read_text(encoding="utf-8"),
                        "corpus_namespace",
                    ),
                    "personal_memory",
                )
            finally:
                if original_runtime is None:
                    os.environ.pop(runtime_config.RUNTIME_HOME_ENV, None)
                else:
                    os.environ[runtime_config.RUNTIME_HOME_ENV] = original_runtime


if __name__ == "__main__":
    unittest.main()
