"""لایه‌ی ذخیره‌سازی (SQLite). حجم این پروژه کوچک است و SQLite کافی است."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

from . import util
from .models import RawItem

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
    primary_published_at TEXT,
    telegram_sent_at TEXT,
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

CREATE TABLE IF NOT EXISTS telegram_news_chats (
    chat_id     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    event_id    INTEGER NOT NULL,
    history_json TEXT NOT NULL DEFAULT '[]',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY(chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_telegram_news_chats_updated
    ON telegram_news_chats(updated_at);

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

CREATE TABLE IF NOT EXISTS hashtag_observations (
    tag             TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    item_fingerprint TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    PRIMARY KEY(tag, source_id, item_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_hashtag_observations_date
    ON hashtag_observations(observed_at);

CREATE TABLE IF NOT EXISTS telegram_inbox (
    update_key   TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL,
    source_name  TEXT NOT NULL,
    source_url   TEXT NOT NULL,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    lang         TEXT NOT NULL,
    country      TEXT NOT NULL,
    extra_json   TEXT NOT NULL DEFAULT '{}',
    published_at TEXT NOT NULL,
    queued_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telegram_inbox_date
    ON telegram_inbox(published_at);
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
            self._migrate_event_publication_columns()
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_event_published "
                "ON items(event_id, published_at)"
            )
            self.conn.commit()

    def _migrate_event_publication_columns(self) -> None:
        """افزودن ستون‌های زمان به دیتابیس‌های قدیمی و پرکردن منبع اولیه از آیتم‌ها."""
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(events)").fetchall()
        }
        if "primary_published_at" not in columns:
            self.conn.execute("ALTER TABLE events ADD COLUMN primary_published_at TEXT")
        if "telegram_sent_at" not in columns:
            self.conn.execute("ALTER TABLE events ADD COLUMN telegram_sent_at TEXT")

        # بازیابی منبع و timestamp زودتر از آیتم‌های پیشینِ همین رخداد.
        self.conn.execute(
            """UPDATE events AS e
               SET (primary_source, primary_url, primary_published_at) = (
                   SELECT i.source_name, i.url, i.published_at
                   FROM items AS i
                   WHERE i.event_id=e.id
                   ORDER BY CASE WHEN i.published_at IS NULL THEN 1 ELSE 0 END,
                            i.published_at, i.id
                   LIMIT 1
               )
               WHERE e.primary_published_at IS NULL
                 AND EXISTS (SELECT 1 FROM items AS i WHERE i.event_id=e.id)"""
        )

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

    # ── هشتگ‌های شبکه‌های اجتماعی ─────────────────────────────
    def record_hashtags(self, tags: list[str], source_id: str, fingerprint: str) -> None:
        if not tags or not source_id or not fingerprint:
            return
        observed = util.iso(util.now_utc())
        rows = [
            (tag, source_id, fingerprint, observed)
            for tag in set(tags)
            if tag and len(tag) <= 64
        ]
        if not rows:
            return
        with self._lock:
            self.conn.executemany(
                """INSERT OR IGNORE INTO hashtag_observations
                   (tag, source_id, item_fingerprint, observed_at) VALUES(?,?,?,?)""",
                rows,
            )
            self.conn.commit()

    def discovered_hashtags(
        self, min_mentions: int = 2, min_sources: int = 2,
        days: int = 30, limit: int = 24,
    ) -> list[str]:
        since = util.hours_ago_iso(max(1, days) * 24)
        with self._lock:
            rows = self.conn.execute(
                """SELECT tag
                   FROM hashtag_observations
                   WHERE observed_at >= ?
                   GROUP BY tag
                   HAVING COUNT(*) >= ? AND COUNT(DISTINCT source_id) >= ?
                   ORDER BY COUNT(DISTINCT source_id) DESC, MAX(observed_at) DESC
                   LIMIT ?""",
                (since, max(1, min_mentions), max(1, min_sources), max(1, limit)),
            ).fetchall()
        return [str(row["tag"]) for row in rows]

    # ── صف بادوام پست‌های کانال تلگرام ──────────────────────
    def enqueue_telegram_post(self, update_key: str, item: RawItem) -> None:
        if not update_key:
            return
        published_at = item.published_at or util.now_utc()
        with self._lock:
            self.conn.execute(
                """INSERT OR IGNORE INTO telegram_inbox
                   (update_key, source_id, source_name, source_url, url, title, body,
                    lang, country, extra_json, published_at, queued_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    update_key,
                    item.source_id,
                    item.source_name,
                    item.source_url,
                    item.url,
                    item.title,
                    item.body,
                    item.lang,
                    item.country,
                    json.dumps(item.extra, ensure_ascii=False),
                    util.iso(published_at),
                    util.iso(util.now_utc()),
                ),
            )
            self.conn.commit()

    def pending_telegram_posts(self, limit: int = 100, max_age_hours: float = 12) -> list[RawItem]:
        cutoff = util.hours_ago_iso(max(1.0, float(max_age_hours)))
        with self._lock:
            self.conn.execute(
                "DELETE FROM telegram_inbox WHERE published_at < ?", (cutoff,)
            )
            rows = self.conn.execute(
                """SELECT * FROM telegram_inbox
                   WHERE published_at >= ? ORDER BY queued_at, update_key LIMIT ?""",
                (cutoff, max(1, int(limit))),
            ).fetchall()
            self.conn.commit()

        items: list[RawItem] = []
        for row in rows:
            try:
                extra = json.loads(row["extra_json"] or "{}")
            except (TypeError, ValueError):
                extra = {}
            extra["telegram_inbox_id"] = str(row["update_key"])
            items.append(
                RawItem(
                    source_id=str(row["source_id"]),
                    source_name=str(row["source_name"]),
                    source_url=str(row["source_url"]),
                    url=str(row["url"]),
                    title=str(row["title"]),
                    body=str(row["body"]),
                    lang=str(row["lang"]),
                    country=str(row["country"]),
                    kind="telegram",
                    published_at=util.parse_iso(row["published_at"]),
                    extra=extra,
                )
            )
        return items

    def ack_telegram_post(self, update_key: str) -> None:
        if not update_key:
            return
        with self._lock:
            self.conn.execute("DELETE FROM telegram_inbox WHERE update_key=?", (update_key,))
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
                        item.extra.get("collected_at") or util.iso(util.now_utc()), event_id,
                    ),
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def delete_item(self, source_id: str, fp: str) -> None:
        """حذف آیتم در صورتی که پردازش آن قبل از ساخت رخداد با خطا متوقف شده باشد."""
        with self._lock:
            self.conn.execute(
                "DELETE FROM items WHERE source_id=? AND fingerprint=? AND event_id IS NULL",
                (source_id, fp),
            )
            self.conn.commit()

    # ── events ───────────────────────────────────────────────
    def recent_events(self, hours: float) -> list[sqlite3.Row]:
        since = util.hours_ago_iso(hours)
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM events WHERE first_seen >= ? ORDER BY first_seen DESC",
                (since,),
            ).fetchall()

    def latest_events(self, limit: int = 5) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()

    def unsent_events(self, hours: float = 6.0, limit: int = 5) -> list[sqlite3.Row]:
        """رخدادهای تازه‌ای که به دلیل خطای موقت تلگرام یا نبود گیرنده هنوز ارسال نشده‌اند."""
        since = util.hours_ago_iso(hours)
        with self._lock:
            return self.conn.execute(
                """SELECT * FROM events
                   WHERE root_message_id IS NULL
                     AND corrected = 0
                     AND first_seen >= ?
                   ORDER BY id ASC
                   LIMIT ?""",
                (since, int(limit)),
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
            "primary_published_at": data.get("primary_published_at"),
            "first_seen": data.get("first_seen") or now,
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

    def earliest_item_for_event(self, event_id: int) -> sqlite3.Row | None:
        """زودترین آیتمِ منتشرشده در میان منابعی که به این رخداد وصل شده‌اند."""
        with self._lock:
            return self.conn.execute(
                """SELECT source_id, source_name, source_url, url, title, body,
                          lang, country, kind, published_at
                   FROM items
                   WHERE event_id=?
                   ORDER BY CASE WHEN published_at IS NULL THEN 1 ELSE 0 END,
                            published_at, id
                   LIMIT 1""",
                (event_id,),
            ).fetchone()

    def save_message_ref(
        self,
        event_id: int,
        chat_id: str,
        message_id: int,
        sent_at: str | None = None,
    ) -> None:
        self.update_event(
            event_id,
            chat_id=str(chat_id),
            root_message_id=int(message_id),
            telegram_sent_at=sent_at or util.iso(util.now_utc()),
        )

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

    # ── گفت‌وگوی خبری تلگرام ─────────────────────────────────
    def save_news_chat_session(
        self,
        chat_id: str,
        user_id: str,
        event_id: int,
        history: list[dict] | None = None,
    ) -> None:
        safe_history = []
        for turn in (history or [])[-12:]:
            if not isinstance(turn, dict):
                continue
            role = str(turn.get("role") or "")
            content = str(turn.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                safe_history.append({"role": role, "content": content[:3000]})
        with self._lock:
            self.conn.execute(
                """INSERT INTO telegram_news_chats
                       (chat_id, user_id, event_id, history_json, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id, user_id) DO UPDATE SET
                       event_id=excluded.event_id,
                       history_json=excluded.history_json,
                       updated_at=excluded.updated_at""",
                (
                    str(chat_id), str(user_id), int(event_id),
                    json.dumps(safe_history, ensure_ascii=False), util.iso(util.now_utc()),
                ),
            )
            self.conn.commit()

    def get_news_chat_session(
        self,
        chat_id: str,
        user_id: str,
        max_age_minutes: float = 120,
    ) -> dict | None:
        ttl_minutes = max(1.0, float(max_age_minutes))
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM telegram_news_chats WHERE chat_id=? AND user_id=?",
                (str(chat_id), str(user_id)),
            ).fetchone()
            if not row:
                return None
            updated_at = util.parse_iso(row["updated_at"])
            age_seconds = (
                (util.now_utc() - updated_at).total_seconds()
                if updated_at else float("inf")
            )
            if age_seconds > ttl_minutes * 60:
                self.conn.execute(
                    "DELETE FROM telegram_news_chats WHERE chat_id=? AND user_id=?",
                    (str(chat_id), str(user_id)),
                )
                self.conn.commit()
                return None
            try:
                history = json.loads(row["history_json"] or "[]")
            except (TypeError, ValueError):
                history = []
            if not isinstance(history, list):
                history = []
            return {
                "event_id": int(row["event_id"]),
                "history": history,
                "updated_at": row["updated_at"],
            }

    def clear_news_chat_session(self, chat_id: str, user_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM telegram_news_chats WHERE chat_id=? AND user_id=?",
                (str(chat_id), str(user_id)),
            )
            self.conn.commit()

    def prune_news_chat_sessions(self, max_age_minutes: float = 120) -> int:
        ttl_minutes = max(1.0, float(max_age_minutes))
        cutoff = util.iso(util.now_utc() - timedelta(minutes=ttl_minutes))
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM telegram_news_chats WHERE updated_at < ?",
                (cutoff,),
            )
            deleted = max(0, int(cursor.rowcount))
            if deleted:
                self.conn.commit()
        return deleted

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

    def sources_health_summary(self) -> dict:
        with self._lock:
            ok = self.conn.execute(
                "SELECT COUNT(*) c FROM sources_health WHERE error_count = 0"
            ).fetchone()["c"]
            failing = self.conn.execute(
                "SELECT source_id, error_count FROM sources_health "
                "WHERE error_count > 0 ORDER BY error_count DESC"
            ).fetchall()
        return {
            "ok": ok,
            "failing": [(row["source_id"], row["error_count"]) for row in failing],
        }

    # ── نگه‌داری و پاک‌سازی دوره‌ای ─────────────────────────
    def prune(self, keep_days: int = 30, max_cache_entries: int = 2000) -> int:
        """حذف آیتم‌های قدیمی برای جلوگیری از رشد بی‌وقفه‌ی دیسک."""
        cutoff = util.hours_ago_iso(keep_days * 24)
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM items WHERE collected_at < ? AND event_id IS NULL",
                (cutoff,),
            )
            deleted = cur.rowcount
            self.conn.execute(
                "DELETE FROM hashtag_observations WHERE observed_at < ?", (cutoff,)
            )
            tr_count = self.conn.execute(
                "SELECT COUNT(*) c FROM kv WHERE key LIKE 'tr:%'"
            ).fetchone()["c"]
            if tr_count > max_cache_entries:
                excess = tr_count - max_cache_entries
                self.conn.execute(
                    "DELETE FROM kv WHERE rowid IN "
                    "(SELECT rowid FROM kv WHERE key LIKE 'tr:%' ORDER BY rowid ASC LIMIT ?)",
                    (excess,),
                )
            self.conn.commit()
        return deleted

    def stats(self) -> dict:
        with self._lock:
            events = self.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
            items = self.conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
            published = self.conn.execute(
                "SELECT COUNT(*) c FROM events WHERE root_message_id IS NOT NULL"
            ).fetchone()["c"]
        return {"events": events, "items": items, "published": published}
