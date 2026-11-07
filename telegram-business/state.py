"""Plugin-owned SQLite state for Telegram Business Mode.

Two tables, both living in the plugin's own database file
(``<HERMES_HOME>/telegram-business/state.db``) — the plugin never touches
Hermes' core ``state.db`` schema:

  business_connections — one row per Telegram Business account that linked
    the bot via BotFather Business Mode. Updated by BusinessConnection
    updates (established / edited / ended).

  business_drafts — pending owner-approval drafts. Created when a customer
    messages a connected chat and the manager produces a candidate reply;
    resolved when the owner taps Send / Edit / Discard or when it expires.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS business_connections (
    connection_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    owner_chat_id TEXT NOT NULL,
    can_reply INTEGER NOT NULL DEFAULT 0,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    auto_draft INTEGER NOT NULL DEFAULT 1,
    paused_chats TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_biz_conn_owner
    ON business_connections(owner_user_id);

CREATE TABLE IF NOT EXISTS business_drafts (
    draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL,
    owner_chat_id TEXT NOT NULL,
    customer_chat_id TEXT NOT NULL,
    customer_msg_id TEXT,
    customer_text TEXT NOT NULL,
    draft_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    owner_message_id TEXT,
    final_sent_text TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_biz_drafts_conn_customer
    ON business_drafts(connection_id, customer_chat_id, status);
"""


def _restrict_to_owner(path: Path) -> None:
    """Make ``path`` readable and writable by its owner only (a no-op on Windows).

    The store holds the customer conversation and the drafts the owner approved, so another
    local account must not read them. Best-effort: a filesystem that does not carry POSIX
    modes (FAT, a network share) keeps the default permissions rather than failing the plugin.
    """
    try:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    except OSError:
        pass


