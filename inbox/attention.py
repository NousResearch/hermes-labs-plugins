"""Plugin-owned attention lanes (ack / snooze) over durable observations.

``AttentionState`` wraps an ``InboxState`` and writes only to its own
``attention`` table (keyed by profile + observation sequence). Source
observations/latest/cursors are never copied, mutated or derived into
attention rows; the ``attention`` table is created only inside an explicit
ack or snooze write transaction, never on a read path.
"""
from __future__ import annotations

import math
import time

from .state import InboxState, InboxStateError

_MAX_SNOOZE_SECONDS = 604800  # one week

_ATTENTION_CREATE = (
    "CREATE TABLE IF NOT EXISTS attention ("
    "profile TEXT NOT NULL, occurrence INTEGER NOT NULL, "
    "acknowledged INTEGER NOT NULL DEFAULT 0, snooze_until REAL, "
    "PRIMARY KEY(profile, occurrence))"
)


class AttentionState:
    def __init__(self, state: InboxState) -> None:
        if not isinstance(state, InboxState):
            raise InboxStateError("attention requires an InboxState")
        self.state = state

    # -- validation -------------------------------------------------------

    @staticmethod
    def _positive_int(name: str, value) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise InboxStateError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _finite_now(value) -> float:
        value = time.time() if value is None else value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise InboxStateError("now must be a finite number")
        return float(value)

    @staticmethod
    def _positive_finite_float(name: str, value, maximum: float) -> float:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0 or value > maximum):
            raise InboxStateError(f"{name} must be a positive finite number of at most {maximum}")
        return float(value)

    def _require_observation(self, conn, occurrence: int) -> None:
        row = conn.execute(
            "SELECT seq FROM observations WHERE profile=? AND seq=?",
            (self.state.profile, occurrence),
        ).fetchone()
        if row is None:
            raise InboxStateError("unknown or foreign occurrence")

    # -- public API --------------------------------------------------------

    def ack(self, occurrence: int) -> dict:
        occurrence = self._positive_int("occurrence", occurrence)
        if not self.state.db_path.exists():
            raise InboxStateError("unknown occurrence: no observations exist")
        with self.state._connection(write=True) as conn:
            conn.execute(_ATTENTION_CREATE)
            self._require_observation(conn, occurrence)
            conn.execute(
                "INSERT INTO attention(profile, occurrence, acknowledged, snooze_until) "
                "VALUES (?, ?, 1, NULL) "
                "ON CONFLICT(profile, occurrence) DO UPDATE SET acknowledged=1, snooze_until=NULL",
                (self.state.profile, occurrence),
            )
        return {"occurrence": occurrence, "acknowledged": True, "snooze_until": None}

    def snooze(self, occurrence: int, seconds: float, now: float | None = None) -> dict:
        occurrence = self._positive_int("occurrence", occurrence)
        seconds = self._positive_finite_float("seconds", seconds, _MAX_SNOOZE_SECONDS)
        now = self._finite_now(now)
        deadline = now + seconds
        if not math.isfinite(deadline):
            raise InboxStateError("snooze deadline must be finite")
        if not self.state.db_path.exists():
            raise InboxStateError("unknown occurrence: no observations exist")
        with self.state._connection(write=True) as conn:
            conn.execute(_ATTENTION_CREATE)
            self._require_observation(conn, occurrence)
            row = conn.execute(
                "SELECT acknowledged FROM attention WHERE profile=? AND occurrence=?",
                (self.state.profile, occurrence),
            ).fetchone()
            acknowledged = int(row[0]) if row is not None else 0
            conn.execute(
                "INSERT INTO attention(profile, occurrence, acknowledged, snooze_until) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(profile, occurrence) DO UPDATE SET snooze_until=excluded.snooze_until",
                (self.state.profile, occurrence, acknowledged, deadline),
            )
        return {"occurrence": occurrence, "acknowledged": bool(acknowledged), "snooze_until": deadline}

    def states_for(self, items: list[dict], now: float | None = None) -> dict:
        if not isinstance(items, list):
            raise InboxStateError("items must be a list")
        now = self._finite_now(now)
        result: dict[str, dict] = {}
        if not self.state.db_path.exists():
            for item in items:
                result[item.get("id", item)] = {
                    "occurrence": None, "acknowledged": False,
                    "snooze_until": None, "visible": True,
                }
            return result
        with self.state._connection() as conn:
            has_attention = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attention'",
            ).fetchone() is not None
            for item in items:
                latest = conn.execute(
                    "SELECT fingerprint, kind, state, seq FROM latest WHERE profile=? AND item_id=?",
                    (self.state.profile, item.get("id")),
                ).fetchone()
                if latest is None or (latest["fingerprint"], latest["kind"], latest["state"]) != (
                    item.get("fingerprint"), item.get("kind"), item.get("state")):
                    result[item.get("id", item)] = {
                        "occurrence": None, "acknowledged": False,
                        "snooze_until": None, "visible": True,
                    }
                    continue
                occurrence = int(latest["seq"])
                acknowledged = False
                snooze_until = None
                if has_attention:
                    att = conn.execute(
                        "SELECT acknowledged, snooze_until FROM attention WHERE profile=? AND occurrence=?",
                        (self.state.profile, occurrence),
                    ).fetchone()
                    if att is not None:
                        acknowledged = bool(att["acknowledged"])
                        snooze_until = float(att["snooze_until"]) if att["snooze_until"] is not None else None
                visible = (not acknowledged) and (snooze_until is None or snooze_until <= now)
                result[item.get("id", item)] = {
                    "occurrence": occurrence, "acknowledged": acknowledged,
                    "snooze_until": snooze_until, "visible": visible,
                }
        return result
