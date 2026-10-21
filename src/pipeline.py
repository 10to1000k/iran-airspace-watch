"""پایپ‌لاین اصلی: از آیتم خام تا پیام تلگرام."""

from __future__ import annotations

import logging
from datetime import timedelta

from . import formatter, util
from .classify import Classifier
from .models import RawItem
from .translator import Translator

log = logging.getLogger(__name__)

SEVERITY_RANK = {"context": 0, "important": 1, "critical": 2}


class Pipeline:
    def __init__(self, config, db, bot, health=None):
        self.cfg = config
        self.db = db
        self.bot = bot
        self.health = health
        self.classifier = Classifier(config)
        self.translator = Translator(config, db)

    # ── ورودی اصلی ───────────────────────────────────────────
    def process(self, item: RawItem) -> str:
        fp = util.fingerprint(item.source_id, item.url, item.title, item.body[:400])

        # ثبت آیتم اول انجام می‌شود تا دو کالکتور همزمان هر دو نفرستند
        if not self.db.add_item(item, fp):
            return "seen"

        classification = self.classifier.classify(item)
        if not classification.relevant:
            return "irrelevant"

        tokens = util.tokens(f"{item.title} {item.body}")
        existing = self._find_cluster(tokens, classification)

        if existing is None:
            return self._create_event(item, fp, classification, tokens)
        return self._update_event(item, fp, classification, existing)

    # ── خوشه‌بندی ────────────────────────────────────────────
    def _find_cluster(self, tokens: set[str], classification):
        window = float(self.cfg.get("app.dedupe_window_hours", 6))
        threshold = float(self.cfg.get("app.similarity_threshold", 0.72))
        boost = float(self.cfg.get("app.entity_boost_threshold", 0.45))
        now = util.now_utc()

        best, best_score = None, 0.0
        for row in self.db.recent_events(window):
            if row["corrected"]:
                continue
            first_seen = util.parse_iso(row["first_seen"])
            if first_seen and now - first_seen > timedelta(hours=window):
                continue
            other = set((row["tokens"] or "").split())
            score = util.jaccard(tokens, other)
            same_entity = bool(classification.entity_key) and row["entity_key"] == classification.entity_key
            if (score >= threshold) or (same_entity and score >= boost):
                if score > best_score:
                    best, best_score = row, score
        return best

    # ── رخداد تازه ───────────────────────────────────────────
    def _create_event(self, item: RawItem, fp: str, cls, tokens: set[str]) -> str:
        translation = self.translator.translate_item(
            title=item.title,
            body=item.body,
            lang=item.lang,
            kind=item.kind,
            decoded_fa=item.extra.get("decoded_fa", ""),
            raw=item.extra.get("raw_notam", ""),
        )

        confidence = "official" if item.tier == 0 else "single"
        event_id, created = self.db.create_event({
            "fingerprint": fp,
            "tokens": tokens,
            "entity_key": cls.entity_key,
            "category": cls.category,
            "severity": cls.severity,
            "countries": cls.countries,
            "airlines": cls.airlines,
            "title_fa": translation.title_fa or item.title,
            "body_fa": translation.text_fa,
            "confidence": confidence,
            "numbers_ok": 1 if translation.numbers_ok else 0,
            "primary_url": item.url,
            "primary_source": item.source_name,
        })
        if not created:
            # رقیب زودتر ساخته — مثل به‌روزرسانی رفتار می‌کنیم
            row = self.db.get_event(event_id)
            return self._update_event(item, fp, cls, row) if row else "raced"

        event = self.db.get_event(event_id)
        text = formatter.format_event(event, self.cfg)
        message_id = None
        if self.cfg.get("notifications.include_feedback_buttons", True):
            keyboard = self.bot.feedback_keyboard(event_id, item.url)
        else:
            keyboard = None
        message_id = self.bot.send(
            text,
            silent=self._silent(cls.severity),
            keyboard=keyboard,
        )
        chat_id = self.bot.chat_id()
        if message_id and chat_id:
            self.db.save_message_ref(event_id, chat_id, message_id)
        self.db.attach_item_to_event(item.source_id, fp, event_id)
        if self.health:
            self.health.mark_publish()
        log.info("منتشر شد [%s/%s] %s", cls.severity, cls.category, item.title[:70])
        return "published"

    # ── به‌روزرسانی رخداد موجود ──────────────────────────────
    def _update_event(self, item: RawItem, fp: str, cls, event) -> str:
        event_id = int(event["id"])
        known_sources = self.db.distinct_sources_for_event(event_id)
        new_source = item.source_id not in known_sources
        self.db.attach_item_to_event(item.source_id, fp, event_id)

        updates: dict = {}
        if SEVERITY_RANK.get(cls.severity, 0) > SEVERITY_RANK.get(event["severity"], 0):
            updates["severity"] = cls.severity
        if cls.category != event["category"] and cls.severity == "critical":
            updates["category"] = cls.category

        if new_source:
            count = self.db.bump_source_count(event_id)
            if count >= 2:
                updates["confidence"] = "multi"

        if updates:
            self.db.update_event(event_id, **updates)

        refreshed = self.db.get_event(event_id)
        if not refreshed:
            return "update-missing"

        if refreshed["root_message_id"] and new_source:
            try:
                self.bot.edit(
                    int(refreshed["root_message_id"]),
                    formatter.format_event(refreshed, self.cfg),
                    chat_id=refreshed["chat_id"],
                    keyboard=self.bot.feedback_keyboard(event_id, refreshed["primary_url"]),
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("ویرایش پیام ریشه ناموفق: %s", exc)

        if new_source and int(refreshed["sources_count"]) <= 3 and refreshed["root_message_id"]:
            self.bot.send(
                formatter.format_update(refreshed, item.source_name),
                reply_to=int(refreshed["root_message_id"]),
                silent=self._silent(refreshed["severity"]),
            )

        log.info("به‌روزرسانی [%s] %s", event_id, item.title[:70])
        return "updated"

    # ── کمکی ─────────────────────────────────────────────────
    def _silent(self, severity: str) -> bool:
        loud = self.cfg.get("notifications.loud_severities", ["critical"]) or []
        silent_others = bool(self.cfg.get("notifications.silent_others", True))
        return silent_others and severity not in loud

    def process_many(self, items: list[RawItem]) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in items:
            try:
                outcome = self.process(item)
            except Exception:  # noqa: BLE001
                log.exception("پردازش آیتم ناموفق: %s", item.title[:70])
                outcome = "error"
            result[outcome] = result.get(outcome, 0) + 1
        return result
