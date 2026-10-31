"""کالکتورها: RSS، کانال‌های تلگرام، توییتر و اینستاگرام.

همه یک قرارداد دارند: poll() -> list[RawItem]
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlparse

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

MAX_FEED_BYTES = (
    2 * 1024 * 1024
)  # سقف ۲ مگابایت برای جلوگیری از مصرف بی‌حد رم یا قفل شدن سوکت
BARE_INDEX_PATHS = {"", "/", "/blog", "/news", "/feed", "/rss"}


class RssCollector:
    def __init__(self, config, db):
        self.cfg = config
        self.db = db
        self.max_age_hours = float(config.get("poll.max_item_age_hours", 12))
        self.per_feed = int(config.get("poll.max_items_per_feed", 15))
        self.max_body_chars = int(config.get("poll.max_feed_body_chars", 1200))
        self.max_workers = max(1, int(config.get("poll.rss_workers", 6)))
        self.skip_url_parts = [
            part.lower() for part in (config.get("poll.skip_url_parts", []) or [])
        ]

    def _should_skip(self, link: str, source_url: str = "") -> bool:
        low = (link or "").strip().lower()
        if not low:
            return False
        if source_url and low.rstrip("/") == source_url.strip().lower().rstrip("/"):
            return True
        if any(part in low for part in self.skip_url_parts):
            return True
        parsed = urlparse(low)
        if parsed.path.rstrip("/") in BARE_INDEX_PATHS and not parsed.query:
            return True
        return False

    def poll(self, sources: list[dict] | None = None) -> list[RawItem]:
        sources = self.cfg.rss_sources if sources is None else sources
        if not sources:
            return []

        items: list[RawItem] = []
        workers = min(self.max_workers, len(sources))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="rss-worker"
        ) as pool:
            future_map = {
                pool.submit(self._fetch, source): source for source in sources
            }
            for future in as_completed(future_map):
                source = future_map[future]
                try:
                    fetched = future.result()
                    items.extend(fetched)
                    if self.db:
                        self.db.mark_source_ok(source["id"])
                except Exception as exc:  # noqa: BLE001
                    log.warning("فید %s خوانده نشد: %s", source.get("id"), exc)
                    if self.db:
                        self.db.mark_source_error(source["id"], str(exc))
        return items

    def _read_bounded(
        self, url: str, timeout: tuple[float, float] = (8.0, 18.0)
    ) -> bytes:
        with requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            },
            timeout=timeout,
            stream=True,
        ) as response:
            response.raise_for_status()
            buf = bytearray()
            for chunk in response.iter_content(chunk_size=32768):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) >= MAX_FEED_BYTES:
                    break
            return bytes(buf)

    def _fetch(self, source: dict) -> list[RawItem]:
        raw_bytes = self._read_bounded(source["url"])
        parsed = feedparser.parse(raw_bytes)
        cutoff = util.now_utc() - timedelta(hours=self.max_age_hours)

        out: list[RawItem] = []
        entry_limit = int(source.get("max_items_per_feed", self.per_feed))
        for entry in parsed.entries[:entry_limit]:
            published = _entry_time(entry)
            if published and published < cutoff:
                continue
            title = util.strip_html(entry.get("title", "")).strip()
            body = _entry_body(entry, self.max_body_chars)
            raw_link = (entry.get("link") or "").strip()
            if raw_link and self._should_skip(raw_link, source["url"]):
                continue
            link = raw_link or source["url"]
            if not title and not body:
                continue
            watched = [
                str(tag).strip().lstrip("#")
                for tag in (source.get("watched_hashtags") or [])
                if str(tag).strip()
            ]
            hashtags = list(dict.fromkeys(
                [f"#{tag}" for tag in watched]
                + extract_hashtags(f"{title} {body}")
            ))[:6]
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
                    extra={
                        "tier": source.get("tier", 2),
                        "hashtags": hashtags,
                        # RSSHub hashtag routes are known topic signals even when
                        # the platform omits the searched tag from its feed text.
                        "watched_hashtags": watched,
                        "airport_hashtag_feed": bool(source.get("airport_hashtag_feed", False)),
                    },
                )
            )
        return out


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def _entry_body(entry, limit: int = 1200) -> str:
    """خلاصه‌ی فید تا سقف limit کاراکتر (سقف قابل تنظیم: poll.max_feed_body_chars).

    اگر فید متن کامل داشته باشد همان می‌آید؛ اگر بریده شود «…» می‌ماند و
    پایپ‌لاین متن کامل را از لینک خبر می‌گیرد (poll.fetch_full_article).
    """
    if entry.get("content"):
        raw = entry["content"][0].get("value", "")
    else:
        raw = entry.get("summary", "") or entry.get("description", "")
    text = util.strip_html(raw)
    text = " ".join(text.split())
    return util.truncate(text, limit)


class TelegramChannelCollector:
    """کانال‌های تلگرام به‌صورت رویدادمحور (تأخیر ۱ تا ۵ ثانیه).

    نیاز به `telethon` و TELEGRAM_API_ID/HASH دارد. بات باید عضو کانال باشد.
    """

    def __init__(self, config):
        self.cfg = config
        self.queue: queue.Queue[RawItem] = queue.Queue()
        self._thread: threading.Thread | None = None
        self.max_body_chars = int(config.get("poll.max_telegram_body_chars", 1200))
        self.watched = load_watchlist(config)
        self.enabled = _flag_direct(config, "ENABLE_TELEGRAM_CHANNELS") and bool(
            config.telegram_channels
        )

    def start(self) -> None:
        if not self.enabled:
            return
        try:
            import telethon  # noqa: F401
        except ImportError:
            log.warning("telethon نصب نیست — کالکتور تلگرام غیرفعال شد")
            self.enabled = False
            return
        self._thread = threading.Thread(
            target=self._run, name="telegram-collector", daemon=True
        )
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
        session = (
            self.cfg.env_get("TELEGRAM_SESSION", "data/telegram") or "data/telegram"
        )
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
            body = util.truncate(text, self.max_body_chars)
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
                    published_at=message.date.astimezone(timezone.utc)
                    if message.date
                    else util.now_utc(),
                    extra={
                        "tier": 1,
                        "hashtags": extract_hashtags(text),
                        "watched_hashtags": watched_hashtags(text, self.watched),
                    },
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
        self.max_body_chars = int(config.get("poll.max_x_body_chars", 1000))
        self.watched = load_watchlist(config)
        self.accounts = [
            a for a in (config.env_get("X_ACCOUNTS", "") or "").split(",") if a
        ]
        self._api = None

    def queries(self) -> list[str]:
        watch = [
            a for a in self.cfg.airlines if a.get("country") in {"TR", "AE", "RU", "CN"}
        ][: int(self.cfg.get("poll.x_max_queries", 5))]
        out = []
        for airline in watch:
            name = airline["name"]
            out.append(
                f'("{name}" OR "{airline.get("iata", "")}") '
                "(Iran OR Tehran OR IKA OR THR OR OIIX OR airspace) -filter:replies"
            )
        out.append(
            '(OIIE OR OIII OR OIIX OR "Tehran FIR") (closed OR NOTAM OR airspace) -filter:replies'
        )
        # کوئری هشتگی: همه‌ی هشتگ‌های تحت پایش در یک جست‌وجو
        if self.watched:
            clauses = " OR ".join(f"#{tag}" for tag in self.watched)
            out.append(f"({clauses}) -filter:replies")
        # کوئری هشتگی از فهرست config/sources.yaml (بخش hashtags) —
        # فقط وقتی فهرست پایش تنظیمات خالی است تا کوئری هشتگی تکراری ساخته نشود
        if not self.watched:
            hashtags_cfg = (self.cfg.sources or {}).get("hashtags") or {}
            tags = [
                str(tag)
                for group in ("iran", "aviation")
                for tag in (hashtags_cfg.get(group) or [])
                if str(tag).isascii()
            ]
            if tags:
                out.append(
                    f"({' OR '.join(tags[:8])}) (Iran OR Tehran OR OIIX OR NOTAM) -filter:replies"
                )
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
                        await api.pool.add_account(
                            parts[0], parts[1], parts[2], ":".join(parts[3:])
                        )
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
                tweet_text = tweet.rawContent or ""
                out.append(
                    RawItem(
                        source_id=f"x_{tweet.user.username}",
                        source_name=f"@{tweet.user.username}",
                        source_url=f"https://x.com/{tweet.user.username}",
                        title=tweet_text.split("\n", 1)[0][:200],
                        body=util.truncate(tweet_text, self.max_body_chars),
                        url=f"https://x.com/{tweet.user.username}/status/{tweet.id}",
                        lang="en",
                        country="INTL",
                        kind="x",
                        published_at=when,
                        extra={
                            "tier": 1,
                            "likes": tweet.likeCount,
                            "reposts": tweet.retweetCount,
                            "hashtags": extract_hashtags(tweet_text),
                            "watched_hashtags": watched_hashtags(
                                tweet_text, self.watched
                            ),
                        },
                    )
                )
        return out


class GdeltCollector:
    """پروژه‌ی متن‌باز GDELT — نمایه‌ی لحظه‌ای اخبار جهان، بدون کلید API.

    مکمل RSS است: به‌جای خواندن تک‌تک فیدها، مستقیم با کوئری دقیق
    «ایران + حریم هوایی/فرودگاه/پرواز» از API رایگان GDELT می‌پرسد،
    برای همین ذاتاً روی موضوع اصلی متمرکز است.
    """

    API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
    QUERY = (
        '(Iran OR Tehran OR "Iranian airspace" OR "Tehran FIR" OR OIIE OR OIII OR OIIX) '
        "(airspace OR NOTAM OR airport OR flights OR airline OR aviation)"
    )
    _LANG_MAP = {
        "English": "en", "Persian": "fa", "Russian": "ru",
        "Chinese": "zh", "Turkish": "tr", "Arabic": "ar",
    }

    def __init__(self, config):
        self.cfg = config
        self.enabled = bool(config.get("gdelt.enabled", False))
        self.timespan_hours = int(config.get("gdelt.timespan_hours", 3))
        self.max_records = int(config.get("gdelt.max_records", 25))

    def poll(self) -> list[RawItem]:
        if not self.enabled:
            return []
        try:
            response = requests.get(
                self.API_URL,
                params={
                    "query": self.QUERY,
                    "mode": "ArtList",
                    "format": "json",
                    "maxrecords": self.max_records,
                    "timespan": f"{self.timespan_hours}h",
                    "sort": "DateDesc",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            response.raise_for_status()
            return self._to_items(response.json())
        except Exception as exc:  # noqa: BLE001
            log.warning("GDELT خوانده نشد: %s", exc)
            return []

    def _to_items(self, payload: dict) -> list[RawItem]:
        out: list[RawItem] = []
        seen_urls: set[str] = set()
        for article in (payload or {}).get("articles", []) or []:
            url = (article.get("url") or "").strip()
            title = util.strip_html(article.get("title", "")).strip()
            if not url or not title or url in seen_urls:
                continue
            seen_urls.add(url)
            domain = (article.get("domain") or "gdelt").strip()
            source_id = "gdelt_" + "".join(
                ch if ch.isalnum() else "_" for ch in domain.lower()
            ).strip("_")
            published = None
            raw_date = article.get("seendate") or ""
            try:
                published = datetime.strptime(raw_date, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
            except (ValueError, TypeError):
                pass
            out.append(
                RawItem(
                    source_id=source_id or "gdelt",
                    source_name=domain,
                    source_url=f"https://{domain}",
                    title=title[:200],
                    body=title,
                    url=url,
                    lang=self._LANG_MAP.get(article.get("language", ""), "en"),
                    country="INTL",
                    kind="gdelt",
                    published_at=published,
                    extra={"tier": 1},
                )
            )
        return out


def _flag_direct(config, name: str) -> bool:
    raw = config.env_get(name, "false") or "false"
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def extract_hashtags(text: str) -> list[str]:
    """هشتگ‌های #abc را از متن استخراج می‌کند."""
    if not text:
        return []
    return re.findall(r"#[\w\u0600-\u06FF]+", text)


