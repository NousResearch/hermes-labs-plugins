"""Plugin-owned observations; native stores are never opened here."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
import time


class InboxStateError(Exception):
    """Unavailable local state or invalid operation; never interpret as all-clear."""


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS observations (seq INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL, item_id TEXT NOT NULL, fingerprint TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, observed_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS observations_profile_seq ON observations(profile, seq)",
    "CREATE TABLE IF NOT EXISTS latest (profile TEXT NOT NULL, item_id TEXT NOT NULL, fingerprint TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY(profile,item_id))",
    "CREATE TABLE IF NOT EXISTS cursors (profile TEXT NOT NULL, consumer TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY(profile,consumer))",
)


class InboxState:
    def __init__(self, home: Path, profile: str):
        if not isinstance(home, (str, Path)):
            raise InboxStateError("home must be a path")
        if not isinstance(profile, str) or not profile.strip():
            raise InboxStateError("profile must be a non-empty string")
        self.home = Path(home).expanduser().resolve()
        self.profile = profile
        self.db_path = self.home / "inbox" / "state.db"

    @staticmethod
    def _validate_consumer(consumer):
        if not isinstance(consumer, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", consumer):
            raise InboxStateError("invalid consumer")

    @contextmanager
    def _connection(self, write=False):
        conn = None
        try:
            if write:
                if self.db_path.is_symlink() or self.db_path.parent.is_symlink():
                    raise InboxStateError("refusing symlinked local state")
                self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
                conn.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    conn.execute(statement)
            else:
                conn = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            if not write:
                conn.execute("BEGIN")  # consistent cursor/latest/page snapshot
            yield conn
            if write:
                conn.commit()
        except (sqlite3.Error, OSError) as exc:
            if conn is not None and write:
                conn.rollback()
            raise InboxStateError("local state unavailable: " + type(exc).__name__) from exc
        except Exception:
            if conn is not None and write:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    def observe(self, items: list[dict]) -> list[int]:
        if not isinstance(items, list):
            raise InboxStateError("items must be a list")
        prepared = {}
        for item in items:
            if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip()
                                                for k in ("id", "fingerprint", "kind", "state")):
                raise InboxStateError("observation requires non-empty id/fingerprint/kind/state")
            safe = {k: item[k] for k in ("id", "fingerprint", "kind", "state")}
            if safe["id"] in prepared and prepared[safe["id"]] != safe:
                raise InboxStateError("conflicting observations for one item in a batch")
            prepared[safe["id"]] = safe
        if not prepared:
            return []
        created = []
        with self._connection(write=True) as conn:
            for item in prepared.values():
                values = (item["fingerprint"], item["kind"], item["state"])
                old = conn.execute("SELECT fingerprint,kind,state FROM latest WHERE profile=? AND item_id=?",
                                   (self.profile, item["id"])).fetchone()
                if old is not None and tuple(old) == values:
                    continue
                seq = conn.execute("INSERT INTO observations(profile,item_id,fingerprint,kind,state,observed_at) VALUES (?,?,?,?,?,?)",
                                   (self.profile, item["id"], *values, time.time())).lastrowid
                conn.execute("INSERT INTO latest(profile,item_id,fingerprint,kind,state,seq) VALUES (?,?,?,?,?,?) "
                             "ON CONFLICT(profile,item_id) DO UPDATE SET fingerprint=excluded.fingerprint,kind=excluded.kind,state=excluded.state,seq=excluded.seq",
                             (self.profile, item["id"], *values, seq))
                created.append(seq)
        return created

    def changes(self, consumer="owner", limit=100) -> dict:
        self._validate_consumer(consumer)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise InboxStateError("limit must be between 1 and 500")
        if not self.db_path.exists():
            return {"events": [], "cursor": 0, "latest": 0, "truncated": False}
        with self._connection() as conn:
            row = conn.execute("SELECT seq FROM cursors WHERE profile=? AND consumer=?", (self.profile, consumer)).fetchone()
            cursor = row[0] if row else 0
            latest = conn.execute("SELECT COALESCE(MAX(seq),0) FROM observations WHERE profile=?", (self.profile,)).fetchone()[0]
            rows = conn.execute("SELECT seq,profile,item_id,fingerprint,kind,state,observed_at FROM observations WHERE profile=? AND seq>? ORDER BY seq LIMIT ?",
                                (self.profile, cursor, limit + 1)).fetchall()
            return {"events": [dict(r) for r in rows[:limit]], "cursor": cursor,
                    "latest": latest, "truncated": len(rows) > limit}

    def mark_seen(self, consumer="owner", upto=None) -> int:
        self._validate_consumer(consumer)
        if upto is not None and (not isinstance(upto, int) or isinstance(upto, bool) or upto < 0):
            raise InboxStateError("through must be a non-negative observation sequence")
        current = self.changes(consumer)
        if upto is not None and upto > current["latest"]:
            raise InboxStateError("cannot mark an unobserved future sequence")
        if not self.db_path.exists():
            return 0
        with self._connection(write=True) as conn:
            latest = conn.execute("SELECT COALESCE(MAX(seq),0) FROM observations WHERE profile=?", (self.profile,)).fetchone()[0]
            target = latest if upto is None else upto
            conn.execute("INSERT INTO cursors(profile,consumer,seq) VALUES (?,?,?) "
                         "ON CONFLICT(profile,consumer) DO UPDATE SET seq=MAX(cursors.seq,excluded.seq)",
                         (self.profile, consumer, target))
            return conn.execute("SELECT seq FROM cursors WHERE profile=? AND consumer=?", (self.profile, consumer)).fetchone()[0]
