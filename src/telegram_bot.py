"""ارسال به تلگرام و دریافت بازخورد (بدون کتابخانه‌ی واسط، مستقیم روی Bot API)."""

from __future__ import annotations

import logging
import threading
import time

import requests

from . import formatter

log = logging.getLogger(__name__)

MAX_LEN = 3900


class TelegramError(RuntimeError):
    pass


class TelegramBot:
    def __init__(self, config, db):
        self.cfg = config
        self.db = db
        self.token = config.telegram_token
        self.dry_run = config.dry_run
        self._stop = threading.Event()

    # ── زیرساخت ──────────────────────────────────────────────
    @property
    def configured(self) -> bool:
        return bool(self.token)

    def chat_id(self, explicit: str | None = None) -> str | None:
        return explicit or self.cfg.telegram_chat_id or self.db.kv_get("chat_id")

    def _call(self, method: str, payload: dict, timeout: int = 30):
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN تنظیم نشده است")
        response = requests.post(
            f"https://api.telegram.org/bot{self.token}/{method}",
            json=payload,
            timeout=timeout,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError(f"پاسخ نامعتبر تلگرام: {response.status_code}") from exc
        if not data.get("ok"):
            description = data.get("description", "خطای نامشخص")
            # ادیت تکراری خطا نیست
            if "message is not modified" in description.lower():
                return None
            raise TelegramError(description)
        return data.get("result")

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
            log.warning("گیرنده‌ای ثبت نشده — یک بار در بات /start بزنید")
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
        ]]
        if url.startswith("http"):
            rows.append([{"text": "🔗 متن اصلی", "url": url}])
        return rows

    # ── شنونده ───────────────────────────────────────────────
    def start_listener(self) -> None:
        if not self.token or self.dry_run:
            log.info("شنونده‌ی تلگرام غیرفعال است (توکن ندارد یا dry-run است)")
            return
        threading.Thread(target=self._listen_loop, name="telegram-listener", daemon=True).start()
        log.info("شنونده‌ی تلگرام فعال شد")

    def _listen_loop(self) -> None:
        offset = int(self.db.kv_get("tg_offset", "0") or 0)
        while not self._stop.is_set():
            try:
                updates = self._call(
                    "getUpdates",
                    {"offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]},
                    timeout=40,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("getUpdates ناموفق: %s", exc)
                time.sleep(5)
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
                "دستورها:\n/status — آمار\n/wrong — روی خبر اشتباه ریپلای کن و بزن تا اصلاح شود\n/help — راهنما",
                chat_id=chat_id,
            )
            return

        if command in ("/help", "/start@") or command == "/راهنما":
            self.send(
                "هر خبر همراه با منبع اصلی و برچسب اطمینان می‌آید.\n"
                "خبرهای هم‌موضوع در یک رشته می‌آیند؛ پیام اول همیشه به‌روز می‌شود.\n"
                "/status — آمار پردازش\n/wrong — ریپلای روی خبر اشتباه",
                chat_id=chat_id,
            )
            return

        if command in ("/status", "/stats"):
            stats = self.db.stats()
            self.send(
                f"📊 رخدادها: {stats['events']}\n"
                f"📥 آیتم‌های پردازش‌شده: {stats['items']}\n"
                f"📤 منتشرشده: {stats['published']}",
                chat_id=chat_id,
            )
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
