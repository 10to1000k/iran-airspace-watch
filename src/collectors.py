"""کالکتورها: RSS، کانال‌های تلگرام، و X API رسمی.

همه یک قرارداد دارند: poll() -> list[RawItem]
"""

from __future__ import annotations

import calendar
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlparse

import feedparser
import requests

from . import util
from .article import ArticleFetcher, clean_html_text
from .classify import Classifier
from .models import RawItem
from .notam import NotamCollector

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MAX_FEED_BYTES = 2 * 1024 * 1024  # سقف ۲ مگابایت برای جلوگیری از مصرف بی‌حد رم یا قفل شدن سوکت
MAX_TELEGRAM_PREVIEW_BYTES = 2 * 1024 * 1024
BARE_INDEX_PATHS = {"", "/", "/blog", "/news", "/feed", "/rss"}


class _TelegramPreviewParser(HTMLParser):
    """استخراج متن پست‌ها از پیش‌نمایش عمومی t.me/s/<username>."""

    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self, username: str):
        super().__init__(convert_charrefs=True)
        self.username = username.casefold()
        self.posts: list[dict] = []
        self.current: dict | None = None
        self._capturing_text = False
        self._text_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        classes = set(str(attributes.get("class") or "").split())
        post_ref = str(attributes.get("data-post") or "")

        if tag == "div" and post_ref and "tgme_widget_message" in classes:
            self._finish_post()
            channel, separator, post_id = post_ref.rpartition("/")
            if separator and channel.casefold() == self.username and post_id.isdigit():
                self.current = {
                    "id": post_id,
                    "date": attributes.get("data-post-date"),
                    "iso_date": None,
                    "text_parts": [],
                }
            else:
                self.current = None
            self._capturing_text = False
            self._text_depth = 0
            return

        if not self.current:
            return

        if tag == "time" and attributes.get("datetime") and not self.current.get("iso_date"):
            self.current["iso_date"] = attributes["datetime"]

        if "tgme_widget_message_text" in classes and not self._capturing_text:
            self._capturing_text = True
            self._text_depth = 1
            return

        if self._capturing_text:
            if tag in self._VOID_TAGS:
                if tag in {"br", "hr"}:
                    self.current["text_parts"].append("\n")
            else:
                if tag in {"div", "p", "li"}:
                    self.current["text_parts"].append("\n")
                self._text_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if not self.current or not self._capturing_text or tag in self._VOID_TAGS:
            return
        self._text_depth -= 1
        if self._text_depth <= 0:
            self._capturing_text = False
            self._text_depth = 0
        elif tag in {"div", "p", "li"}:
            self.current["text_parts"].append("\n")

    def handle_data(self, data: str) -> None:
        if self.current and self._capturing_text and data:
            self.current["text_parts"].append(data)

    def close(self) -> None:
        super().close()
        self._finish_post()

    def _finish_post(self) -> None:
        if not self.current:
            return
        raw_text = "".join(self.current.pop("text_parts", []))
        raw_text = raw_text.replace("\xa0", " ")
        raw_text = re.sub(r"[\t\r\f\v ]+", " ", raw_text)
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        self.current["text"] = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        self.posts.append(self.current)
        self.current = None
        self._capturing_text = False
        self._text_depth = 0