def load_watchlist(config) -> list[str]:
    """هشتگ‌های تحت پایش از تنظیمات (hashtags.watchlist)؛ خاموشی سراسری محترم است."""
    if not config.get("hashtags.enabled", False):
        return []
    out: list[str] = []
    for entry in config.get("hashtags.watchlist", []) or []:
        tag = entry.get("tag") if isinstance(entry, dict) else entry
        tag = str(tag or "").strip().lstrip("#")
        if tag and tag not in out:
            out.append(tag)
    return out


def watched_hashtags(text: str, watchlist: list[str]) -> list[str]:
    """کدام هشتگ‌های تحت پایش در متن هستند؟ (مقایسه‌ی بدون # و با نرمال‌سازی)"""
    if not text or not watchlist:
        return []
    present = {util.normalize(tag.lstrip("#")) for tag in extract_hashtags(text)}
    return [
        tag for tag in watchlist if util.normalize(tag.lstrip("#")) in present
    ]


class InstagramHashtagCollector:
    """پایش هشتگ در اینستاگرام (اختیاری، پیش‌فرض خاموش).

    ⚠️ اسکرپینگ اینستاگرام خلاف شرایط سرویس آن است و می‌تواند حساب/آی‌پی را
    مسدود کند — همان هشدار کالکتور X. فعال‌سازی: ENABLE_INSTAGRAM=true و
    ترجیحاً INSTAGRAM_USERNAME/PASSWORD (بدون لاگین، اینستاگرام بعد از چند
    درخواست محدود می‌کند). هشتگ‌ها از hashtags.watchlist خوانده می‌شوند.
    """

    def __init__(self, config):
        self.cfg = config
        self.watched = load_watchlist(config)
        self.max_posts = int(config.get("hashtags.instagram.max_posts_per_tag", 12))
        self.lang = str(config.get("hashtags.instagram.lang", "en"))
        self.max_body_chars = int(config.get("poll.max_instagram_body_chars", 2200))
        self.max_age_hours = float(config.get("poll.max_item_age_hours", 12))
        self.enabled = _flag_direct(config, "ENABLE_INSTAGRAM") and bool(self.watched)

    def poll(self) -> list[RawItem]:
        if not self.enabled:
            return []
        try:
            loader = self._make_loader()
        except Exception as exc:  # noqa: BLE001
            log.warning("اینستاگرام در دسترس نیست — این چرخه رد شد: %s", exc)
            return []
        cutoff = util.now_utc() - timedelta(hours=self.max_age_hours)
        out: list[RawItem] = []
        for tag in self.watched:
            try:
                posts = self._tag_posts(loader, tag)
            except Exception as exc:  # noqa: BLE001
                log.debug("هشتگ اینستاگرام #%s خوانده نشد: %s", tag, exc)
                posts = []
            for post in posts:
                when = (
                    post.date_utc.astimezone(timezone.utc) if post.date_utc else None
                )
                if when and when < cutoff:
                    continue
                caption = (post.caption or "").strip()
                if not caption:
                    continue
                out.append(
                    RawItem(
                        # source_id ثابت: یک پست که از دو هشتگ بیاید فقط یک‌بار ثبت شود
                        source_id="instagram",
                        source_name=f"Instagram #{tag}",
                        source_url=f"https://www.instagram.com/explore/tags/{tag}/",
                        title=caption.split("\n", 1)[0][:200],
                        body=util.truncate(caption, self.max_body_chars),
                        url=f"https://www.instagram.com/p/{post.shortcode}/",
                        lang=self.lang,
                        country="INTL",
                        kind="instagram",
                        published_at=when,
                        extra={
                            "tier": 2,
                            "hashtags": extract_hashtags(caption),
                            "watched_hashtags": watched_hashtags(caption, self.watched),
                        },
                    )
                )
            time.sleep(2)  # فاصله‌ی ملایم بین تگ‌ها برای جلوگیری از محدودسازی
        return out

    def _make_loader(self):
        import instaloader

        loader = instaloader.Instaloader(
            quiet=True,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            download_geotags=False,
            download_pic_comments=False,
        )
        username = self.cfg.env_get("INSTAGRAM_USERNAME", "") or ""
        password = self.cfg.env_get("INSTAGRAM_PASSWORD", "") or ""
        if username and password:
            try:
                loader.login(username, password)
            except Exception as exc:  # noqa: BLE001
                log.warning("لاگین اینستاگرام ناموفق — ناشناس ادامه می‌دهیم: %s", exc)
        return loader

    def _tag_posts(self, loader, tag: str) -> list:
        from instaloader import Hashtag

        hashtag = Hashtag.from_name(loader.context, tag)
        posts: list = []
        for post in hashtag.get_posts():
            posts.append(post)
            if len(posts) >= self.max_posts:
                break
        return posts


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
    instagram = InstagramHashtagCollector(config)

    hashtag_sources = [
        source for source in config.rss_sources if source.get("airport_hashtag_feed")
    ]
    regular_sources = [
        source for source in config.rss_sources if not source.get("airport_hashtag_feed")
    ]
    jobs = [
        CollectorJob(
            "rss",
            int(config.get("poll.rss_seconds", 75)),
            lambda: rss.poll(regular_sources),
        )
    ]
    if hashtag_sources:
        jobs.append(
            CollectorJob(
                "social_hashtags",
                int(config.get("poll.social_hashtag_seconds", 30)),
                lambda: rss.poll(hashtag_sources),
            )
        )
    # فقط کالکتورهایی که واقعاً فعال‌اند زمان‌بندی می‌شوند تا نخ بیکار نسازند
    # و سلامت‌سنج بتواند مرگ کالکتورهای واقعی را تشخیص دهد.
    if config.get("notam.enabled", False):
        jobs.append(
            CollectorJob("notam", int(config.get("poll.notam_seconds", 60)), notam.poll)
        )
    if telegram.enabled:
        jobs.append(
            CollectorJob(
                "telegram",
                int(config.get("poll.telegram_queue_seconds", 10)),
                telegram.poll,
            )
        )
    if x.enabled:
        jobs.append(CollectorJob("x", int(config.get("poll.x_seconds", 90)), x.poll))
    gdelt = GdeltCollector(config)
    if gdelt.enabled:
        jobs.append(
            CollectorJob("gdelt", int(config.get("gdelt.seconds", 300)), gdelt.poll)
        )
    if instagram.enabled:
        jobs.append(
            CollectorJob(
                "instagram",
                int(config.get("hashtags.instagram.seconds", 900)),
                instagram.poll,
            )
        )
    return jobs


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
