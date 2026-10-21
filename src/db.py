"""لایه‌ی ذخیره‌سازی (SQLite). حجم این پروژه کوچک است و SQLite کافی است."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from . import util

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     TEXT NOT NULL,
    source_name   TEXT NOT NULL,
    source_url    TEXT NOT NULL,
    url           TEXT,
    lang          TEXT,
    country       TEXT,
    kind          TEXT,
    title         TEXT,
    body          TEXT,
    fingerprint   TEXT NOT NULL,
    published_at  TEXT,
    collected_at  TEXT NOT NULL,
    event_id      INTEGER,
    UNIQUE(source_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT NOT NULL UNIQUE,
    tokens          TEXT NOT NULL DEFAULT '',
    entity_key      TEXT NOT NULL DEFAULT '',
    category        TEXT,
    severity        TEXT,
    countries       TEXT,
    airlines        TEXT,
    title_fa        TEXT,
    body_fa         TEXT,
    confidence      TEXT,
    numbers_ok      INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'open',
    primary_url     TEXT,
    primary_source  TEXT,
    chat_id         TEXT,
    root_message_id INTEGER,
    sources_count   INTEGER NOT NULL DEFAULT 1,
    corrected       INTEGER NOT NULL DEFAULT 0,
    first_seen      TEXT NOT NULL,
    last_updated    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_recent ON events(first_seen);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);

CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL,
    verdict    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources_health (
    source_id   TEXT PRIMARY KEY,
    last_ok     TEXT,
    last_error  TEXT,
    error_count INTEGER NOT NULL DEFAULT 0
);
"""


class Database:
    def __init__(self, path: Path):
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=20)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ── kv ───────────────────────────────────────────────────
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.conn.commit()

    # ── items ────────────────────────────────────────────────
    def item_seen(self, source_id: str, fp: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM items WHERE source_id=? AND fingerprint=?",
                (source_id, fp),
            ).fetchone()
        return row is not None

    def add_item(self, item, fp: str, event_id: int | None = None) -> bool:
        """True اگر تازه ثبت شد، False اگر تکراری بود."""
        with self._lock:
            try:
                self.conn.execute(
                    """INSERT INTO items
                       (source_id, source_name, source_url, url, lang, country, kind,
                        title, body, fingerprint, published_at, collected_at, event_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item.source_id, item.source_name, item.source_url, item.url,
                        item.lang, item.country, item.kind, item.title, item.body,
                        fp, util.iso(item.published_at) if item.published_at else None,
                        util.iso(util.now_utc()), event_id,
                    ),
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # ── events ───────────────────────────────────────────────
    def recent_events(self, hours: float) -> list[sqlite3.Row]:
        since = util.hours_ago_iso(hours)
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM events WHERE first_seen >= ? ORDER BY first_seen DESC",
                (since,),
            ).fetchall()

    def get_event(self, event_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM events WHERE id=?", (event_id,)
            ).fetchone()

    def create_event(self, data: dict) -> tuple[int, bool]:
        """قفل اثر انگشت: اگر رقیب زودتر ساخته باشد، IntegrityError می‌دهد."""
        now = util.iso(util.now_utc())
        payload = {
            "fingerprint": data["fingerprint"],
            "tokens": " ".join(sorted(data.get("tokens", []))),
            "entity_key": data.get("entity_key", ""),
            "category": data.get("category", "context"),
            "severity": data.get("severity", "context"),
            "countries": ",".join(data.get("countries", [])),
            "airlines": ",".join(data.get("airlines", [])),
            "title_fa": data.get("title_fa", ""),
            "body_fa": data.get("body_fa", ""),
            "confidence": data.get("confidence", "single"),
            "numbers_ok": int(data.get("numbers_ok", 1)),
            "primary_url": data.get("primary_url", ""),
            "primary_source": data.get("primary_source", ""),
            "first_seen": now,
            "last_updated": now,
        }
        columns = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        with self._lock:
            try:
                cur = self.conn.execute(
                    f"INSERT INTO events ({columns}) VALUES ({placeholders})",
                    tuple(payload.values()),
                )
                self.conn.commit()
                return int(cur.lastrowid), True
            except sqlite3.IntegrityError:
                row = self.conn.execute(
                    "SELECT id FROM events WHERE fingerprint=?", (data["fingerprint"],)
                ).fetchone()
                return (int(row["id"]) if row else 0), False

    def update_event(self, event_id: int, **fields) -> None:
        if not fields:
            return
        fields["last_updated"] = util.iso(util.now_utc())
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self._lock:
            self.conn.execute(
                f"UPDATE events SET {assignments} WHERE id=?",
                (*fields.values(), event_id),
            )
            self.conn.commit()

    def bump_source_count(self, event_id: int) -> int:
        with self._lock:
            self.conn.execute(
                "UPDATE events SET sources_count = sources_count + 1 WHERE id=?",
                (event_id,),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT sources_count FROM events WHERE id=?", (event_id,)
            ).fetchone()
        return int(row["sources_count"]) if row else 1

    def attach_item_to_event(self, source_id: str, fp: str, event_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE items SET event_id=? WHERE source_id=? AND fingerprint=?",
                (event_id, source_id, fp),
            )
            self.conn.commit()

    def save_message_ref(self, event_id: int, chat_id: str, message_id: int) -> None:
        self.update_event(event_id, chat_id=str(chat_id), root_message_id=int(message_id))

    def find_event_by_message(self, chat_id: str, message_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM events WHERE chat_id=? AND root_message_id=?",
                (str(chat_id), int(message_id)),
            ).fetchone()

    def distinct_sources_for_event(self, event_id: int) -> set[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT source_id FROM items WHERE event_id=?", (event_id,)
            ).fetchall()
        return {row["source_id"] for row in rows}

    def mark_corrected(self, event_id: int) -> None:
        self.update_event(event_id, corrected=1, confidence="corrected")

    # ── بازخورد ──────────────────────────────────────────────
    def add_feedback(self, event_id: int, verdict: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO feedback(event_id, verdict, created_at) VALUES (?,?,?)",
                (event_id, verdict, util.iso(util.now_utc())),
            )
            self.conn.commit()

    def feedback_summary(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                """SELECT e.primary_source AS source,
                          SUM(CASE WHEN f.verdict='up'   THEN 1 ELSE 0 END) AS up,
                          SUM(CASE WHEN f.verdict='down' THEN 1 ELSE 0 END) AS down
                   FROM feedback f JOIN events e ON e.id = f.event_id
                   GROUP BY e.primary_source
                   ORDER BY down DESC, up DESC"""
            ).fetchall()

    # ── سلامت منابع ──────────────────────────────────────────
    def mark_source_ok(self, source_id: str) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO sources_health(source_id, last_ok, error_count)
                   VALUES(?,?,0)
                   ON CONFLICT(source_id) DO UPDATE
                   SET last_ok=excluded.last_ok, last_error=NULL, error_count=0""",
                (source_id, util.iso(util.now_utc())),
            )
            self.conn.commit()

    def mark_source_error(self, source_id: str, error: str) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO sources_health(source_id, last_error, error_count)
                   VALUES(?,?,1)
                   ON CONFLICT(source_id) DO UPDATE
                   SET last_error=excluded.last_error,
                       error_count=sources_health.error_count + 1""",
                (source_id, error[:400]),
            )
            self.conn.commit()

    def stats(self) -> dict:
        with self._lock:
            events = self.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
            items = self.conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
            published = self.conn.execute(
                "SELECT COUNT(*) c FROM events WHERE root_message_id IS NOT NULL"
            ).fetchone()["c"]
        return {"events": events, "items": items, "published": published}