class RssCollector:
    def __init__(self, config, db):
        self.cfg = config
        self.db = db
        self.max_age_hours = float(config.get("poll.max_item_age_hours", 12))
        self.per_feed = int(config.get("poll.max_items_per_feed", 15))
        self.max_workers = max(1, int(config.get("poll.rss_workers", 6)))
        self.fetch_full_articles = bool(config.get("poll.fetch_full_articles", True))
        self.article_fetch_min_chars = max(
            0, int(config.get("poll.article_fetch_min_rss_chars", 1800))
        )
        self.article_fetch_max_per_feed = max(
            0, int(config.get("poll.article_fetch_max_per_feed", 3))
        )
        self.article_fetch_max_per_poll = max(
            0, int(config.get("poll.article_fetch_max_per_poll", 18))
        )
        self._article_fetch_count = 0
        self._article_fetch_lock = threading.Lock()
        self.classifier = Classifier(config)
        self.article_fetcher = ArticleFetcher(config)
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
        with self._article_fetch_lock:
            self._article_fetch_count = 0
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
        article_fetches = 0
        for entry in parsed.entries[: self.per_feed]:
            published = _entry_time(entry)
            if published and published < cutoff:
                continue
            title = util.strip_html(entry.get("title", "")).strip()
            body = _entry_body(entry)
            raw_link = (entry.get("link") or "").strip()
            link = urljoin(source["url"], raw_link) if raw_link else source["url"]
            if raw_link and self._should_skip(link, source["url"]):
                continue
            if not title and not body:
                continue

            item = RawItem(
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
                    "collected_at": util.iso(util.now_utc()),
                },
            )
            full_body = None
            if self._is_article_candidate(item):
                cache_hit, cached_body = self.article_fetcher.cached(link, source["url"])
                if cache_hit:
                    full_body = cached_body
                elif self._should_fetch_article(item, article_fetches, candidate_checked=True):
                    article_fetches += 1
                    full_body = self.article_fetcher.fetch(link, source["url"])
                if full_body and _is_better_article_text(full_body, item.body):
                    item.extra["article_text_fetched"] = True
                    item.extra["rss_body_chars"] = len(item.body)
                    item.body = full_body
                    log.debug(
                        "متن صفحه‌ی مقاله برای %s دریافت شد (%d نویسه)",
                        source.get("id"),
                        len(full_body),
                    )
            out.append(item)
        return out

    def _is_article_candidate(self, item: RawItem) -> bool:
        if (
            not self.fetch_full_articles
            or len(item.body) >= self.article_fetch_min_chars
            or not item.url
            or not ArticleFetcher.can_fetch(item.url, item.source_url)
        ):
            return False
        try:
            return self.classifier.classify(item).relevant
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "پیش‌فیلتر مقاله ناموفق شد؛ واکشی انجام نمی‌شود: %s", exc
            )
            return False

    def _should_fetch_article(
        self, item: RawItem, article_fetches: int, candidate_checked: bool = False
    ) -> bool:
        if (
            article_fetches >= self.article_fetch_max_per_feed
            or (not candidate_checked and not self._is_article_candidate(item))
        ):
            return False
        with self._article_fetch_lock:
            if self._article_fetch_count >= self.article_fetch_max_per_poll:
                return False
            self._article_fetch_count += 1
        return True


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def _entry_body(entry) -> str:
    """طولانی‌ترین متن RSS را بدون بریدن نگه می‌دارد و شکست پاراگراف‌ها را حفظ می‌کند."""
    candidates: list[str] = []
    for content in entry.get("content", []) or []:
        if isinstance(content, dict):
            candidates.append(clean_html_text(content.get("value", "")))
    for key in ("summary", "description", "content_encoded"):
        value = entry.get(key, "") or ""
        if isinstance(value, dict):
            value = value.get("value", "")
        candidates.append(clean_html_text(value))
    return max(candidates, key=len, default="")


def _is_better_article_text(article_body: str, rss_body: str) -> bool:
    """بررسی می‌کند متن صفحه به‌اندازه‌ی کافی از excerpt RSS کامل‌تر باشد."""
    article_chars = len(clean_html_text(article_body))
    rss_chars = len(rss_body or "")
    return article_chars >= max(200, rss_chars + 80, int(rss_chars * 1.15))


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


class TelegramPreviewRateLimited(RuntimeError):
    def __init__(self, retry_after: float = 60):
        super().__init__("Telegram public preview rate limit")
        self.retry_after = retry_after


