"""ارسال به تلگرام و دریافت بازخورد (بدون کتابخانه‌ی واسط، مستقیم روی Bot API)."""

from __future__ import annotations

from datetime import datetime, timezone
import html
import json
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
        self._conflict_count = 0
        self.chat_responder = None  # Pipeline ست می‌کند: (event, question, history) -> str
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
        """گیرنده‌ی اصلی (اولین شناسه) — برای سازگاری با کدهای قدیمی."""
        if explicit:
            return str(explicit)
        configured = (self.cfg.telegram_chat_id or "").split(",")[0].strip()
        return configured or self.db.kv_get("chat_id")

    # ── گیرندگان (چندنفره) ────────────────────────────────────
    def recipients(self) -> list[str]:
        """همه‌ی گیرنده‌های خبر، به‌ترتیب و بدون تکرار.

        اولویت: شناسه‌های ثابت محیط (TELEGRAM_CHAT_ID، چندتا با کاما)،
        بعد مشترکانی که با /start ثبت شده‌اند، بعد گیرنده‌ی قدیمی تکی.
        """
        ids: list[str] = []

        def add(raw) -> None:
            for part in str(raw or "").split(","):
                part = part.strip()
                if part and part not in ids:
                    ids.append(part)

        add(self.cfg.telegram_chat_id)
        try:
            stored = json.loads(self.db.kv_get("chat_ids", "[]") or "[]")
        except (ValueError, TypeError):
            stored = []
        if isinstance(stored, list):
            for item in stored:
                add(item)
        add(self.db.kv_get("chat_id"))
        return ids

    def subscribe(self, chat_id: str) -> bool:
        """ثبت یک مشترک تازه؛ True اگر قبلاً عضو نبوده."""
        chat_id = str(chat_id)
        try:
            stored = json.loads(self.db.kv_get("chat_ids", "[]") or "[]")
            if not isinstance(stored, list):
                stored = []
        except (ValueError, TypeError):
            stored = []
        stored = [str(item) for item in stored]
        is_new = chat_id not in stored and chat_id not in self.recipients()
        if chat_id not in stored:
            stored.append(chat_id)
            self.db.kv_set("chat_ids", json.dumps(stored))
        # اولین مشترک، گیرنده‌ی «اصلی» قدیمی هم می‌شود تا مسیرهای قدیمی نشکنند
        if not self.db.kv_get("chat_id"):
            self.db.kv_set("chat_id", chat_id)
        return is_new

    def unsubscribe(self, chat_id: str) -> bool:
        """لغو اشتراک؛ True اگر عضو بوده و حذف شد."""
        chat_id = str(chat_id)
        try:
            stored = json.loads(self.db.kv_get("chat_ids", "[]") or "[]")
            if not isinstance(stored, list):
                stored = []
        except (ValueError, TypeError):
            stored = []
        stored = [str(item) for item in stored]
        was_member = chat_id in stored or self.db.kv_get("chat_id") == chat_id
        if chat_id in stored:
            stored.remove(chat_id)
            self.db.kv_set("chat_ids", json.dumps(stored))
        if self.db.kv_get("chat_id") == chat_id:
            remaining = self.recipients()
            remaining = [item for item in remaining if item != chat_id]
            if remaining:
                self.db.kv_set("chat_id", remaining[0])
            else:
                self.db.kv_delete("chat_id")
        return was_member

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

    def edit(self, message_id: int, text: str, chat_id: str | None = None, keyboard: list | None = None) -> None:
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

    def answer_callback(self, callback_id: str, text: str, show_alert: bool = False) -> None:
        if self.dry_run or not self.token:
            return
        try:
            self._call(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": text, "show_alert": show_alert},
            )
        except TelegramError as exc:
            log.debug("answerCallbackQuery: %s", exc)

    # ── کیبورد ───────────────────────────────────────────────
    def feedback_keyboard(self, event_id: int, url: str = "") -> list:
        rows = [[
            {"text": "👍 مفید", "callback_data": f"fb:up:{event_id}"},
            {"text": "👎 بی‌ربط", "callback_data": f"fb:down:{event_id}"},
        ], [
            {"text": "💬 گفت‌وگو درباره‌ی خبر", "callback_data": f"ask:{event_id}"},
        ]]
        if url.startswith("http"):
            rows.append([{"text": "🔗 متن اصلی", "url": url}])
        return rows

    # ── گفت‌وگو درباره‌ی خبر ──────────────────────────────────
    CHAT_TTL_SECONDS = 2 * 3600

    def set_chat_responder(self, responder) -> None:
        self.chat_responder = responder

    def _activate_chat(self, chat_id: str, event_id: int) -> None:
        self.db.kv_set(f"chat_event:{chat_id}", str(event_id))
        self.db.kv_set(f"chat_since:{chat_id}", str(int(time.time())))

    def _deactivate_chat(self, chat_id: str) -> None:
        self.db.kv_delete(f"chat_event:{chat_id}")
        self.db.kv_delete(f"chat_since:{chat_id}")

    def _active_chat_event(self, chat_id: str) -> int | None:
        raw = self.db.kv_get(f"chat_event:{chat_id}")
        if not raw:
            return None
        since = int(self.db.kv_get(f"chat_since:{chat_id}", "0") or 0)
        if since and time.time() - since > self.CHAT_TTL_SECONDS:
            self._deactivate_chat(chat_id)
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _chat_history(self, chat_id: str, event_id: int) -> list:
        try:
            history = json.loads(
                self.db.kv_get(f"chat_history:{chat_id}:{event_id}", "[]") or "[]"
            )
        except (ValueError, TypeError):
            return []
        return history if isinstance(history, list) else []

    def _save_chat_turn(self, chat_id: str, event_id: int, question: str, answer: str) -> None:
        history = self._chat_history(chat_id, event_id)
        history.append({"q": question[:500], "a": answer[:1500]})
        self.db.kv_set(
            f"chat_history:{chat_id}:{event_id}",
            json.dumps(history[-6:], ensure_ascii=False),
        )

    def _route_chat_message(self, chat_id: str, text: str) -> bool:
        """اگر کاربر در حالت گفت‌وگوست، پرسش را به LLM می‌دهد. True یعنی مصرف شد."""
        event_id = self._active_chat_event(chat_id)
        if event_id is None:
            return False
        event = self.db.get_event(event_id)
        if not event:
            self._deactivate_chat(chat_id)
            self.send("آن خبر دیگر در دسترس نیست؛ حالت گفت‌وگو بسته شد.", chat_id=chat_id)
            return True
        if self.chat_responder is None:
            self.send(
                "قابلیت گفت‌وگو در این نمونه فعال نیست (کلید LLM تنظیم نشده).",
                chat_id=chat_id,
            )
            return True
        history = self._chat_history(chat_id, event_id)
        answer = self.chat_responder(event, text, history)
        self._save_chat_turn(chat_id, event_id, text, answer)
        self.send(answer, chat_id=chat_id)
        return True

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
                    {"offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]},
                    timeout=40,
                    retries=1,
                )
                if self.health:
                    self.health.mark_poll("telegram_bot")
                if self._conflict_count:
                    log.info("تداخل تلگرام برطرف شد؛ شنونده به حالت عادی برگشت")
                    self._conflict_count = 0
            except TelegramError as exc:
                # تلگرام جواب داده (حتی با خطا)؛ پس نخ شنونده زنده و در حال
                # پیشرفت است. اگر اینجا mark_poll نکنیم، سلامت‌سنج بعد از
                # چند دقیقه پاد را «مرده» حساب می‌کند و OpenShift بی‌دلیل
                # ری‌استارتش می‌کند — در حالی که ری‌استارت، تداخلِ یک نمونه‌ی
                # خارجی را حل نمی‌کند و فقط چرخه‌ی ری‌استارت می‌سازد.
                if self.health:
                    self.health.mark_poll("telegram_bot")
                if exc.status_code == 409:
                    self._conflict_count += 1
                    wait = min(10 * self._conflict_count, 60)
                    if self._conflict_count == 1:
                        log.error(
                            "تداخل تلگرام (409 Conflict): یک نمونه‌ی دیگر از همین بات همزمان روشن است! "
                            "فقط یک نمونه باید با این توکن اجرا شود. جزئیات تلگرام: %s",
                            exc,
                        )
                    else:
                        log.warning(
                            "تداخل تلگرام (409) همچنان ادامه دارد (تلاش %d)؛ نمونه‌ی دیگر را متوقف کنید. تلاش بعدی %d ثانیه دیگر.",
                            self._conflict_count,
                            wait,
                        )
                    self._stop.wait(wait)
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

        if command in ("/endchat", "/cancel"):
            self._deactivate_chat(chat_id)
            self.send("گفت‌وگو پایان یافت.", chat_id=chat_id)
            return

        # در حالت گفت‌وگو، پیام متنیِ بدون دستور پرسشِ کاربر درباره‌ی خبر است
        if text and not text.startswith("/"):
            if self._route_chat_message(chat_id, text):
                return

        if command == "/start":
            # چندگیرنده: ثبت مشترک تازه، گیرنده‌های قبلی را حذف نمی‌کند
            self.subscribe(chat_id)
            self.send(
                "✅ ثبت شدی.\nاین بات اخبار حریم هوایی ایران، فرودگاه‌های مهرآباد و امام خمینی "
                "و ایرلاین‌های مرتبط را ۲۴ ساعته رصد می‌کند و همان لحظه برایت می‌فرستد.\n\n"
                "دستورها:\n"
                "/status — وضعیت زنده و سلامت منابع\n"
                "/last — آخرین خبرهای ثبت‌شده\n"
                "/wrong — روی خبر اشتباه ریپلای کن و بزن تا اصلاح شود\n"
                "💬 با دکمه‌ی «گفت‌وگو درباره‌ی خبر» زیر هر خبر، می‌توانی درباره‌اش از LLM بپرسی\n"
                "/stop — لغو دریافت خبرها\n"
                "/help — راهنما",
                chat_id=chat_id,
            )
            for cb in self._on_ready_callbacks:
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    log.debug("اجرای callback پس از /start ناموفق بود")
            return

        if command == "/stop":
            if self.unsubscribe(chat_id):
                self.send(
                    "اشتراکت لغو شد؛ دیگر خبری برایت ارسال نمی‌شود. برای ثبت دوباره /start بزن.",
                    chat_id=chat_id,
                )
            else:
                self.send("اشتراک فعالی نداشتی.", chat_id=chat_id)
            return

        if command in ("/help", "/start@") or command == "/راهنما":
            self.send(
                "هر خبر همراه با منبع اصلی و برچسب اطمینان می‌آید.\n"
                "خبرهای هم‌موضوع در یک رشته می‌آیند؛ پیام اول همیشه به‌روز می‌شود.\n"
                "/status — وضعیت زنده و سلامت فیدها\n"
                "/last — ۵ رخداد آخر\n"
                "/wrong — ریپلای روی خبر اشتباه\n"
                "/stop — لغو دریافت خبرها",
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

    def _handle_callback(self, callback: dict) -> None:
        data = callback.get("data") or ""
        callback_id = callback.get("id", "")
        parts = data.split(":")

        if len(parts) == 2 and parts[0] == "ask":
            chat = (callback.get("message") or {}).get("chat") or {}
            sender = callback.get("from") or {}
            chat_id = str(chat.get("id") or sender.get("id") or "")
            try:
                event_id = int(parts[1])
            except ValueError:
                event_id = 0
            event = self.db.get_event(event_id) if event_id else None
            if not event or not chat_id:
                self.answer_callback(callback_id, "این خبر دیگر در دسترس نیست")
                return
            self._activate_chat(chat_id, event_id)
            self.answer_callback(callback_id, "حالت گفت‌وگو فعال شد 💬")
            title = html.escape(util.truncate(event["title_fa"] or "خبر", 90))
            self.send(
                f"💬 <b>گفت‌وگو درباره‌ی این خبر فعال شد:</b>\n{title}\n\n"
                "هر سؤالی درباره‌ی همین خبر داری بپرس؛ پاسخ فقط بر اساس متن خبر و منابعش داده می‌شود.\n"
                "برای پایان گفت‌وگو: /endchat",
                chat_id=chat_id,
            )
            return
        if len(parts) != 3 or parts[0] != "fb":
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
