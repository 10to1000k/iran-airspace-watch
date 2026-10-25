"""ارسال به تلگرام و دریافت بازخورد (بدون کتابخانه‌ی واسط، مستقیم روی Bot API)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
import logging
import threading
import time

import requests

from . import formatter, util
from .models import RawItem

log = logging.getLogger(__name__)

MAX_LEN = 3900


class TelegramError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0, retry_after: float = 0.0):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class TelegramBot:
    def __init__(self, config, db, health=None):
        self.cfg = config
        self.db = db
        self.health = health
        self.token = config.telegram_token
        self.dry_run = config.dry_run
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_ready_callbacks: list = []
        self._message_continuations: dict[int, list[int]] = {}
        self._news_chat_handler = None
        self._news_chat_enabled = False
        bot_username = str(config.env_get("TELEGRAM_BOT_USERNAME", "") or "").strip().lstrip("@")
        self._bot_username = (
            bot_username
            if bot_username.isascii() and all(char.isalnum() or char == "_" for char in bot_username)
            else ""
        )
        self._bot_username_checked = bool(self._bot_username)
        enabled_raw = (config.env_get("ENABLE_TELEGRAM_CHANNELS", "false") or "false")
        self._channel_monitor_enabled = (
            enabled_raw.strip().lower() in {"1", "true", "yes", "on"}
            and bool(config.telegram_channels)
        )
        self._channel_sources = {
            str(channel["username"]).strip().lstrip("@").casefold(): channel
            for channel in config.telegram_channels
            if channel.get("username")
        } if self._channel_monitor_enabled else {}

    def bind_health(self, health) -> None:
        self.health = health

    def on_recipient_ready(self, callback) -> None:
        self._on_ready_callbacks.append(callback)

    def bind_news_chat(self, handler, enabled: bool = True) -> None:
        self._news_chat_handler = handler if callable(handler) else None
        self._news_chat_enabled = bool(enabled and self._news_chat_handler)

    def _resolve_bot_username(self) -> str | None:
        if self._bot_username:
            return self._bot_username
        if self._bot_username_checked or not self.token or self.dry_run:
            return None
        self._bot_username_checked = True
        try:
            result = self._call("getMe", {}, timeout=10, retries=1)
            username = str((result or {}).get("username") or "").strip().lstrip("@")
            if username and username.isascii() and all(char.isalnum() or char == "_" for char in username):
                self._bot_username = username
                return username
        except Exception as exc:  # noqa: BLE001
            # خطای مجوز دائمی است؛ بقیه‌ی خطاها در درخواست بعدی دوباره امتحان می‌شوند.
            self._bot_username_checked = (
                isinstance(exc, TelegramError) and exc.status_code == 401
            )
            log.warning("نام کاربری بات از تلگرام دریافت نشد؛ دکمه‌ی گفت‌وگو از مسیر جایگزین استفاده می‌کند: %s", exc)
        return None

    def poll_channel_posts(self) -> list[RawItem]:
        return self.db.pending_telegram_posts(
            limit=100,
            max_age_hours=float(self.cfg.get("poll.telegram_queue_max_age_hours", 12)),
        )

    def ack_channel_post(self, update_key: str) -> None:
        self.db.ack_telegram_post(update_key)

    # ── زیرساخت ──────────────────────────────────────────────
    @property
    def configured(self) -> bool:
        return bool(self.token)

    @property
    def listener_enabled(self) -> bool:
        return bool(self.token and not self.dry_run)

    @property
    def listener_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def chat_id(self, explicit: str | None = None) -> str | None:
        return explicit or self.cfg.telegram_chat_id or self.db.kv_get("chat_id")

    def _call(self, method: str, payload: dict, timeout: int = 30, retries: int = 3):
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN تنظیم نشده است")

        url = f"https://api.telegram.org/bot{self.token}/{method}"
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                response = requests.post(url, json=payload, timeout=timeout)
            except requests.RequestException as exc:
                last_err = exc
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise TelegramError(f"خطای شبکه در تماس با تلگرام ({method}): {exc}") from exc

            try:
                data = response.json()
            except ValueError as exc:
                last_err = exc
                if response.status_code >= 500 and attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise TelegramError(
                    f"پاسخ نامعتبر تلگرام: {response.status_code}",
                    status_code=response.status_code,
                ) from exc

            if data.get("ok"):
                return data.get("result")

            code = int(data.get("error_code") or response.status_code or 0)
            description = data.get("description", "خطای نامشخص")
            retry_after = float((data.get("parameters") or {}).get("retry_after", 0) or 0)

            # ادیت تکراری خطا نیست
            if "message is not modified" in description.lower():
                return None

            # خطای قالب HTML: بدون parse_mode دوباره بفرست تا خبر حیاتی نسوزد
            if code == 400 and "can't parse entities" in description.lower() and "parse_mode" in payload:
                plain_payload = {k: v for k, v in payload.items() if k != "parse_mode"}
                plain_payload["text"] = util.strip_html(str(payload.get("text", "")))
                payload = plain_payload
                continue

            # محدودیت نرخ تلگرام (429) یا خطای موقت ۵xx
            if code == 429 and attempt < retries - 1:
                wait = min(max(retry_after, 1.0), 30.0)
                log.warning("محدودیت نرخ تلگرام (429) — مکث %.1f ثانیه", wait)
                time.sleep(wait)
                continue
            if code >= 500 and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue

            raise TelegramError(description, status_code=code, retry_after=retry_after)

        raise TelegramError(f"تماس با تلگرام ناموفق ماند: {last_err}")

    # ── ارسال ────────────────────────────────────────────────
    def send(
        self,
        text: str,
        chat_id: str | None = None,
        reply_to: int | None = None,
        silent: bool = False,
        keyboard: list | None = None,
    ) -> int | None:
        target = self.chat_id(chat_id)
        if self.dry_run or not self.token:
            _print_dry(text, target)
            return None
        if not target:
            log.warning("گیرنده‌ای ثبت نشده — یک بار در بات /start بزنید یا TELEGRAM_CHAT_ID را در .env بگذارید")
            return None

        chunks = _split_message(text)
        first_id: int | None = None
        continuation_ids: list[int] = []
        continuation_parent = reply_to
        for index, chunk in enumerate(chunks):
            payload = {
                "chat_id": target,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            }
            if continuation_parent:
                payload["reply_parameters"] = {
                    "message_id": continuation_parent,
                    "allow_sending_without_reply": True,
                }
            # شناسه‌ی ثبت‌شده در رخداد باید به پیام اول اشاره کند؛ دکمه‌ها هم روی همان پیام باشند.
            if keyboard and index == 0:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            result = self._call("sendMessage", payload)
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if index == 0:
                first_id = message_id
                if first_id:
                    continuation_parent = first_id
            elif message_id:
                continuation_ids.append(int(message_id))
            if index < len(chunks) - 1:
                time.sleep(0.4)
        if first_id and continuation_ids:
            self._message_continuations[int(first_id)] = continuation_ids
        return first_id

    def edit(self, message_id: int, text: str, chat_id: str | None = None, keyboard: list | None = None) -> None:
        target = self.chat_id(chat_id)
        if self.dry_run or not self.token:
            print(f"[DRY] ویرایش پیام {message_id}: {text[:80]}…")
            return
        if not target or not message_id:
            return

        chunks = _split_message(text)
        payload = {
            "chat_id": target,
            "message_id": message_id,
            "text": chunks[0],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        self._call("editMessageText", payload)

        old_continuations = self._message_continuations.pop(int(message_id), [])
        failed_deletions: list[int] = []
        for old_id in old_continuations:
            try:
                self._call("deleteMessage", {"chat_id": target, "message_id": old_id})
            except TelegramError as exc:
                failed_deletions.append(old_id)
                log.debug("حذف ادامه‌ی قدیمی پیام %d ناموفق بود: %s", old_id, exc)

        new_continuations: list[int] = failed_deletions
        if failed_deletions:
            self._message_continuations[int(message_id)] = failed_deletions
        for index, chunk in enumerate(chunks[1:], start=1):
            result = self._call(
                "sendMessage",
                {
                    "chat_id": target,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "disable_notification": True,
                    "reply_parameters": {
                        "message_id": int(message_id),
                        "allow_sending_without_reply": True,
                    },
                },
            )
            continuation_id = result.get("message_id") if isinstance(result, dict) else None
            if continuation_id:
                new_continuations.append(int(continuation_id))
                self._message_continuations[int(message_id)] = new_continuations
            if index < len(chunks) - 1:
                time.sleep(0.4)
        if new_continuations:
            self._message_continuations[int(message_id)] = new_continuations

    def answer_callback(
        self,
        callback_id: str,
        text: str,
        show_alert: bool = False,
        url: str | None = None,
    ) -> None:
        if self.dry_run or not self.token:
            return
        payload = {
            "callback_query_id": callback_id,
            "text": text,
            "show_alert": show_alert,
        }
        if url:
            payload["url"] = url
        try:
            self._call("answerCallbackQuery", payload)
        except TelegramError as exc:
            log.debug("answerCallbackQuery: %s", exc)

    # ── کیبورد ───────────────────────────────────────────────
    def news_chat_keyboard(self, event_id: int, url: str = "") -> list:
        rows = []
        if self._news_chat_enabled:
            username = self._resolve_bot_username()
            if username:
                chat_url = f"https://t.me/{username}?start=chat_{int(event_id)}"
                rows.append([{"text": "💬 گفت‌وگو درباره‌ی خبر", "url": chat_url}])
            else:
                rows.append([{
                    "text": "💬 گفت‌وگو درباره‌ی خبر",
                    "callback_data": f"chat:{int(event_id)}",
                }])
        if url.startswith("http"):
            rows.append([{"text": "🔗 متن اصلی", "url": url}])
        return rows

    def feedback_keyboard(self, event_id: int, url: str = "") -> list:
        rows = [[
            {"text": "👍 مفید", "callback_data": f"fb:up:{event_id}"},
            {"text": "👎 بی‌ربط", "callback_data": f"fb:down:{event_id}"},
        ]]
        rows.extend(self.news_chat_keyboard(event_id, url))
        return rows

    # ── شنونده ───────────────────────────────────────────────
    def start_listener(self) -> None:
        if not self.listener_enabled:
            log.info("شنونده‌ی تلگرام غیرفعال است (توکن ندارد یا dry-run است)")
            return
        if self._thread and self._thread.is_alive():
            return
        if self._news_chat_enabled:
            self._resolve_bot_username()
        self._thread = threading.Thread(
            target=self._listen_loop, name="telegram-listener", daemon=True
        )
        self._thread.start()
        log.info("شنونده‌ی تلگرام فعال شد")

    def _listen_loop(self) -> None:
        offset = int(self.db.kv_get("tg_offset", "0") or 0)
        while not self._stop.is_set():
            try:
                allowed_updates = ["message", "callback_query"]
                if self._channel_monitor_enabled:
                    allowed_updates.append("channel_post")
                updates = self._call(
                    "getUpdates",
                    {"offset": offset, "timeout": 25, "allowed_updates": allowed_updates},
                    timeout=40,
                    retries=1,
                )
                if self.health:
                    self.health.mark_poll("telegram_bot")
                prune_sessions = getattr(self.db, "prune_news_chat_sessions", None)
                if callable(prune_sessions):
                    try:
                        prune_sessions(
                            float(self.cfg.get("news_chat.session_ttl_minutes", 120))
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.debug("پاک‌سازی نشست‌های چت خبری ناموفق بود: %s", exc)
            except TelegramError as exc:
                if exc.status_code == 409:
                    log.error(
                        "تداخل تلگرام (409 Conflict): یک نمونه‌ی دیگر از همین بات همزمان روشن است! "
                        "فقط یک نمونه باید با این توکن اجرا شود."
                    )
                    self._stop.wait(10)
                elif exc.status_code in (401, 404):
                    log.error("توکن تلگرام نامعتبر یا باطل شده است (%s): %s", exc.status_code, exc)
                    self._stop.wait(30)
                else:
                    log.warning("getUpdates ناموفق: %s", exc)
                    self._stop.wait(5)
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("getUpdates خطای غیرمنتظره داد: %s", exc)
                self._stop.wait(5)
                continue

            for update in updates or []:
                next_offset = int(update["update_id"]) + 1
                try:
                    # channel_post پیش از تأیید offset در SQLite صف می‌شود؛ با ری‌استارت
                    # پاد، نه خبر گم می‌شود و نه آپدیت پیش از ذخیره‌سازی تأیید می‌شود.
                    self._handle_update(update)
                    self.db.kv_set("tg_offset", str(next_offset))
                    offset = next_offset
                except Exception:  # noqa: BLE001
                    log.exception("پردازش/ثبت آپدیت تلگرام ناموفق؛ برای تلاش مجدد offset جلو نمی‌رود")
                    self._stop.wait(2)
                    break

    def _handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return
        channel_post = update.get("channel_post")
        if channel_post:
            self._handle_channel_post(channel_post, update.get("update_id"))
            return
        message = update.get("message")
        if not message:
            return
        chat_id = str(message["chat"]["id"])
        sender = message.get("from") or {}
        user_id = str(sender.get("id") or chat_id)
        text = (message.get("text") or "").strip()
        parts = text.split(maxsplit=1)
        command = parts[0].split("@")[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command == "/start":
            if argument.startswith("chat_"):
                if (message.get("chat") or {}).get("type") != "private":
                    self.send("برای حفظ حریم خصوصی، گفت‌وگوی خبری را در پیام خصوصی بات ادامه بده.", chat_id=chat_id)
                    return
                event_id_text = argument.removeprefix("chat_").strip()
                if event_id_text.isdigit():
                    self._start_news_chat(chat_id, user_id, int(event_id_text))
                else:
                    self.send("پیوند گفت‌وگوی خبر معتبر نیست.", chat_id=chat_id)
                return
            # شروع گفت‌وگوی خبری از deep-link نباید مقصد انتشار عمومی را عوض کند.
            self.db.kv_set("chat_id", chat_id)
            self.send(
                "✅ ثبت شد.\nاین بات اخبار حریم هوایی ایران، فرودگاه‌های مهرآباد و امام خمینی "
                "و ایرلاین‌های مرتبط را ۲۴ ساعته رصد می‌کند و همان لحظه برایت می‌فرستد.\n\n"
                "دستورها:\n"
                "/status — وضعیت زنده و سلامت منابع\n"
                "/last — آخرین خبرهای ثبت‌شده\n"
                "/hashtags — هشتگ‌های پایش‌شده\n"
                "💬 برای پرسش درباره‌ی خبر، دکمه‌ی «گفت‌وگو درباره‌ی خبر» را بزن\n"
                "/stopchat — پایان گفت‌وگوی خبری\n"
                "/wrong — روی خبر اشتباه ریپلای کن و بزن تا اصلاح شود\n"
                "/help — راهنما",
                chat_id=chat_id,
            )
            for cb in self._on_ready_callbacks:
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    log.debug("اجرای callback پس از /start ناموفق بود")
            return

        if command in ("/help", "/start@") or command == "/راهنما":
            self.send(
                "هر خبر همراه با منبع اصلی و برچسب اطمینان می‌آید.\n"
                "خبرهای هم‌موضوع در یک رشته می‌آیند؛ پیام اول همیشه به‌روز می‌شود.\n"
                "/status — وضعیت زنده و سلامت فیدها\n"
                "/last — ۵ رخداد آخر\n"
                "/hashtags — هشتگ‌های پایش‌شده\n"
                "💬 دکمه‌ی «گفت‌وگو درباره‌ی خبر» زیر هر خبر چت را شروع می‌کند\n"
                "/stopchat — پایان گفت‌وگوی خبری\n"
                "/wrong — ریپلای روی خبر اشتباه",
                chat_id=chat_id,
            )
            return

        if command in ("/status", "/stats"):
            stats = self.db.stats()
            src_health = self.db.sources_health_summary()
            lines = [
                "🟢 <b>ربات فعال است</b>",
                f"📊 رخدادها: {util.fa_digits(stats['events'])} | "
                f"منتشرشده: {util.fa_digits(stats['published'])} | "
                f"آیتم‌ها: {util.fa_digits(stats['items'])}",
                f"📡 منابع سالم: {util.fa_digits(src_health['ok'])} | "
                f"خطادار: {util.fa_digits(len(src_health['failing']))}",
                (
                    "🌐 ترجمه: فعال"
                    if self.cfg.llm_api_key
                    else "⚠️ ترجمه‌ی خبرهای غیرفارسی متوقف است؛ LLM_API_KEY را تنظیم کنید"
                ),
            ]
            if self.health:
                snap = self.health.snapshot()
                uptime_min = snap["uptime_seconds"] // 60
                lines.append(f"⏱ زمان فعالیت فعلی: {util.fa_digits(uptime_min)} دقیقه")
                rss_ts = snap.get("last_poll_epoch", {}).get("rss")
                if rss_ts:
                    dt = datetime.fromtimestamp(rss_ts, tz=timezone.utc)
                    lines.append(f"🔄 آخرین پایش RSS: {util.format_moment(dt)}")
            if src_health["failing"]:
                bad = "، ".join(f"{sid}({cnt})" for sid, cnt in src_health["failing"][:5])
                lines.append(f"⚠️ فیدهای دارای خطا: {html.escape(bad)}")
            self.send("\n".join(lines), chat_id=chat_id)
            return

        if command == "/last":
            events = self.db.latest_events(limit=5)
            if not events:
                self.send("هنوز رخدادی در دیتابیس ثبت نشده است.", chat_id=chat_id)
                return
            lines = ["🗂 <b>آخرین رخدادهای ثبت‌شده:</b>", ""]
            for ev in events:
                icon = formatter.SEVERITY_ICON.get(ev["severity"], "⚪")
                title = html.escape(util.truncate(ev["title_fa"] or "بدون تیتر", 80))
                when = util.short_time(ev["first_seen"])
                url = ev["primary_url"] or ""
                if url.startswith("http"):
                    lines.append(f'{icon} [{when}] <a href="{html.escape(url)}">{title}</a>')
                else:
                    lines.append(f"{icon} [{when}] {title}")
            self.send("\n".join(lines), chat_id=chat_id)
            return

        if command in ("/hashtags", "/tags"):
            fixed = self.cfg.get("social.seed_hashtags", []) or []
            discovered = self.db.discovered_hashtags(
                min_mentions=int(self.cfg.get("social.hashtag_min_mentions", 2)),
                min_sources=int(self.cfg.get("social.hashtag_min_sources", 2)),
                days=int(self.cfg.get("social.hashtag_window_days", 30)),
                limit=int(self.cfg.get("social.max_discovered_hashtags", 24)),
            )
            lines = ["🏷 <b>هشتگ‌های پایش</b>", "", "<b>ثابت:</b>"]
            lines.extend(f"#{html.escape(str(tag).lstrip('#'))}" for tag in fixed[:30])
            lines.extend(["", "<b>کشف‌شده از منابع مرتبط:</b>"])
            lines.extend(f"#{html.escape(tag)}" for tag in discovered)
            if not discovered:
                lines.append("هنوز هشتگ تازه‌ای از چند منبع مستقل تأیید نشده است.")
            self.send("\n".join(lines), chat_id=chat_id)
            return

        if command in {"/stopchat", "/endchat"}:
            self.db.clear_news_chat_session(chat_id, user_id)
            self.send("گفت‌وگوی خبری پایان یافت. برای شروع دوباره، دکمه‌ی زیر خبر را بزن.", chat_id=chat_id)
            return

        if command == "/chat":
            if (message.get("chat") or {}).get("type") != "private":
                self.send("برای حفظ حریم خصوصی، گفت‌وگوی خبری فقط در پیام خصوصی بات انجام می‌شود.", chat_id=chat_id)
                return
            if not argument.isdigit():
                self.send("برای شروع، دکمه‌ی «گفت‌وگو درباره‌ی خبر» زیر خبر را بزن.", chat_id=chat_id)
                return
            self._start_news_chat(chat_id, user_id, int(argument))
            return

        if command == "/wrong":
            replied = message.get("reply_to_message")
            if not replied:
                self.send("روی همان پیام خبری که اشتباه است ریپلای کن و /wrong بزن.", chat_id=chat_id)
                return
            event = self.db.find_event_by_message(chat_id, int(replied["message_id"]))
            if not event:
                self.send("این پیام به هیچ رخدادی وصل نیست.", chat_id=chat_id)
                return
            self.db.mark_corrected(int(event["id"]))
            updated = self.db.get_event(int(event["id"]))
            self.edit(
                int(replied["message_id"]),
                formatter.format_event(updated, self.cfg),
                chat_id=chat_id,
            )
            self.send("❌ خبر اصلاح شد و منبعش برای بازبینی علامت خورد.", chat_id=chat_id)
            return

        self._handle_news_chat_message(chat_id, user_id, text)

    def _send_news_chat_message(self, text: str, chat_id: str) -> bool:
        try:
            self.send(text, chat_id=chat_id)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("ارسال پیام گفت‌وگوی خبری به تلگرام ناموفق بود: %s", exc)
            return False

    def _start_news_chat(self, chat_id: str, user_id: str, event_id: int) -> bool:
        if not self._news_chat_enabled or not self._news_chat_handler:
            self._send_news_chat_message("گفت‌وگوی هوشمند فعلاً فعال نیست؛ تنظیم LLM را بررسی کنید.", chat_id)
            return False
        event = self.db.get_event(int(event_id))
        if not event:
            self._send_news_chat_message("خبر موردنظر دیگر در دسترس نیست.", chat_id)
            return False

        title = html.escape(str(event["title_fa"] or "خبر بدون تیتر"))
        intro = (
            f"💬 <b>گفت‌وگو درباره‌ی خبر {util.fa_digits(event_id)}</b>\n"
            f"<i>{title}</i>\n\n"
            "سؤالت را همین‌جا بفرست؛ پاسخ فقط بر اساس متن همین خبر و گفت‌وگوی اخیر خواهد بود.\n"
            "برای پاسخ، متن خبر و پرسش‌ها به سرویس LLM تنظیم‌شده فرستاده می‌شوند.\n"
            "پایان گفت‌وگو: /stopchat"
        )
        if not self._send_news_chat_message(intro, chat_id):
            return False
        self.db.save_news_chat_session(chat_id, user_id, int(event_id), [])
        return True

    def _handle_news_chat_message(self, chat_id: str, user_id: str, text: str) -> bool:
        if not self._news_chat_enabled or not text or text.startswith("/"):
            return False
        ttl = float(self.cfg.get("news_chat.session_ttl_minutes", 120))
        session = self.db.get_news_chat_session(chat_id, user_id, max_age_minutes=ttl)
        if not session:
            return False
        if not callable(self._news_chat_handler):
            self.db.clear_news_chat_session(chat_id, user_id)
            self._send_news_chat_message("گفت‌وگوی هوشمند فعلاً فعال نیست؛ تنظیم LLM را بررسی کنید.", chat_id)
            return True

        max_question_chars = max(100, int(self.cfg.get("news_chat.max_question_chars", 2000)))
        if len(text) > max_question_chars:
            self._send_news_chat_message(
                f"سؤال خیلی بلند است؛ لطفاً آن را به کمتر از {util.fa_digits(max_question_chars)} نویسه برسان.",
                chat_id,
            )
            return True

        event = self.db.get_event(int(session["event_id"]))
        if not event:
            self.db.clear_news_chat_session(chat_id, user_id)
            self._send_news_chat_message("خبر دیگر در دسترس نیست؛ گفت‌وگو پایان یافت.", chat_id)
            return True

        history = session.get("history") or []
        self._send_news_chat_message("⏳ سؤال دریافت شد؛ دارم بر اساس متن خبر پاسخ می‌دهم…", chat_id)
        try:
            answer = self._news_chat_handler(dict(event), text, history)
        except Exception as exc:  # noqa: BLE001
            log.warning("پاسخ LLM برای گفت‌وگوی رخداد %d ناموفق بود: %s", session["event_id"], exc)
            self._send_news_chat_message("پاسخ فعلاً آماده نشد. کمی بعد دوباره تلاش کن.", chat_id)
            return True

        history = [
            *history,
            {"role": "user", "content": text},
            {"role": "assistant", "content": str(answer)},
        ][-12:]
        self.db.save_news_chat_session(chat_id, user_id, int(session["event_id"]), history)
        self._send_news_chat_message(
            f"💬 پاسخ درباره‌ی خبر {util.fa_digits(session['event_id'])}:\n\n"
            f"{html.escape(str(answer))}\n\nبرای پایان گفت‌وگو: /stopchat",
            chat_id,
        )
        return True

    def _handle_news_chat_callback(self, callback: dict, event_id: int) -> None:
        callback_id = callback.get("id", "")
        sender = callback.get("from") or {}
        user_id = str(sender.get("id") or "")
        if not user_id:
            self.answer_callback(callback_id, "شناسه‌ی کاربر در دسترس نیست.", show_alert=True)
            return

        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") == "private":
            chat_id = str(chat.get("id") or user_id)
            try:
                started = self._start_news_chat(chat_id, user_id, event_id)
            except TelegramError as exc:
                log.warning("شروع گفت‌وگوی رخداد %d در DM تلگرام ناموفق بود: %s", event_id, exc)
                started = False
            self.answer_callback(
                callback_id,
                "گفت‌وگو در همین چت شروع شد." if started else "گفت‌وگو شروع نشد.",
                show_alert=not started,
            )
            return

        # اگر پیام خبر در کانال/گروه است، ادامه‌ی گفت‌وگو را به DM کاربر می‌بریم.
        try:
            started = self._start_news_chat(user_id, user_id, event_id)
            if not started:
                self.answer_callback(callback_id, "گفت‌وگو شروع نشد.", show_alert=True)
                return
            self.answer_callback(callback_id, "پیام خصوصی بات را بررسی کن.")
        except TelegramError:
            username = self._resolve_bot_username()
            url = f"https://t.me/{username}?start=chat_{event_id}" if username else None
            self.answer_callback(
                callback_id,
                "برای ادامه، گفت‌وگو را در پیام خصوصی بات شروع کن.",
                show_alert=True,
                url=url,
            )

    def _handle_channel_post(self, message: dict, update_id: int | str | None = None) -> None:
        if not self._channel_monitor_enabled:
            return
        chat = message.get("chat") or {}
        username = str(chat.get("username") or "").strip().lstrip("@").casefold()
        source = self._channel_sources.get(username)
        if not source:
            return

        text = (message.get("text") or message.get("caption") or "").strip()
        if not text:
            return
        message_id = message.get("message_id")
        if not message_id:
            return
        try:
            published_at = datetime.fromtimestamp(int(message["date"]), tz=timezone.utc)
        except (KeyError, TypeError, ValueError, OSError):
            published_at = util.now_utc()
        max_age = max(1.0, float(self.cfg.get("poll.telegram_queue_max_age_hours", 12)))
        if published_at < util.now_utc() - timedelta(hours=max_age):
            return

        channel_name = str(source.get("name") or chat.get("title") or f"@{username}")
        source_url = f"https://t.me/{username}"
        item = RawItem(
            source_id=str(source.get("id") or f"tg_{username}"),
            source_name=channel_name,
            source_url=source_url,
            title=text.splitlines()[0][:200],
            body=text,
            url=f"{source_url}/{message_id}",
            lang=str(source.get("lang") or "auto"),
            country=str(source.get("country") or "INTL"),
            kind="telegram",
            published_at=published_at,
            extra={
                "tier": int(source.get("tier", 1)),
                "channel": username,
                "collected_at": util.iso(util.now_utc()),
            },
        )
        update_key = (
            f"tg-update:{update_id}"
            if update_id is not None
            else f"tg-post:{username}:{message_id}"
        )
        self.db.enqueue_telegram_post(update_key, item)

    def _handle_callback(self, callback: dict) -> None:
        data = callback.get("data") or ""
        callback_id = callback.get("id", "")
        parts = data.split(":")
        if len(parts) == 2 and parts[0] == "chat":
            if parts[1].isdigit():
                self._handle_news_chat_callback(callback, int(parts[1]))
            else:
                self.answer_callback(callback_id, "شناسه‌ی خبر نامعتبر است.", show_alert=True)
            return
        if len(parts) != 3 or parts[0] != "fb":
            self.answer_callback(callback_id, "نامشخص")
            return
        verdict, event_id_text = parts[1], parts[2]
        if verdict not in {"up", "down"} or not event_id_text.isdigit():
            self.answer_callback(callback_id, "نامشخص")
            return
        self.db.add_feedback(int(event_id_text), verdict)
        self.answer_callback(
            callback_id,
            "ثبت شد ✅" if verdict == "up" else "ثبت شد — این منبع بازبینی می‌شود",
        )

    def stop(self) -> None:
        self._stop.set()


def _split_message(text: str) -> list[str]:
    """تقسیم کامل متن به قطعات مجاز؛ حتی یک پاراگراف/خط بلند هم بریده نمی‌شود."""
    if len(text) <= MAX_LEN:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > MAX_LEN:
        cut = MAX_LEN
        newline = remaining.rfind("\n", 0, MAX_LEN)
        space = remaining.rfind(" ", 0, MAX_LEN)
        if newline >= MAX_LEN // 2:
            cut = newline + 1
        elif space >= MAX_LEN // 2:
            cut = space + 1

        # متن‌های قالب‌بندی‌شده‌ی برنامه ممکن است HTML entity یا تگ کوتاه داشته باشند.
        # مرز قطعه را به وسط آن‌ها نمی‌بریم تا Telegram کل پیام را بدون خطای parse بپذیرد.
        tag_start = remaining.rfind("<", 0, cut)
        tag_end = remaining.rfind(">", 0, cut)
        if tag_start > tag_end:
            cut = tag_start

        entity_start = remaining.rfind("&", 0, cut)
        if entity_start >= 0 and ";" not in remaining[entity_start:cut]:
            suffix = remaining[entity_start:cut]
            if suffix[1:].replace("#", "").isalnum() or suffix == "&":
                cut = entity_start

        if cut < max(1, MAX_LEN // 2):
            cut = MAX_LEN
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]

    if remaining:
        chunks.append(remaining)
    return chunks


def _print_dry(text: str, target: str | None) -> None:
    line = "─" * 70
    print(f"\n{line}\n[DRY-RUN → {target or 'بدون گیرنده'}]\n{line}")
    print(text)
    print(line)