class TelegramPublicPreviewCollector:
    """پایش کانال‌های عمومی از صفحه‌ی وب t.me/s؛ بدون Bot API یا حساب کاربری.

    این صفحه API رسمی پایش نیست؛ فقط محتوای عمومی را می‌خوانیم، با فاصله‌ی
    محافظه‌کارانه و سقف اندازه‌ی پاسخ. برای کانال خصوصی/بدون پیش‌نمایش کار نمی‌کند.
    """

    def __init__(self, config, db=None):
        self.cfg = config
        self.db = db
        self.channels = config.telegram_channels
        self.max_age_hours = max(1.0, float(config.get("poll.max_item_age_hours", 12)))
        self.enabled = (
            _flag_direct(config, "ENABLE_TELEGRAM_PUBLIC_PREVIEW", default=True)
            and bool(self.channels)
        )
        self._cooldown_until = 0.0
        if not self.enabled and self.channels:
            log.info("پایش پیش‌نمایش عمومی تلگرام خاموش است")

    @staticmethod
    def _published_at(post: dict) -> datetime | None:
        raw_timestamp = str(post.get("date") or "").strip()
        if raw_timestamp:
            try:
                return datetime.fromtimestamp(int(raw_timestamp), tz=timezone.utc)
            except (TypeError, ValueError, OSError, OverflowError):
                pass

        raw_iso = str(post.get("iso_date") or "").strip()
        if raw_iso:
            try:
                value = datetime.fromisoformat(raw_iso.replace("Z", "+00:00"))
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value.astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass
        return None

    def parse_page(self, page: str, source: dict) -> list[RawItem]:
        username = str(source.get("username") or "").strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return []

        parser = _TelegramPreviewParser(username)
        parser.feed(page)
        parser.close()
        cutoff = util.now_utc() - timedelta(hours=self.max_age_hours)
        source_id = str(source.get("id") or f"tg_{username.casefold()}")
        source_url = f"https://t.me/{username}"
        source_name = str(source.get("name") or f"@{username}")
        try:
            tier = int(source.get("tier", 1))
        except (TypeError, ValueError):
            tier = 1

        items: list[RawItem] = []
        seen_ids: set[str] = set()
        for post in parser.posts:
            message_id = str(post.get("id") or "")
            text = str(post.get("text") or "").strip()
            published_at = self._published_at(post)
            if (
                not message_id
                or message_id in seen_ids
                or not text
                or not published_at
                or published_at < cutoff
            ):
                continue
            seen_ids.add(message_id)
            url = f"{source_url}/{message_id}"
            item = RawItem(
                source_id=source_id,
                source_name=source_name,
                source_url=source_url,
                title=text.splitlines()[0][:200],
                body=text,
                url=url,
                lang=str(source.get("lang") or "auto"),
                country=str(source.get("country") or "INTL"),
                kind="telegram",
                published_at=published_at,
                extra={
                    "tier": tier,
                    "collected_at": util.iso(util.now_utc()),
                    "channel": username.casefold(),
                    "telegram_message_id": message_id,
                    "telegram_collection": "public_preview",
                },
            )
            if self.db:
                fingerprint = util.fingerprint(
                    item.source_id, item.url, item.title, item.body[:400]
                )
                if self.db.item_seen(item.source_id, fingerprint):
                    continue
            items.append(item)
        return items

    def _read_page(self, username: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            raise ValueError("نام کاربری عمومی تلگرام معتبر نیست")
        url = f"https://t.me/s/{username}"
        with requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=(8.0, 18.0),
            stream=True,
        ) as response:
            if response.status_code == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After", 60))
                except (TypeError, ValueError):
                    retry_after = 60.0
                raise TelegramPreviewRateLimited(max(1.0, retry_after))
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_content(chunk_size=16_384):
                if not chunk:
                    continue
                content.extend(chunk)
                if len(content) > MAX_TELEGRAM_PREVIEW_BYTES:
                    raise ValueError("پیش‌نمایش تلگرام از سقف ۲ مگابایت بزرگ‌تر است")
            return bytes(content).decode(response.encoding or "utf-8", errors="replace")

    def poll(self) -> list[RawItem]:
        if not self.enabled or time.monotonic() < self._cooldown_until:
            return []

        out: list[RawItem] = []
        for source in self.channels:
            username = str(source.get("username") or "").strip().lstrip("@")
            if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
                log.warning("نام کاربری نامعتبر برای پیش‌نمایش تلگرام نادیده گرفته شد")
                continue
            try:
                page = self._read_page(username)
                if "data-post" not in page:
                    raise ValueError("پیش‌نمایش عمومی یا پست قابل‌خواندن نیست")
                out.extend(self.parse_page(page, source))
                if self.db:
                    self.db.mark_source_ok(str(source.get("id") or f"tg_{username.casefold()}"))
            except TelegramPreviewRateLimited as exc:
                self._cooldown_until = time.monotonic() + min(exc.retry_after, 3600.0)
                log.warning("تلگرام محدودیت نرخ داد؛ پایش تا %.0f ثانیه متوقف شد", min(exc.retry_after, 3600.0))
                if self.db:
                    self.db.mark_source_error(str(source.get("id") or f"tg_{username.casefold()}"), str(exc))
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("پیش‌نمایش عمومی تلگرام @%s خوانده نشد: %s", username, exc)
                if self.db:
                    self.db.mark_source_error(str(source.get("id") or f"tg_{username.casefold()}"), str(exc))
        return out


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
                        body=text,
                        url=f"https://x.com/{username}/status/{post.get('id', '')}",
                        lang=str(post.get("lang") or "en"),
                        country="INTL",
                        kind="x",
                        published_at=when,
                        extra={
                            "tier": 1,
                            "collected_at": util.iso(util.now_utc()),
                            "likes": int(public.get("like_count", 0) or 0),
                            "reposts": int(public.get("retweet_count", 0) or 0),
                        },
                    )
                )
        return out


def _flag_direct(config, name: str, default: bool = False) -> bool:
    raw = config.env_get(name, "true" if default else "false") or "false"
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
    telegram_public = TelegramPublicPreviewCollector(config, db)
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
    if telegram_public.enabled:
        jobs.append(
            CollectorJob(
                "telegram_public",
                int(config.get("poll.telegram_public_seconds", 300)),
                telegram_public.poll,
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
