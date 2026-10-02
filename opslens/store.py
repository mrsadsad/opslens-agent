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
            connection.execute(
                "CREATE INDEX IF NOT EXISTS runs_case_scope ON runs "
                "(json_extract(state, '$.case_id'), json_extract(state, '$.case_fingerprint'))"
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

    def list_runs(self, case_fingerprints: dict[str, str], *, limit: int = 50,
                  before: int | None = None) -> tuple[list[dict], int | None]:
        """Read one newest-first page for exactly the supplied snapshots.

        The insertion rowid is stable across checkpoint updates, unlike revision
        or activity time. New runs cannot shift an already-issued older-page
        cursor. No state is changed, and only one bounded page is decoded.
        """
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("history limit must be 1..200")
        if before is not None and (type(before) is not int or not 1 <= before <= 2**63 - 1):
            raise ValueError("invalid history cursor")
        if not case_fingerprints:
            return [], None
        scopes = " OR ".join(
            "(json_extract(state, '$.case_id')=? AND json_extract(state, '$.case_fingerprint')=?)"
            for _ in case_fingerprints
        )
        parameters = [value for pair in case_fingerprints.items() for value in pair]
        query = f"SELECT rowid, state FROM runs WHERE ({scopes})"
        if before is not None:
            query += " AND rowid < ?"
            parameters.append(before)
        query += " ORDER BY rowid DESC LIMIT ?"
        parameters.append(limit + 1)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        next_before = rows[limit - 1][0] if len(rows) > limit else None
        return [json.loads(row[1]) for row in rows[:limit]], next_before
