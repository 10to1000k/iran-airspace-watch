"""کالکتورها: RSS، کانال‌های تلگرام، و X API رسمی.

همه یک قرارداد دارند: poll() -> list[RawItem]
"""

from __future__ import annotations

import calendar
import logging
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

MAX_FEED_BYTES = 2 * 1024 * 1024  # سقف ۲ مگابایت برای جلوگیری از مصرف بی‌حد رم یا قفل شدن سوکت
BARE_INDEX_PATHS = {"", "/", "/blog", "/news", "/feed", "/rss"}


class RssCollector:
    def __init__(self, config, db):
        self.cfg = config
        self.db = db
        self.max_age_hours = float(config.get("poll.max_item_age_hours", 12))
        self.per_feed = int(config.get("poll.max_items_per_feed", 15))
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

    def poll(self) -> list[RawItem]:
        sources = self.cfg.rss_sources
        if not sources:
            return []

        items: list[RawItem] = []
        workers = min(self.max_workers, len(sources))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rss-worker") as pool:
            future_map = {pool.submit(self._fetch, source): source for source in sources}
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

    def _read_bounded(self, url: str, timeout: tuple[float, float] = (8.0, 18.0)) -> bytes:
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
        for entry in parsed.entries[: self.per_feed]:
            published = _entry_time(entry)
            if published and published < cutoff:
                continue
            title = util.strip_html(entry.get("title", "")).strip()
            body = _entry_body(entry)
            raw_link = (entry.get("link") or "").strip()
            if raw_link and self._should_skip(raw_link, source["url"]):
                continue
            link = raw_link or source["url"]
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
    """برداشت پست کانال از صندوق بادوامی که getUpdates بات پر می‌کند.

    Bot API فقط پست کانال‌هایی را می‌دهد که خود بات به آن‌ها اضافه شده باشد؛
    این کار به حساب کاربری/اسکرپینگ Telethon نیاز ندارد.
    """

    def __init__(self, config, bot=None):
        self.bot = bot
        self.channels = config.telegram_channels
        self.enabled = (
            _flag_direct(config, "ENABLE_TELEGRAM_CHANNELS")
            and bool(self.channels)
            and bool(bot and bot.listener_enabled)
        )
        if _flag_direct(config, "ENABLE_TELEGRAM_CHANNELS") and self.channels and not self.enabled:
            log.warning("پایش تلگرام نیازمند TELEGRAM_BOT_TOKEN و شنونده‌ی Bot API فعال است")

    def poll(self) -> list[RawItem]:
        return self.bot.poll_channel_posts() if self.enabled else []