class BusinessStateDB:
    """Thread-safe SQLite store for connections + drafts."""

    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _restrict_to_owner(db_path.parent)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _restrict_to_owner(db_path)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    def upsert_telegram_business_connection(
        self,
        *,
        connection_id: str,
        owner_user_id: str,
        owner_chat_id: str,
        can_reply: bool,
        is_enabled: bool,
    ) -> None:
        """Insert or update a connection row.

        Preserves ``auto_draft`` and ``paused_chats`` across updates so the
        owner's preferences survive Telegram re-issuing the connection.
        """
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO business_connections (
                    connection_id, owner_user_id, owner_chat_id,
                    can_reply, is_enabled, auto_draft, paused_chats,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, '[]', ?, ?)
                ON CONFLICT(connection_id) DO UPDATE SET
                    owner_user_id = excluded.owner_user_id,
                    owner_chat_id = excluded.owner_chat_id,
                    can_reply = excluded.can_reply,
                    is_enabled = excluded.is_enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    str(connection_id), str(owner_user_id), str(owner_chat_id),
                    1 if can_reply else 0, 1 if is_enabled else 0, now, now,
                ),
            )
            self._conn.commit()

    @staticmethod
    def _conn_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["paused_chats"] = json.loads(d.get("paused_chats") or "[]")
        except (TypeError, ValueError):
            d["paused_chats"] = []
        d["can_reply"] = bool(d.get("can_reply"))
        d["is_enabled"] = bool(d.get("is_enabled"))
        d["auto_draft"] = bool(d.get("auto_draft", 1))
        return d

    def get_telegram_business_connection(
        self, connection_id: str
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM business_connections WHERE connection_id = ?",
                (str(connection_id),),
            ).fetchone()
        return self._conn_row_to_dict(row) if row is not None else None

    def list_telegram_business_connections(
        self, *, owner_user_id: Optional[str] = None, enabled_only: bool = True
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM business_connections"
        clauses: List[str] = []
        params: List[Any] = []
        if owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            params.append(str(owner_user_id))
        if enabled_only:
            clauses.append("is_enabled = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._conn_row_to_dict(r) for r in rows]

    def set_telegram_business_auto_draft(
        self, connection_id: str, *, auto_draft: bool
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE business_connections SET auto_draft = ?, updated_at = ? "
                "WHERE connection_id = ?",
                (1 if auto_draft else 0, time.time(), str(connection_id)),
            )
            self._conn.commit()

    def set_telegram_business_paused_chats(
        self, connection_id: str, paused_chats: List[str]
    ) -> None:
        payload = json.dumps([str(c) for c in (paused_chats or [])])
        with self._lock:
            self._conn.execute(
                "UPDATE business_connections SET paused_chats = ?, updated_at = ? "
                "WHERE connection_id = ?",
                (payload, time.time(), str(connection_id)),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Drafts
    # ------------------------------------------------------------------

    def create_telegram_business_draft(
        self,
        *,
        connection_id: str,
        owner_chat_id: str,
        customer_chat_id: str,
        customer_msg_id: Optional[str],
        customer_text: str,
        draft_text: str,
        ttl_seconds: float = 86400.0,
    ) -> int:
        """Insert a pending draft row and return its draft_id.

        Any prior pending drafts for the same (connection, customer_chat)
        are marked superseded so only one Send button is ever live per
        conversation.
        """
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE business_drafts SET status = 'superseded', updated_at = ? "
                "WHERE connection_id = ? AND customer_chat_id = ? AND status = 'pending'",
                (now, str(connection_id), str(customer_chat_id)),
            )
            cur = self._conn.execute(
                """
                INSERT INTO business_drafts (
                    connection_id, owner_chat_id, customer_chat_id,
                    customer_msg_id, customer_text, draft_text,
                    status, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    str(connection_id), str(owner_chat_id), str(customer_chat_id),
                    str(customer_msg_id) if customer_msg_id is not None else None,
                    customer_text, draft_text,
                    now, now, now + max(60.0, float(ttl_seconds)),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def get_telegram_business_draft(self, draft_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM business_drafts WHERE draft_id = ?",
                (int(draft_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_telegram_business_draft_owner_message(
        self, draft_id: int, owner_message_id: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE business_drafts SET owner_message_id = ?, updated_at = ? "
                "WHERE draft_id = ?",
                (str(owner_message_id), time.time(), int(draft_id)),
            )
            self._conn.commit()

    def resolve_telegram_business_draft(
        self,
        draft_id: int,
        *,
        status: str,
        final_sent_text: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Atomically mark a draft sent / edited / discarded / expired.

        Returns the prior row, or None if the draft no longer exists or was
        already resolved (so callbacks for stale buttons no-op safely).
        """
        if status not in {"sent", "edited", "discarded", "expired"}:
            raise ValueError(f"invalid business draft status: {status}")
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM business_drafts WHERE draft_id = ? AND status = 'pending'",
                (int(draft_id),),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE business_drafts SET status = ?, "
                "final_sent_text = COALESCE(?, final_sent_text), "
                "updated_at = ? WHERE draft_id = ?",
                (status, final_sent_text, now, int(draft_id)),
            )
            self._conn.commit()
            return dict(row)

    def get_pending_telegram_business_drafts_for_owner(
        self, owner_chat_id: str
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM business_drafts WHERE owner_chat_id = ? "
                "AND status = 'pending' ORDER BY created_at ASC",
                (str(owner_chat_id),),
            ).fetchall()
        return [dict(r) for r in rows]

    def expire_telegram_business_drafts(self, *, now: Optional[float] = None) -> int:
        """Settle pending drafts whose TTL has passed as ``expired``.

        ``now`` is the comparison instant, but ``updated_at`` records when the
        row was settled — a caller passing a future cut-off (a sweep run ahead
        of the clock) used to stamp every row with that future instant, so
        ``updated_at`` could sit past the wall clock and misorder against rows
        resolved in the normal flow.
        """
        cutoff = now if now is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE business_drafts SET status = 'expired', updated_at = ? "
                "WHERE status = 'pending' AND expires_at < ?",
                (time.time(), cutoff),
            )
            self._conn.commit()
            return int(cur.rowcount)
