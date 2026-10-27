"""ارسال به تلگرام و دریافت بازخورد (بدون کتابخانه‌ی واسط، مستقیم روی Bot API)."""

from __future__ import annotations

from datetime import datetime, timezone
import html
import logging
import threading
import time

import requests

from . import formatter, util

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

    def bind_health(self, health) -> None:
        self.health = health

    def on_recipient_ready(self, callback) -> None:
        self._on_ready_callbacks.append(callback)

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
                raise TelegramError(
                    f"خطای شبکه در تماس با تلگرام ({method}): {exc}"
                ) from exc

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
            retry_after = float(
                (data.get("parameters") or {}).get("retry_after", 0) or 0
            )

            # ادیت تکراری خطا نیست
            if "message is not modified" in description.lower():
                return None

            # خطای قالب HTML: بدون parse_mode دوباره بفرست تا خبر حیاتی نسوزد
            if (
                code == 400
                and "can't parse entities" in description.lower()
                and "parse_mode" in payload
            ):
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
            log.warning(
                "گیرنده‌ای ثبت نشده — یک بار در بات /start بزنید یا TELEGRAM_CHAT_ID را در .env بگذارید"
            )
            return None

        chunks = _split_message(text)
        last_id: int | None = None
        for index, chunk in enumerate(chunks):
            payload = {
                "chat_id": target,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            }
            if reply_to and index == 0:
                payload["reply_parameters"] = {
                    "message_id": reply_to,
                    "allow_sending_without_reply": True,
                }
            if keyboard and index == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            result = self._call("sendMessage", payload)
            if isinstance(result, dict):
                last_id = result.get("message_id")
            if index < len(chunks) - 1:
                time.sleep(0.4)
        return last_id

    def edit(
        self,
        message_id: int,
        text: str,
        chat_id: str | None = None,
        keyboard: list | None = None,
    ) -> None:
        target = self.chat_id(chat_id)
        if self.dry_run or not self.token:
            print(f"[DRY] ویرایش پیام {message_id}: {text[:80]}…")
            return
        if not target or not message_id:
            return
        payload = {
            "chat_id": target,
            "message_id": message_id,
            "text": text[:MAX_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        self._call("editMessageText", payload)

    def answer_callback(
        self, callback_id: str, text: str, show_alert: bool = False
    ) -> None:
        if self.dry_run or not self.token:
            return
        try:
            self._call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_id,
                    "text": text,
                    "show_alert": show_alert,
                },
            )
        except TelegramError as exc:
            log.debug("answerCallbackQuery: %s", exc)

    # ── کیبورد ───────────────────────────────────────────────
    def feedback_keyboard(self, event_id: int, url: str = "") -> list:
        rows = [
            [
                {"text": "👍 مفید", "callback_data": f"fb:up:{event_id}"},
                {"text": "👎 بی‌ربط", "callback_data": f"fb:down:{event_id}"},
                {"text": "💬 گفتگو", "callback_data": f"chat:open:{event_id}"},
            ]
        ]
        if url.startswith("http"):
            rows.append([{"text": "🔗 متن اصلی", "url": url}])
        return rows

    # ── شنونده ───────────────────────────────────────────────
    def start_listener(self) -> None:
        if not self.listener_enabled:
            log.info("شنونده‌ی تلگرام غیرفعال است (توکن ندارد یا dry-run است)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._listen_loop, name="telegram-listener", daemon=True
        )
        self._thread.start()
        log.info("شنونده‌ی تلگرام فعال شد")

    def _listen_loop(self) -> None:
        offset = int(self.db.kv_get("tg_offset", "0") or 0)
        while not self._stop.is_set():
            try:
                updates = self._call(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 25,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    timeout=40,
                    retries=1,
                )
                if self.health:
                    self.health.mark_poll("telegram_bot")
            except TelegramError as exc:
                if exc.status_code == 409:
                    log.error(
                        "تداخل تلگرام (409 Conflict): یک نمونه‌ی دیگر از همین بات همزمان روشن است! "
                        "فقط یک نمونه باید با این توکن اجرا شود."
                    )
                    self._stop.wait(10)
                elif exc.status_code in (401, 404):
                    log.error(
                        "توکن تلگرام نامعتبر یا باطل شده است (%s): %s",
                        exc.status_code,
                        exc,
                    )
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
                offset = int(update["update_id"]) + 1
                self.db.kv_set("tg_offset", str(offset))
                try:
                    self._handle_update(update)
                except Exception:  # noqa: BLE001
                    log.exception("پردازش آپدیت تلگرام ناموفق")

    def _handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return
        message = update.get("message")
        if not message:
            return
        chat_id = str(message["chat"]["id"])
        text = (message.get("text") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text else ""

        if command == "/start":
            self.db.kv_set("chat_id", chat_id)
            self.send(
                "✅ ثبت شد.\nاین بات اخبار حریم هوایی ایران، فرودگاه‌های مهرآباد و امام خمینی "
                "و ایرلاین‌های مرتبط را ۲۴ ساعته رصد می‌کند و همان لحظه برایت می‌فرستد.\n\n"
                "دستورها:\n"
                "/status — وضعیت زنده و سلامت منابع\n"
                "/last — آخرین خبرهای ثبت‌شده\n"
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
                bad = "، ".join(
                    f"{sid}({cnt})" for sid, cnt in src_health["failing"][:5]
                )
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
                    lines.append(
                        f'{icon} [{when}] <a href="{html.escape(url)}">{title}</a>'
                    )
                else:
                    lines.append(f"{icon} [{when}] {title}")
            self.send("\n".join(lines), chat_id=chat_id)
            return

        if command == "/wrong":
            replied = message.get("reply_to_message")
            if not replied:
                self.send(
                    "روی همان پیام خبری که اشتباه است ریپلای کن و /wrong بزن.",
                    chat_id=chat_id,
                )
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
            self.send(
                "❌ خبر اصلاح شد و منبعش برای بازبینی علامت خورد.", chat_id=chat_id
            )
            return

        if command == "/stop_chat":
            self.db.kv_set(f"chat_event:{chat_id}", "")
            self.db.kv_set(f"chat_session:{chat_id}", "")
            self.send("✅ جلسه‌ی گفتگو پایان یافت.", chat_id=chat_id)
            return

        if command == "/chat":
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                self.send("استفاده: /chat <شماره_خبر>\nمثال: /chat 42", chat_id=chat_id)
                return
            try:
                event_id = int(parts[1])
            except ValueError:
                self.send("شماره‌ی الخبر معتبر نیست.", chat_id=chat_id)
                return
            event = self.db.get_event(event_id)
            if not event:
                self.send("خبر پیدا نشد.", chat_id=chat_id)
                return
            session_id = self.db.get_or_create_chat_session(event_id, chat_id)
            self.db.kv_set(f"chat_event:{chat_id}", str(event_id))
            self.db.kv_set(f"chat_session:{chat_id}", str(session_id))
            title = event["title_fa"] or "بدون تیتر"
            self.send(
                f"💬 شروع گفتگو درباره‌ی: {title}\n"
                f"سوال خود را بفرستید. برای پایان /stop_chat بزنید.",
                chat_id=chat_id,
            )
            return

        # ── چت فعال با LLM ──
        active_event = self.db.kv_get(f"chat_event:{chat_id}")
        if active_event and text and not text.startswith("/"):
            self._handle_chat_message(chat_id, active_event, text)
            return

    def _handle_chat_message(
        self, chat_id: str, event_id_str: str, user_text: str
    ) -> None:
        """پاسخ به پیام چت کاربر با استفاده از LLM و 백그ард الخبر."""
        try:
            event_id = int(event_id_str)
        except ValueError:
            return

        event = self.db.get_event(event_id)
        if not event:
            self.send("خبر پیدا نشد.", chat_id=chat_id)
            return

        session_id_str = self.db.kv_get(f"chat_session:{chat_id}")
        session_id = int(session_id_str) if session_id_str else None

        # ذخیره‌ی پیام کاربر
        if session_id:
            self.db.add_chat_message(session_id, "user", user_text)

        # ساخت پرامپت با 백그ард الخبر
        title = event["title_fa"] or event.get("title", "") or "بدون تیتر"
        body = event["body_fa"] or ""
        category = event.get("category", "context")
        severity = event.get("severity", "context")
        source = event["primary_source"] or "نامشخص"
        url = event["primary_url"] or ""

        system_prompt = (
            "شخصی یک تحلیلگر هوانوردی و خبرهٔ حریم هوایی ایران هستید. "
            "کاربر در مورد یک خبر خاص با شما صحبت می‌کند. "
            "فقط بر اساس اطلاعات خبر پاسخ دهید و विषय را تغییر ندهید. "
            "اگر اطلاعات کافی نیست، بگویید که در الخبر ذکر نشده است. "
            "پاسخ را به فارسی، کوتاه و مفید بنویسید (حداکثر ۳۰۰ کلمه).\n\n"
            f"خبر: {title}\n"
            f"دسته: {category}\n"
            f"شدت: {severity}\n"
            f"منبع: {source}\n"
            f"متن: {body}\n"
            f"لینک اصلی: {url}"
        )

        # استفاده از Translator (که LLM دارد) برای پاسخ چت
        try:
            from .translator import Translator

            translator = Translator(self.cfg, self.db)
            response = translator.answer_chat(system_prompt, user_text)
        except Exception as exc:
            log.warning("LLM chat failed: %s", exc)
            response = "متأسفانه در حال حاضر نمی‌توانم پاسخ دهم. لطفاً بعداً امتحان کنید."

        self.send(response, chat_id=chat_id)

        # ذخیره‌ی پاسخ助理
        if session_id:
            self.db.add_chat_message(session_id, "assistant", response)

    def _handle_callback(self, callback: dict) -> None:
        data = callback.get("data") or ""
        callback_id = callback.get("id", "")
        parts = data.split(":")
        if len(parts) < 3:
            self.answer_callback(callback_id, "نامشخص")
            return

        if parts[0] == "fb":
            if len(parts) != 3:
                self.answer_callback(callback_id, "نامشخص")
                return
            verdict, event_id = parts[1], parts[2]
            if verdict not in {"up", "down"}:
                self.answer_callback(callback_id, "نامشخص")
                return
            self.db.add_feedback(int(event_id), verdict)
            self.answer_callback(
                callback_id,
                "ثبت شد ✅" if verdict == "up" else "ثبت شد — این منبع بازبینی می‌شود",
            )
            return

        if parts[0] == "chat":
            if len(parts) != 3 or parts[1] != "open":
                self.answer_callback(callback_id, "نامشخص")
                return
            event_id = int(parts[2])
            self._start_chat(callback, callback_id, event_id)
            return

        self.answer_callback(callback_id, "نامشخص")

    def _start_chat(self, callback: dict, callback_id: str, event_id: int) -> None:
        """شروع یا ادامه‌ی جلسه‌ی چت برای یک الخبر."""
        from_user = callback.get("from", {})
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        if not chat_id:
            self.answer_callback(callback_id, "گفتگو تنها در چت شخصی 가능 است.")
            return

        session_id = self.db.get_or_create_chat_session(event_id, chat_id)
        event = self.db.get_event(event_id)
        if not event:
            self.answer_callback(callback_id, "خبر پیدا نشد.")
            return

        # پیام خوش‌آمدگویی با خلاصه‌ی الخبر
        title = event["title_fa"] or "بدون تیتر"
        body = event["body_fa"] or ""
        category = event["category"] or "context"
        source = event["primary_source"] or "نامشخص"

        welcome = (
            f"💬 <b>گفتگو درباره‌ی experts</b>\n"
            f"📰 {title}\n"
            f"📂 دسته: {category}\n"
            f"📌 منبع: {source}\n\n"
            f" qualsevol سوال درباره‌ی این خبر بپرسید (مثلاً «چرا پروازها لغو شد؟»).\n"
            f"برای پایان گفتگو /stop_chat بزنید."
        )

        self.send(welcome, chat_id=chat_id)
        self.answer_callback(callback_id, "جلسه‌ی گفتگو شروع شد ✅")

        # ذخیره‌ی event_id در kv برای استفاده در handler پیام
        self.db.kv_set(f"chat_event:{chat_id}", str(event_id))
        self.db.kv_set(f"chat_session:{chat_id}", str(session_id))

    def stop(self) -> None:
        self._stop.set()


def _split_message(text: str) -> list[str]:
    if len(text) <= MAX_LEN:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > MAX_LEN:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def _print_dry(text: str, target: str | None) -> None:
    line = "─" * 70
    print(f"\n{line}\n[DRY-RUN → {target or 'بدون گیرنده'}]\n{line}")
    print(text)
    print(line)
