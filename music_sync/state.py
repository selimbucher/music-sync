"""Persistent sync state: identity mappings, last-known sets, journal.

The last-known set per collection is what makes a delete distinguishable from
an add. It is only ever written from *confirmed* post-apply state, never from
a plan -- see merge.py.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS identity (
    identity   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    apple_id   TEXT,
    spotify_id TEXT,
    label      TEXT,
    updated    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS identity_apple   ON identity(apple_id);
CREATE INDEX IF NOT EXISTS identity_spotify ON identity(spotify_id);

-- One row per (collection, identity) that was confirmed present on BOTH
-- sides at the end of a run.
CREATE TABLE IF NOT EXISTS last_known (
    collection TEXT NOT NULL,
    identity   TEXT NOT NULL,
    PRIMARY KEY (collection, identity)
);

-- Every collection: paired playlists and the library facets alike.
-- ``seeded`` gates bidirectional merging: until a collection has been seeded
-- one-way from the master, an empty last_known would make every far-side item
-- look like a fresh add and union the two libraries together.
CREATE TABLE IF NOT EXISTS pair (
    collection TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    label      TEXT NOT NULL,
    apple_id   TEXT,
    spotify_id TEXT,
    seeded     INTEGER NOT NULL DEFAULT 0,
    updated    REAL NOT NULL
);

-- Items we could not resolve on the far side. Re-tried with backoff rather
-- than every run, and NEVER treated as a deletion.
CREATE TABLE IF NOT EXISTS quarantine (
    collection TEXT NOT NULL,
    identity   TEXT NOT NULL,
    side       TEXT NOT NULL,
    label      TEXT,
    reason     TEXT,
    attempts   INTEGER NOT NULL DEFAULT 1,
    last_try   REAL NOT NULL,
    PRIMARY KEY (collection, identity, side)
);

CREATE TABLE IF NOT EXISTS journal (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    run        TEXT NOT NULL,
    collection TEXT,
    action     TEXT NOT NULL,
    side       TEXT,
    label      TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS journal_run ON journal(run);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Quarantined items are retried on a widening schedule, in seconds.
RETRY_BACKOFF = (0, 3600, 6 * 3600, 24 * 3600, 7 * 24 * 3600)


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN")
        try:
            yield
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    # -- identities ---------------------------------------------------------

    def remember_identity(
        self, identity: str, kind: str, side: str, native_id: str, label: str = ""
    ) -> None:
        col = "apple_id" if side == "apple" else "spotify_id"
        self.db.execute(
            f"""INSERT INTO identity (identity, kind, {col}, label, updated)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(identity) DO UPDATE SET
                    {col}=excluded.{col},
                    label=COALESCE(NULLIF(excluded.label,''), identity.label),
                    updated=excluded.updated""",
            (identity, kind, native_id, label, time.time()),
        )

    def identity_for(self, side: str, native_id: str) -> str | None:
        """Reverse lookup: this side's native id -> identity, if ever matched."""
        col = "apple_id" if side == "apple" else "spotify_id"
        row = self.db.execute(
            f"SELECT identity FROM identity WHERE {col}=?", (native_id,)
        ).fetchone()
        return row["identity"] if row else None

    def identities_for(self, side: str, native_ids: list[str]) -> dict[str, str]:
        col = "apple_id" if side == "apple" else "spotify_id"
        out: dict[str, str] = {}
        for i in range(0, len(native_ids), 500):
            chunk = native_ids[i:i + 500]
            q = f"SELECT {col} AS n, identity FROM identity WHERE {col} IN ({','.join('?' * len(chunk))})"
            for r in self.db.execute(q, chunk):
                out[r["n"]] = r["identity"]
        return out

    def native_id(self, identity: str, side: str) -> str | None:
        col = "apple_id" if side == "apple" else "spotify_id"
        row = self.db.execute(
            f"SELECT {col} AS v FROM identity WHERE identity=?", (identity,)
        ).fetchone()
        return row["v"] if row else None

    # -- last known ---------------------------------------------------------

    def last_known(self, collection: str) -> set[str]:
        return {
            r["identity"]
            for r in self.db.execute(
                "SELECT identity FROM last_known WHERE collection=?", (collection,)
            )
        }

    def set_last_known(self, collection: str, identities: set[str]) -> None:
        self.db.execute("DELETE FROM last_known WHERE collection=?", (collection,))
        self.db.executemany(
            "INSERT INTO last_known (collection, identity) VALUES (?, ?)",
            [(collection, i) for i in identities],
        )

    def forget_collection(self, collection: str) -> None:
        for table in ("last_known", "quarantine"):
            self.db.execute(f"DELETE FROM {table} WHERE collection=?", (collection,))
        self.db.execute("DELETE FROM pair WHERE collection=?", (collection,))

    # -- playlist pairing ---------------------------------------------------

    def pairs(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM pair"))

    def pair_for(self, side: str, native_id: str) -> sqlite3.Row | None:
        col = "apple_id" if side == "apple" else "spotify_id"
        return self.db.execute(
            f"SELECT * FROM pair WHERE {col}=?", (native_id,)
        ).fetchone()

    def save_pair(
        self,
        collection: str,
        kind: str,
        label: str,
        apple_id: str | None = None,
        spotify_id: str | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO pair (collection, kind, label, apple_id, spotify_id, updated)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(collection) DO UPDATE SET
                   label=excluded.label,
                   apple_id=COALESCE(excluded.apple_id, pair.apple_id),
                   spotify_id=COALESCE(excluded.spotify_id, pair.spotify_id),
                   updated=excluded.updated""",
            (collection, kind, label, apple_id, spotify_id, time.time()),
        )

    def is_seeded(self, collection: str) -> bool:
        row = self.db.execute(
            "SELECT seeded FROM pair WHERE collection=?", (collection,)
        ).fetchone()
        return bool(row and row["seeded"])

    def mark_seeded(self, collection: str) -> None:
        self.db.execute(
            "UPDATE pair SET seeded=1, updated=? WHERE collection=?",
            (time.time(), collection),
        )

    def any_seeded(self) -> bool:
        return bool(self.db.execute("SELECT 1 FROM pair WHERE seeded=1 LIMIT 1").fetchone())

    def unseeded(self) -> list[str]:
        return [
            r["collection"]
            for r in self.db.execute("SELECT collection FROM pair WHERE seeded=0")
        ]

    # -- quarantine ---------------------------------------------------------

    def quarantine(
        self, collection: str, identity: str, side: str, label: str, reason: str
    ) -> None:
        self.db.execute(
            """INSERT INTO quarantine
                   (collection, identity, side, label, reason, attempts, last_try)
               VALUES (?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(collection, identity, side) DO UPDATE SET
                   attempts=quarantine.attempts+1,
                   reason=excluded.reason,
                   last_try=excluded.last_try""",
            (collection, identity, side, label, reason, time.time()),
        )

    def clear_quarantine(self, collection: str, identity: str, side: str) -> None:
        self.db.execute(
            "DELETE FROM quarantine WHERE collection=? AND identity=? AND side=?",
            (collection, identity, side),
        )

    def is_suppressed(self, collection: str, identity: str, side: str) -> bool:
        """True while an unresolvable item is still inside its backoff window."""
        row = self.db.execute(
            "SELECT attempts, last_try FROM quarantine"
            " WHERE collection=? AND identity=? AND side=?",
            (collection, identity, side),
        ).fetchone()
        if not row:
            return False
        idx = min(row["attempts"], len(RETRY_BACKOFF) - 1)
        return (time.time() - row["last_try"]) < RETRY_BACKOFF[idx]

    def quarantined(self) -> list[sqlite3.Row]:
        return list(
            self.db.execute("SELECT * FROM quarantine ORDER BY attempts DESC, label")
        )

    # -- journal ------------------------------------------------------------

    def log(
        self,
        run: str,
        action: str,
        collection: str | None = None,
        side: str | None = None,
        label: str | None = None,
        detail: object = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO journal (ts, run, collection, action, side, label, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                run,
                collection,
                action,
                side,
                label,
                json.dumps(detail, default=str) if detail is not None else None,
            ),
        )

    def recent(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,)
            )
        )

    def prune_journal(self, keep_days: int = 90) -> None:
        self.db.execute(
            "DELETE FROM journal WHERE ts < ?", (time.time() - keep_days * 86400,)
        )

    # -- meta ---------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
