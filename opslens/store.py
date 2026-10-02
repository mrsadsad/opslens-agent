"""Atomic SQLite checkpoints; no external database required for the first milestone."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class CheckpointConflict(RuntimeError):
    pass


class RunStore:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS runs "
                "(run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, state: dict) -> None:
        old = state.get("revision", -1)
        snapshot = {**state, "revision": old + 1}
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        with self._connect() as connection:
            if old == -1:
                try:
                    connection.execute("INSERT INTO runs VALUES (?, ?, ?)",
                                       (state["run_id"], 0, encoded))
                except sqlite3.IntegrityError as error:
                    raise CheckpointConflict("run already exists") from error
            else:
                result = connection.execute(
                    "UPDATE runs SET revision=?, state=? WHERE run_id=? AND revision=?",
                    (old + 1, encoded, state["run_id"], old),
                )
                if result.rowcount != 1:
                    raise CheckpointConflict("checkpoint changed; concurrent runners are unsupported")
        state["revision"] = old + 1

    def load(self, run_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        return json.loads(row[0])
