"""کالکتورها: RSS، کانال‌های تلگرام، و توییتر.

همه یک قرارداد دارند: poll() -> list[RawItem]
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import calendar
from datetime import datetime, timedelta, timezone
from typing import Callable

import feedparser
import requests

from . import util
from .models import RawItem
from .notam import NotamCollector

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class RssCollector:
    def __init__(self, config, db):
        self.cfg = config
        self.db = db
        self.max_age_hours = float(config.get("poll.max_item_age_hours", 12))
        self.per_feed = int(config.get("poll.max_items_per_feed", 15))
        self.skip_url_parts = [
            part.lower() for part in (config.get("poll.skip_url_parts", []) or [])
        ]

    def _should_skip(self, link: str) -> bool:
        low = (link or "").lower()
        return any(part in low for part in self.skip_url_parts)

    def poll(self) -> list[RawItem]:
        items: list[RawItem] = []
        for source in self.cfg.rss_sources:
            try:
                items.extend(self._fetch(source))
                self.db.mark_source_ok(source["id"])
            except Exception as exc:  # noqa: BLE001
                log.warning("فید %s خوانده نشد: %s", source.get("id"), exc)
                self.db.mark_source_error(source["id"], str(exc))
        return items

    def _fetch(self, source: dict) -> list[RawItem]:
        response = requests.get(
            source["url"],
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
            timeout=25,
        )
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        cutoff = util.now_utc() - timedelta(hours=self.max_age_hours)

        out: list[RawItem] = []
        for entry in parsed.entries[: self.per_feed]:
            published = _entry_time(entry)
            if published and published < cutoff:
                continue
            title = util.strip_html(entry.get("title", "")).strip()
            body = _entry_body(entry)
            link = entry.get("link", "") or source["url"]
            if self._should_skip(link):
                continue
            if not title and not body:
                continue
            out.append(
                RawItem(
                    source_id=source["id"],
                    source_name=source["name"],
                    source_url=source["url"],
                    title=title,
                    body=body,
                    url=link,
                    lang=source.get("lang", "en"),
                    country=source.get("country", "INTL"),
                    kind="rss",
                    published_at=published,
                    extra={"tier": source.get("tier", 2)},
                )
            )
        return out


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def _entry_body(entry) -> str:
    if entry.get("content"):
        raw = entry["content"][0].get("value", "")
    else:
        raw = entry.get("summary", "") or entry.get("description", "")
    text = util.strip_html(raw)
    text = " ".join(text.split())
    return util.truncate(text, 1200)


class TelegramChannelCollector:
    """کانال‌های تلگرام به‌صورت رویدادمحور (تأخیر ۱ تا ۵ ثانیه).

    نیاز به `telethon` و TELEGRAM_API_ID/HASH دارد. بات باید عضو کانال باشد.
    """

    def __init__(self, config):
        self.cfg = config
        self.queue: queue.Queue[RawItem] = queue.Queue()
        self._thread: threading.Thread | None = None
        self.enabled = _flag_direct(config, "ENABLE_TELEGRAM_CHANNELS") and bool(config.telegram_channels)

    def start(self) -> None:
        if not self.enabled:
            return
        try:
            import telethon  # noqa: F401
        except ImportError:
            log.warning("telethon نصب نیست — کالکتور تلگرام غیرفعال شد")
            self.enabled = False
            return
        self._thread = threading.Thread(target=self._run, name="telegram-collector", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # noqa: BLE001
            log.error("کالکتور تلگرام متوقف شد: %s", exc)

    async def _main(self) -> None:
        from telethon import TelegramClient, events

        api_id = int(self.cfg.env_get("TELEGRAM_API_ID", "0") or 0)
        api_hash = self.cfg.env_get("TELEGRAM_API_HASH", "") or ""
        session = self.cfg.env_get("TELEGRAM_SESSION", "data/telegram") or "data/telegram"
        if not api_id or not api_hash:
            log.warning("TELEGRAM_API_ID/HASH تنظیم نشده — کالکتور تلگرام غیرفعال")
            return

        client = TelegramClient(session, api_id, api_hash)
        channels = [c["username"] for c in self.cfg.telegram_channels]
        log.info("اتصال به تلگرام برای %d کانال", len(channels))

        @client.on(events.NewMessage(chats=channels))
        async def handler(event):  # noqa: ANN001
            message = event.message
            text = (message.message or "").strip()
            if not text:
                return
            username = getattr(event.chat, "username", None)
            url = (
                f"https://t.me/{username}/{message.id}"
                if username
                else f"https://t.me/c/{event.chat_id}/{message.id}"
            )
            title = text.split("\n", 1)[0][:200]
            body = util.truncate(text, 1200)
            self.queue.put(
                RawItem(
                    source_id=f"tg_{username or event.chat_id}",
                    source_name=getattr(event.chat, "title", "Telegram"),
                    source_url=f"https://t.me/{username}" if username else "",
                    title=title,
                    body=body,
                    url=url,
                    lang="fa",
                    country="INTL",
                    kind="telegram",
                    published_at=message.date.astimezone(timezone.utc) if message.date else util.now_utc(),
                    extra={"tier": 1},
                )
            )

        await client.start()
        await client.run_until_disconnected()

    def poll(self) -> list[RawItem]:
        items: list[RawItem] = []
        while True:
            try:
                items.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return items


class XCollector:
    """جست‌وجوی توییتر با twscrape.

    ⚠️ خلاف شرایط سرویس X است و می‌تواند حساب/آی‌پی را مسدود کند.
    پیش‌فرض خاموش است. مسئولیتش با کسی است که فعالش می‌کند.
    """

    def __init__(self, config, airline_index):
        self.cfg = config
        self.airlines = airline_index
        self.enabled = _flag_direct(config, "ENABLE_X")
        self.accounts = [a for a in (config.env_get("X_ACCOUNTS", "") or "").split(",") if a]
        self._api = None

    def queries(self) -> list[str]:
        watch = [
            a for a in self.cfg.airlines
            if a.get("country") in {"TR", "AE", "RU", "CN"}
        ][: int(self.cfg.get("poll.x_max_queries", 5))]
        out = []
        for airline in watch:
            name = airline["name"]
            out.append(
                f'("{name}" OR "{airline.get("iata", "")}") '
                '(Iran OR Tehran OR IKA OR THR OR OIIX OR airspace) -filter:replies'
            )
        out.append('(OIIE OR OIII OR OIIX OR "Tehran FIR") (closed OR NOTAM OR airspace) -filter:replies')
        return out

    def poll(self) -> list[RawItem]:
        if not self.enabled:
            return []
        if not self.accounts:
            log.warning("ENABLE_X=true است ولی X_ACCOUNTS خالی است")
            return []
        try:
            return asyncio.run(self._poll_once())
        except Exception as exc:  # noqa: BLE001
            log.warning("جست‌وجوی X ناموفق: %s", exc)
            return []

    async def _poll_once(self) -> list[RawItem]:
        from twscrape import API, gather

        api = API("data/x_accounts.db")
        if not self._api:
            for line in self.accounts:
                parts = line.split(":")
                if len(parts) >= 4:
                    try:
                        await api.pool.add_account(parts[0], parts[1], parts[2], ":".join(parts[3:]))
                    except Exception as exc:  # noqa: BLE001
                        log.debug("افزودن اکانت X ناموفق: %s", exc)
            await api.pool.login_all()
            self._api = api

        cutoff = util.now_utc() - timedelta(hours=2)
        out: list[RawItem] = []
        for query in self.queries():
            try:
                tweets = await gather(self._api.search(query, limit=10))
            except Exception as exc:  # noqa: BLE001
                log.debug("کوئری ناموفق: %s", exc)
                continue
            for tweet in tweets:
                when = tweet.date.replace(tzinfo=timezone.utc) if tweet.date else None
                if when and when < cutoff:
                    continue
                if tweet.retweetedTweet:
                    continue
                out.append(
                    RawItem(
                        source_id=f"x_{tweet.user.username}",
                        source_name=f"@{tweet.user.username}",
                        source_url=f"https://x.com/{tweet.user.username}",
                        title=(tweet.rawContent or "").split("\n", 1)[0][:200],
                        body=util.truncate(tweet.rawContent or "", 1000),
                        url=f"https://x.com/{tweet.user.username}/status/{tweet.id}",
                        lang="en",
                        country="INTL",
                        kind="x",
                        published_at=when,
                        extra={"tier": 1, "likes": tweet.likeCount, "reposts": tweet.retweetCount},
                    )
                )
        return out


def _flag_direct(config, name: str) -> bool:
    raw = config.env_get(name, "false") or "false"
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class CollectorJob:
    def __init__(self, name: str, interval: int, fn: Callable[[], list[RawItem]]):
        self.name = name
        self.interval = interval
        self.fn = fn


def build_jobs(config, db, airline_index) -> list[CollectorJob]:
    rss = RssCollector(config, db)
    notam = NotamCollector(config)
    telegram = TelegramChannelCollector(config)
    telegram.start()
    x = XCollector(config, airline_index)

    return [
        CollectorJob("rss", int(config.get("poll.rss_seconds", 75)), rss.poll),
        CollectorJob("notam", int(config.get("poll.notam_seconds", 60)), notam.poll),
        CollectorJob(
            "telegram",
            int(config.get("poll.telegram_queue_seconds", 10)),
            telegram.poll,
        ),
        CollectorJob("x", int(config.get("poll.x_seconds", 90)), x.poll),
    ]


def check_sources(config, db) -> int:
    """سلامت فیدها را بررسی می‌کند. تعداد خرابی‌ها را برمی‌گرداند."""
    failures = 0
    print(f"{'منبع':<32} {'وضعیت':<10} جزئیات")
    print("-" * 80)
    for source in config.rss_sources:
        try:
            response = requests.get(
                source["url"], headers={"User-Agent": USER_AGENT}, timeout=20
            )
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            count = len(parsed.entries)
            status = "OK" if count else "خالی"
            if not count:
                failures += 1
            print(f"{source['name'][:30]:<32} {status:<10} {count} آیتم")
            db.mark_source_ok(source["id"])
        except Exception as exc:  # noqa: BLE001
            failures += 1
            db.mark_source_error(source["id"], str(exc))
            print(f"{source['name'][:30]:<32} {'خراب':<10} {type(exc).__name__}: {exc}")
    return failures
