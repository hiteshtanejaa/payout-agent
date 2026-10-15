"""Storage behavior; run with `python -m unittest discover -s tests`.

PostgreSQL checks require an empty disposable database via TEST_DATABASE_URL.
They never use DATABASE_URL or application credentials for their connection.
"""
import concurrent.futures
import importlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import ledger


class LedgerBehavior:
    def test_intent_lifecycle_and_matching(self):
        pending = ledger.new_intent(merchant="Figma", amount=12000, category="software")
        self.assertEqual(pending["status"], "awaiting_approval")
        self.assertIsNone(ledger.find_open_intent("figma", 12000, 0.02))
        ledger.update_intent(pending["id"], status="cleared", approval={"approver": "finance"})
        self.assertEqual(ledger.find_open_intent(" FIGMA ", 12240, 0.02)["id"], pending["id"])
        self.assertIsNone(ledger.find_open_intent("figma", 12241, 0.02))
        self.assertIsNone(ledger.find_open_intent("other merchant", 12000, 0.02))
        ledger.update_intent(pending["id"], status="executed", authorization_id="ia_test")
        self.assertEqual(ledger.get_intent(pending["id"])["authorization_id"], "ia_test")
        self.assertEqual(ledger.list_intents("cleared"), [])
        self.assertIsNone(ledger.get_intent("missing"))
        with self.assertRaises(KeyError):
            ledger.update_intent("missing", status="cleared")

    def test_decision_order_and_monthly_total(self):
        records = [
            ledger.record_decision(stage="proposal", decision="approve", amount=1000,
                                   ts="2026-10-01T00:00:00+00:00"),
            ledger.record_decision(stage="authorization", decision="approve", amount=12000,
                                   ts="2026-10-02T00:00:00+00:00"),
            ledger.record_decision(stage="authorization", decision="decline", amount=500,
                                   ts="2026-10-03T00:00:00+00:00"),
            ledger.record_decision(stage="authorization", decision="approve", amount=300,
                                   ts="2026-09-30T23:59:59+00:00"),
            ledger.record_decision(stage="capture", decision="approve", amount=12000,
                                   ts="2026-10-04T00:00:00+00:00"),
        ]
        self.assertEqual(ledger.decisions(), records)
        self.assertEqual(ledger.month_spent(datetime(2026, 10, 15, tzinfo=timezone.utc)), 12000)

    def test_processed_inbox_remembers_name_and_content(self):
        self.assertFalse(ledger.inbox_processed("request.txt", "v1"))
        ledger.mark_inbox_processed("request.txt", "v1")
        ledger.mark_inbox_processed("request.txt", "v1")
        ledger.mark_inbox_processed("request.txt", "v2")
        self.assertTrue(ledger.inbox_processed("request.txt", "v1"))
        self.assertTrue(ledger.inbox_processed("request.txt", "v2"))
        self.assertFalse(ledger.inbox_processed("different.txt", "v1"))
        self.assertFalse(ledger.inbox_processed("request.txt", "v3"))

    def test_intents_preserve_creation_order_and_filter_status(self):
        first = ledger.new_intent(merchant="First", amount=100)
        second = ledger.new_intent(merchant="Second", amount=200, status="cleared")
        third = ledger.new_intent(merchant="Third", amount=300)
        ledger.update_intent(first["id"], description="updated later")
        self.assertEqual([it["id"] for it in ledger.list_intents()],
                         [first["id"], second["id"], third["id"]])
        self.assertEqual([it["id"] for it in ledger.list_intents("cleared")], [second["id"]])

    def test_concurrent_intent_creation_and_updates_preserve_records(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            intents = list(pool.map(lambda n: ledger.new_intent(merchant=f"Merchant {n}", amount=n), range(32)))
            target = intents[0]["id"]
            list(pool.map(lambda n: ledger.update_intent(target, **{f"field_{n}": n}), range(16)))
            list(pool.map(lambda n: ledger.record_decision(stage="test", number=n), range(32)))
        self.assertEqual(len(ledger.list_intents()), 32)
        self.assertEqual(len(ledger.decisions()), 32)
        updated = ledger.get_intent(target)
        for n in range(16):
            self.assertEqual(updated[f"field_{n}"], n)


class FileLedgerTests(LedgerBehavior, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"LEDGER_DIR": self.directory.name, "DATABASE_URL": ""})
        self.environment.start()
        importlib.reload(ledger)

    def tearDown(self):
        self.environment.stop()
        importlib.reload(ledger)
        self.directory.cleanup()

    def test_storage_survives_module_reload_and_uses_original_file_formats(self):
        intent = ledger.new_intent(merchant="Figma", amount=12000)
        decision = ledger.record_decision(stage="proposal", intent_id=intent["id"])
        ledger.mark_inbox_processed("request.txt", "hash")
        importlib.reload(ledger)
        self.assertEqual(ledger.get_intent(intent["id"]), intent)
        self.assertEqual(ledger.decisions(), [decision])
        self.assertTrue(ledger.inbox_processed("request.txt", "hash"))
        with open(ledger.INTENTS, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {intent["id"]: intent})
        with open(ledger.DECISIONS, encoding="utf-8") as f:
            self.assertEqual(json.loads(f.readline()), decision)

    def test_database_mode_never_silently_falls_back_to_files(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://unused.example/test"}), \
                patch.dict("sys.modules", {"psycopg": None}):
            with self.assertRaisesRegex(RuntimeError, "psycopg"):
                ledger.ensure_storage()
        self.assertEqual(os.listdir(self.directory.name), [])


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to an empty disposable PostgreSQL database")
class PostgresLedgerTests(LedgerBehavior, unittest.TestCase):
    def setUp(self):
        import psycopg
        self.environment = patch.dict(os.environ, {"DATABASE_URL": os.environ["TEST_DATABASE_URL"]})
        self.environment.start()
        importlib.reload(ledger)
        ledger.ensure_storage()
        self.connection = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        counts = [self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("treasurer_intents", "treasurer_decisions", "treasurer_processed_inbox")]
        if any(counts):
            self.connection.close()
            self.environment.stop()
            importlib.reload(ledger)
            self.fail("TEST_DATABASE_URL must point to an empty disposable database")

    def tearDown(self):
        self.connection.execute("TRUNCATE treasurer_intents, treasurer_decisions, treasurer_processed_inbox RESTART IDENTITY")
        self.connection.close()
        self.environment.stop()
        importlib.reload(ledger)

    def test_database_survives_module_reload(self):
        intent = ledger.new_intent(merchant="Figma", amount=12000)
        decision = ledger.record_decision(stage="proposal", intent_id=intent["id"])
        ledger.mark_inbox_processed("request.txt", "hash")
        importlib.reload(ledger)
        self.assertEqual(ledger.get_intent(intent["id"]), intent)
        self.assertEqual(ledger.decisions(), [decision])
        self.assertTrue(ledger.inbox_processed("request.txt", "hash"))


if __name__ == "__main__":
    unittest.main()
