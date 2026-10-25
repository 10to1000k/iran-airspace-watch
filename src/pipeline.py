"""پایپ‌لاین اصلی: از آیتم خام تا پیام تلگرام."""

from __future__ import annotations

import logging
from datetime import timedelta

from . import formatter, util
from .classify import Classifier
from .models import RawItem
from .social import extract_hashtags
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
            self._ack_telegram_post(item)
            return "seen"

        try:
            classification = self.classifier.classify(item)
            if not classification.relevant:
                self._ack_telegram_post(item)
                return "irrelevant"

            tokens = util.tokens(f"{item.title} {item.body}")
            existing = self._find_cluster(tokens, classification, item.published_at)

            if existing is None:
                outcome = self._create_event(item, fp, classification, tokens)
            else:
                outcome = self._update_event(item, fp, classification, existing)

            if (
                item.kind in {"x", "telegram"}
                and self.cfg.get("social.auto_discover_hashtags", True)
            ):
                self.db.record_hashtags(extract_hashtags(item.text), item.source_id, fp)
            self._ack_telegram_post(item)
            return outcome
        except Exception:
            # اگر قبل از اتصال آیتم به رخداد خطای غیرمنتظره رخ داد، آیتم را آزاد کن
            # تا در چرخه‌ی بعدی دوباره پردازش شود و برای همیشه نسوزد.
            self.db.delete_item(item.source_id, fp)
            raise

    def _ack_telegram_post(self, item: RawItem) -> None:
        update_key = item.extra.get("telegram_inbox_id")
        acknowledge = getattr(self.bot, "ack_channel_post", None)
        if not update_key or not callable(acknowledge):
            return
        try:
            acknowledge(str(update_key))
        except Exception as exc:  # noqa: BLE001
            log.warning("تأیید پیام صف تلگرام ناموفق ماند؛ در چرخه‌ی بعد دوباره بررسی می‌شود: %s", exc)

    # ── خوشه‌بندی ────────────────────────────────────────────
    def _find_cluster(self, tokens: set[str], classification, published_at=None):
        window = max(0.0, float(self.cfg.get("app.dedupe_window_hours", 6)))
        late_window = max(
            window,
            float(self.cfg.get("app.late_dedupe_window_hours", 168)),
        )
        publication_window = max(
            0.0,
            float(self.cfg.get("app.late_dedupe_published_window_hours", 168)),
        )
        threshold = float(self.cfg.get("app.similarity_threshold", 0.72))
        boost = float(self.cfg.get("app.entity_boost_threshold", 0.45))
        now = util.now_utc()

        if published_at and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=util.UTC)

        best, best_score = None, 0.0
        for row in self.db.recent_events(late_window):
            if row["corrected"]:
                continue
            first_seen = util.parse_iso(row["first_seen"])
            age = now - first_seen if first_seen else None
            recent_match = age is not None and age <= timedelta(hours=window)
            late_match = False
            if not recent_match and published_at and row["primary_published_at"]:
                prior_published_at = util.parse_iso(row["primary_published_at"])
                if prior_published_at:
                    late_match = (
                        abs((published_at - prior_published_at).total_seconds())
                        <= publication_window * 3600
                    )
            if not recent_match and not late_match:
                continue

            other = set((row["tokens"] or "").split())
            score = util.jaccard(tokens, other)
            same_entity = (
                bool(classification.entity_key)
                and row["entity_key"] == classification.entity_key
            )
            # برای کپی‌های دیررس سخت‌گیری بیشتر می‌کنیم تا دو رخداد تکرارشونده
            # در یک فرودگاه صرفاً به‌خاطر entity مشترک ادغام نشوند.
            matches = score >= threshold or (
                recent_match and same_entity and score >= boost
            )
            if matches and score > best_score:
                best, best_score = row, score
        return best

    def _translate_item(self, item: RawItem):
        translation = self.translator.translate_item(
            title=item.title,
            body=item.body,
            lang=item.lang,
            kind=item.kind,
            decoded_fa=item.extra.get("decoded_fa", ""),
            raw=item.extra.get("raw_notam", ""),
        )
        if translation.engine in {"disabled", "failed"} or (item.body and not translation.text_fa):
            raise RuntimeError("ترجمه‌ی فارسی کامل نشد؛ انتشار متن ترجمه‌نشده متوقف شد")
        return translation

    # ── رخداد تازه ───────────────────────────────────────────
    def _create_event(self, item: RawItem, fp: str, cls, tokens: set[str]) -> str:
        translation = self._translate_item(item)
        collected_at = util.parse_iso(str(item.extra.get("collected_at") or ""))

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
            "primary_published_at": util.iso(item.published_at) if item.published_at else None,
            "first_seen": util.iso(collected_at or util.now_utc()),
        })
        if not created:
            # رقیب زودتر ساخته — مثل به‌روزرسانی رفتار می‌کنیم
            row = self.db.get_event(event_id)
            return self._update_event(item, fp, cls, row) if row else "raced"

        # اول آیتم به رخداد وصل می‌شود تا حتی اگر ارسال تلگرام موقتاً شکست خورد،
        # رخداد ساخته بماند و flush_unsent در چرخه‌ی بعدی آن را ارسال کند.
        self.db.attach_item_to_event(item.source_id, fp, event_id)
        event = self.db.get_event(event_id)
        if not self._deliver_event(event):
            log.warning("رخداد %d ثبت شد ولی ارسال تلگرام به چرخه‌ی بعد موکول شد", event_id)
            return "queued"
        log.info("منتشر شد [%s/%s] %s", cls.severity, cls.category, item.title[:70])
        return "published"

    def _deliver_event(self, event) -> bool:
        event_id = int(event["id"])
        sent_at = util.iso(util.now_utc())
        display_event = dict(event)
        display_event["telegram_sent_at"] = sent_at
        text = formatter.format_event(display_event, self.cfg)
        if self.cfg.get("notifications.include_feedback_buttons", True):
            keyboard = self.bot.feedback_keyboard(event_id, event["primary_url"] or "")
        else:
            keyboard = None
        try:
            message_id = self.bot.send(
                text,
                silent=self._silent(event["severity"]),
                keyboard=keyboard,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ارسال تلگرام برای رخداد %d ناموفق بود: %s", event_id, exc)
            return False

        chat_id = self.bot.chat_id()
        if message_id and chat_id:
            self.db.save_message_ref(event_id, chat_id, message_id, sent_at=sent_at)
            if self.health:
                self.health.mark_publish()
            return True
        return bool(self.cfg.dry_run)

    def flush_unsent(self) -> int:
        """ارسال مجدد رخدادهای تازه‌ای که به خاطر قطعی موقت شبکه یا نبود گیرنده جا مانده‌اند."""
        if self.cfg.dry_run or not self.bot.configured or not self.bot.chat_id():
            return 0
        window = float(self.cfg.get("app.dedupe_window_hours", 6))
        sent = 0
        for event in self.db.unsent_events(hours=window, limit=5):
            if self._deliver_event(event):
                sent += 1
        if sent:
            log.info("%d رخداد جامانده با موفقیت به تلگرام ارسال شد", sent)
        return sent

    # ── به‌روزرسانی رخداد موجود ──────────────────────────────
    def _update_event(self, item: RawItem, fp: str, cls, event) -> str:
        event_id = int(event["id"])
        known_sources = self.db.distinct_sources_for_event(event_id)
        new_source = item.source_id not in known_sources
        send_full_source = (
            new_source
            and bool(event["root_message_id"])
            and int(event["sources_count"] or 1) < 3
        )
        source_translation = None
        if send_full_source:
            try:
                source_translation = self._translate_item(item)
            except RuntimeError as exc:
                # ترجمه‌نشدنِ بازنشر نباید منبع/زمان زودتر را از سابقه حذف کند؛
                # متن خام منتشر نمی‌شود و فقط metadata رخداد به‌روز خواهد شد.
                log.warning("ترجمه‌ی منبع تازه برای رخداد %d انجام نشد: %s", event_id, exc)
        self.db.attach_item_to_event(item.source_id, fp, event_id)
        earliest = self.db.earliest_item_for_event(event_id)

        updates: dict = {}
        primary_changed = False
        if earliest:
            primary_fields = {
                "primary_source": earliest["source_name"] or "",
                "primary_url": earliest["url"] or "",
                "primary_published_at": earliest["published_at"],
            }
            primary_changed = any(
                event[key] != value for key, value in primary_fields.items()
            )
            if primary_changed:
                updates.update(primary_fields)
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

        if refreshed["root_message_id"] and (new_source or primary_changed or updates):
            try:
                self.bot.edit(
                    int(refreshed["root_message_id"]),
                    formatter.format_event(refreshed, self.cfg),
                    chat_id=refreshed["chat_id"],
                    keyboard=self.bot.feedback_keyboard(event_id, refreshed["primary_url"]),
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("ویرایش پیام ریشه ناموفق: %s", exc)

        if source_translation and int(refreshed["sources_count"]) <= 3 and refreshed["root_message_id"]:
            try:
                source_event = dict(refreshed)
                source_event.update({
                    "title_fa": source_translation.title_fa or item.title,
                    "body_fa": source_translation.text_fa,
                    "numbers_ok": 1 if source_translation.numbers_ok else 0,
                    "primary_source": item.source_name,
                    "primary_url": item.url,
                    "primary_published_at": util.iso(item.published_at) if item.published_at else None,
                    "telegram_sent_at": None,
                })
                self.bot.send(
                    formatter.format_source_update(source_event, self.cfg),
                    reply_to=int(refreshed["root_message_id"]),
                    silent=self._silent(refreshed["severity"]),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("ارسال متن کامل منبع تازه برای رخداد %d ناموفق بود: %s", event_id, exc)

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
