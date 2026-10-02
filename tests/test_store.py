"""History pagination remains compatible with existing checkpoint databases."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from opslens.store import RunStore


class StoreHistoryTests(unittest.TestCase):
    def test_existing_database_is_readable_without_rewriting_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            original = {"run_id": "old", "revision": 4, "case_id": "case",
                        "case_fingerprint": "snapshot", "status": "paused"}
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE runs "
                                   "(run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL)")
                connection.execute("INSERT INTO runs VALUES (?, ?, ?)", ("old", 4, json.dumps(original)))
                connection.commit()
            store = RunStore(path)
            states, cursor = store.list_runs({"case": "snapshot"})
            self.assertEqual(states, [original])
            self.assertIsNone(cursor)
            self.assertEqual(store.load("old"), original)

    def test_checkpoint_updates_do_not_reorder_pages_and_scopes_are_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.sqlite3")
            states = []
            for index, fingerprint in enumerate(("same", "different", "same", "same")):
                state = {"run_id": str(index), "case_id": "case", "case_fingerprint": fingerprint}
                store.save(state)
                states.append(state)
            first, cursor = store.list_runs({"case": "same"}, limit=2)
            self.assertEqual([item["run_id"] for item in first], ["3", "2"])
            states[0]["status"] = "completed"
            store.save(states[0])
            store.save({"run_id": "new", "case_id": "case", "case_fingerprint": "same"})
            rest, end = store.list_runs({"case": "same"}, limit=2, before=cursor)
            self.assertEqual(rest, [states[0]])
            self.assertIsNone(end)
            self.assertEqual(store.list_runs({"unknown": "same"}), ([], None))
            self.assertEqual(store.list_runs({}), ([], None))


if __name__ == "__main__":
    unittest.main()
