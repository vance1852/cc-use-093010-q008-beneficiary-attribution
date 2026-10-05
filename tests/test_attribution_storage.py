from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from benefit_attribution.storage import connect, initialize, inspect_schema, transaction


class AttributionStorageTests(unittest.TestCase):
    def test_initialize_is_repeatable_and_complete(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            initialize(connection)
            initialize(connection)
            summary = inspect_schema(connection)
        finally:
            connection.close()
        self.assertEqual(summary["missing_tables"], [])
        self.assertEqual(summary["schema_version"], "1")
        self.assertTrue(summary["foreign_keys"])

    def test_foreign_key_blocks_orphan_policy_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "attr.sqlite3")
            try:
                initialize(connection)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO policy_versions(policy_id,version,title,effective_from,"
                        "canonical_json,content_sha256,published_by,published_at) "
                        "VALUES('p',1,'t','2026-01-01','{}',?, 'nobody','2026-01-01')",
                        ("a" * 64,),
                    )
            finally:
                connection.close()

    def test_transaction_rolls_back(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        try:
            initialize(connection)
            with self.assertRaises(RuntimeError):
                with transaction(connection, immediate=True):
                    connection.execute(
                        "INSERT INTO users(user_id,display_name,role) VALUES('u','u','auditor')"
                    )
                    raise RuntimeError("stop")
            count = connection.execute("SELECT count(*) FROM users").fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