class XCollector:
    """جست‌وجوی رسمی X API v2؛ نیازمند X_BEARER_TOKEN و دسترسی recent search است."""

    SEARCH_URL = "https://api.x.com/2/tweets/search/recent"

    def __init__(self, config, airline_index, db=None):
        self.cfg = config
        self.airlines = airline_index
        self.db = db
        self.token = config.env_get("X_BEARER_TOKEN", "") or ""
        self.enabled = _flag_direct(config, "ENABLE_X") and bool(self.token)
        if _flag_direct(config, "ENABLE_X") and not self.token:
            log.warning("ENABLE_X=true است ولی X_BEARER_TOKEN تنظیم نشده؛ جست‌وجوی X خاموش ماند")

    def queries(self) -> list[str]:
        limit = max(1, int(self.cfg.get("poll.x_max_queries", 5)))
        queries = [
            str(query).strip()
            for query in (self.cfg.get("social.x_keyword_queries", []) or [])
            if str(query).strip()
        ]
        seed_tags = list(self.cfg.get("social.seed_hashtags", []) or [])
        dynamic_tags: list[str] = []
        if self.db and self.cfg.get("social.auto_discover_hashtags", True):
            dynamic_tags = self.db.discovered_hashtags(
                min_mentions=int(self.cfg.get("social.hashtag_min_mentions", 2)),
                min_sources=int(self.cfg.get("social.hashtag_min_sources", 2)),
                days=int(self.cfg.get("social.hashtag_window_days", 30)),
                limit=int(self.cfg.get("social.max_discovered_hashtags", 24)),
            )

        def hashtag_query(tags: list[str]) -> str:
            terms = [f"#{tag.lstrip('#')}" for tag in tags if tag]
            return f"({' OR '.join(terms)}) -is:retweet -is:reply" if terms else ""

        seed_query = hashtag_query(seed_tags)
        if seed_query:
            queries.append(seed_query)
        # هشتگ‌های کشف‌شده بعد از queryهای ثابت می‌آیند تا هیچ فهرست ثابتی حذف نشود.
        for offset in range(0, len(dynamic_tags), 8):
            query = hashtag_query(dynamic_tags[offset:offset + 8])
            if query:
                queries.append(query)
        return list(dict.fromkeys(queries))[:limit]

    def poll(self) -> list[RawItem]:
        if not self.enabled:
            return []
        cutoff = util.now_utc() - timedelta(hours=float(self.cfg.get("poll.max_item_age_hours", 12)))
        out: list[RawItem] = []
        max_results = max(10, min(100, int(self.cfg.get("social.x_max_results", 25))))
        params_base = {
            "max_results": max_results,
            "tweet.fields": "created_at,lang,public_metrics,author_id,entities",
            "expansions": "author_id",
            "user.fields": "username,name",
        }
        for query in self.queries():
            try:
                response = requests.get(
                    self.SEARCH_URL,
                    params={**params_base, "query": query},
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=(8.0, 25.0),
                )
                if response.status_code == 429:
                    log.warning("سهمیه‌ی X API تمام شد (429)؛ جست‌وجوی این نوبت متوقف شد")
                    break
                if response.status_code in (401, 403):
                    log.error(
                        "X API دسترسی را رد کرد (%s)؛ توکن و مجوز recent search را بررسی کن",
                        response.status_code,
                    )
                    break
                response.raise_for_status()
                payload = response.json()
            except requests.RequestException as exc:
                log.warning("جست‌وجوی X ناموفق شد: %s", exc)
                continue
            except ValueError as exc:
                log.warning("پاسخ X API قابل‌خواندن نیست: %s", exc)
                continue

            users = {
                str(user.get("id")): user
                for user in (payload.get("includes", {}).get("users") or [])
            }
            for post in payload.get("data") or []:
                when = None
                if post.get("created_at"):
                    try:
                        when = datetime.fromisoformat(
                            post["created_at"].replace("Z", "+00:00")
                        ).astimezone(timezone.utc)
                    except (TypeError, ValueError):
                        pass
                if when and when < cutoff:
                    continue
                user = users.get(str(post.get("author_id") or ""), {})
                username = str(user.get("username") or post.get("author_id") or "unknown")
                text = str(post.get("text") or "").strip()
                if not text:
                    continue
                public = post.get("public_metrics") or {}
                out.append(
                    RawItem(
                        source_id=f"x_{username.casefold()}",
                        source_name=f"@{username}",
                        source_url=f"https://x.com/{username}",
                        title=text.splitlines()[0][:200],
                        body=util.truncate(text, 1000),
                        url=f"https://x.com/{username}/status/{post.get('id', '')}",
                        lang=str(post.get("lang") or "en"),
                        country="INTL",
                        kind="x",
                        published_at=when,
                        extra={
                            "tier": 1,
                            "likes": int(public.get("like_count", 0) or 0),
                            "reposts": int(public.get("retweet_count", 0) or 0),
                        },
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


def build_jobs(config, db, airline_index, bot=None) -> list[CollectorJob]:
    rss = RssCollector(config, db)
    notam = NotamCollector(config)
    telegram = TelegramChannelCollector(config, bot)
    x = XCollector(config, airline_index, db)

    jobs = [CollectorJob("rss", int(config.get("poll.rss_seconds", 75)), rss.poll)]
    # فقط کالکتورهایی که واقعاً فعال‌اند زمان‌بندی می‌شوند تا نخ بیکار نسازند
    # و سلامت‌سنج بتواند مرگ کالکتورهای واقعی را تشخیص دهد.
    if config.get("notam.enabled", False):
        jobs.append(CollectorJob("notam", int(config.get("poll.notam_seconds", 60)), notam.poll))
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
