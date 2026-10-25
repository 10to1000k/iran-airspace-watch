"""استخراج متن مقاله از صفحه‌ی ناشر با محدودیت اندازه و ایمنی دامنه."""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger(__name__)

MAX_ARTICLE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
ARTICLE_CACHE_SIZE = 256
ARTICLE_CACHE_TTL = 60 * 60
FAILED_CACHE_TTL = 10 * 60

_MULTI_LABEL_SUFFIXES = {
    "ac.jp", "ac.uk", "asn.au", "co.in", "co.jp", "co.kr", "co.nz",
    "co.uk", "com.ar", "com.au", "com.br", "com.cn", "com.hk",
    "com.mx", "com.sg", "com.tr", "com.tw", "com.ua", "com.vn",
    "edu.au", "gov.uk", "net.au", "org.au",
}


def clean_html_text(raw: str) -> str:
    """HTML را به متن ساده تبدیل می‌کند و پاراگراف‌ها را نگه می‌دارد."""
    raw = str(raw or "")
    raw = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", " ", raw)
    raw = re.sub(r"(?i)<\s*br\b[^>]*>", "\n", raw)
    raw = re.sub(
        r"(?i)</\s*(?:p|div|li|blockquote|h[1-6]|tr)\s*>",
        "\n\n",
        raw,
    )
    raw = re.sub(r"(?i)<\s*li\b[^>]*>", "• ", raw)
    text = html.unescape(re.sub(r"<[^>]*>", " ", raw)).replace("\xa0", " ")
    text = re.sub(r"[\t\r\f\v ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class _ArticleHTMLParser(HTMLParser):
    """استخراج متن از article/main و ظرف‌های بدنه‌ی خبر؛ بدون منو و تبلیغات."""

    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
    _BLOCK_TAGS = {
        "address", "article", "blockquote", "dd", "div", "dl", "dt",
        "h1", "h2", "h3", "h4", "h5", "h6", "li", "main", "ol",
        "p", "pre", "section", "table", "td", "th", "tr", "ul",
    }
    _EXCLUDED_TAGS = {
        "aside", "button", "footer", "form", "header", "iframe", "nav",
        "noscript", "select", "svg", "script", "style", "template",
    }
    _CONTENT_CLASS = re.compile(
        r"(?:article|story|entry|post|news|content|full)[-_ ]{0,2}"
        r"(?:body|content|text|detail)|"
        r"(?:body|content|text|detail)[-_ ]{0,2}"
        r"(?:article|story|entry|post|news)|field-name-body",
        re.I,
    )
    _SKIP_CLASS = re.compile(
        r"(?:^|[-_ ])(?:advert|advertisement|breadcrumb|comment|comments|"
        r"cookie|footer|menu|navigation|newsletter|related|share|sidebar|"
        r"social|subscribe|toolbar)(?:$|[-_ ])",
        re.I,
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._elements: list[str] = []
        self._active: list[dict] = []
        self._candidates: list[tuple[int, str]] = []
        self._skip_depth = 0
        self._jsonld_parts: list[str] | None = None
        self._jsonld_bodies: list[str] = []

    def _candidate_priority(self, tag: str, attrs: dict[str, str | None]) -> int:
        descriptor = " ".join(
            str(value or "") for value in (attrs.get("id"), attrs.get("class"))
        )
        if self._SKIP_CLASS.search(descriptor):
            return 0
        if tag == "article":
            return 4
        if self._CONTENT_CLASS.search(descriptor):
            return 5
        if tag == "main":
            return 3
        if tag == "body":
            return 1
        return 0

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if self._skip_depth:
            if tag not in self._VOID_TAGS:
                self._skip_depth += 1
            return

        if tag in self._EXCLUDED_TAGS:
            if (
                tag == "script"
                and "ld+json" in str(attributes.get("type") or "").casefold()
            ):
                self._jsonld_parts = []
            if tag not in self._VOID_TAGS:
                self._skip_depth = 1
            return

        priority = self._candidate_priority(tag, attributes)
        if priority:
            candidate = {
                "tag": tag,
                "depth": len(self._elements) + 1,
                "priority": priority,
                "parts": [],
            }
            self._active.append(candidate)

        if tag == "br":
            self._append_to_active("\n")
            return
        if tag in self._BLOCK_TAGS:
            self._append_to_active("\n\n")
        if tag not in self._VOID_TAGS:
            self._elements.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            if tag == "script" and self._jsonld_parts is not None:
                raw = "".join(self._jsonld_parts).strip()
                self._jsonld_parts = None
                self._read_jsonld(raw)
            if tag not in self._VOID_TAGS:
                self._skip_depth = max(0, self._skip_depth - 1)
            return

        if tag in self._BLOCK_TAGS:
            self._append_to_active("\n\n")

        depth = len(self._elements)
        for index in range(len(self._active) - 1, -1, -1):
            candidate = self._active[index]
            if candidate["tag"] == tag and candidate["depth"] == depth:
                self._save_candidate(candidate)
                del self._active[index]
                break

        for index in range(len(self._elements) - 1, -1, -1):
            if self._elements[index] == tag:
                del self._elements[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._jsonld_parts is not None:
            self._jsonld_parts.append(data)
        elif not self._skip_depth and data:
            self._append_to_active(data)

    def close(self) -> None:
        super().close()
        while self._active:
            self._save_candidate(self._active.pop())
        if self._jsonld_parts is not None:
            self._read_jsonld("".join(self._jsonld_parts))
            self._jsonld_parts = None

    def _append_to_active(self, value: str) -> None:
        for candidate in self._active:
            candidate["parts"].append(value)

    def _save_candidate(self, candidate: dict) -> None:
        text = clean_html_text("".join(candidate["parts"]))
        if text:
            self._candidates.append((int(candidate["priority"]), text))

    def _read_jsonld(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return

        def visit(value) -> None:
            if isinstance(value, dict):
                body = value.get("articleBody")
                if isinstance(body, str) and body.strip():
                    self._jsonld_bodies.append(body)
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload)

    def result(self) -> str:
        choices = [
            (6, clean_html_text(body))
            for body in self._jsonld_bodies
            if isinstance(body, str)
        ] + self._candidates
        choices = [(priority, text) for priority, text in choices if len(text) >= 100]
        if not choices:
            return ""
        return max(choices, key=lambda item: (len(item[1]) + item[0] * 250, item[0]))[1]


def extract_article_text(document: str) -> str:
    """متن بدنه‌ی مقاله را از HTML عمومی و بدون ورود استخراج می‌کند."""
    parser = _ArticleHTMLParser()
    try:
        parser.feed(document or "")
        parser.close()
    except Exception:  # noqa: BLE001
        log.debug("HTML مقاله قابل‌تجزیه نبود", exc_info=True)
    return parser.result()


def _registrable_domain(host: str) -> str:
    host = (host or "").casefold().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _safe_same_site(article_url: str, source_url: str) -> bool:
    article = urlparse(article_url)
    source = urlparse(source_url)
    try:
        article_port = article.port
        source.port
    except ValueError:
        return False
    if (
        article.scheme.casefold() not in {"http", "https"}
        or source.scheme.casefold() not in {"http", "https"}
        or not article.hostname
        or not source.hostname
        or article.username
        or article.password
        or (article_port not in (None, 80, 443))
    ):
        return False
    article_host = article.hostname.casefold().rstrip(".")
    source_host = source.hostname.casefold().rstrip(".")
    if article_host == "localhost" or article_host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        ipaddress.ip_address(article_host)
        return False
    except ValueError:
        pass
    return _registrable_domain(article_host) == _registrable_domain(source_host)


def _decode_html(data: bytes, response) -> str:
    head = data[:8192].decode("ascii", errors="ignore")
    charset = re.search(
        r"<meta[^>]+charset\s*=\s*[\"']?([\w.-]+)", head, re.I
    )
    content_type = str(response.headers.get("Content-Type", ""))
    header_charset = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", content_type, re.I)
    encodings = [
        charset.group(1) if charset else None,
        header_charset.group(1) if header_charset else None,
        getattr(response, "apparent_encoding", None),
        getattr(response, "encoding", None),
        "utf-8",
    ]
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return data.decode(encoding, errors="replace")
        except (LookupError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


class ArticleFetcher:
    """واکشی محدود و cacheشده‌ی مقاله‌های ناشر، بدون دنبال‌کردن دامنه‌ی ثالث."""

    def __init__(self, config=None):
        self.timeout = (
            float(config.get("poll.article_fetch_timeout_seconds", 12))
            if config
            else 12.0
        )
        self.max_bytes = max(
            64 * 1024,
            int(config.get("poll.article_fetch_max_bytes", MAX_ARTICLE_BYTES))
            if config
            else MAX_ARTICLE_BYTES,
        )
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def can_fetch(article_url: str, source_url: str) -> bool:
        return _safe_same_site(article_url, source_url)

    def cached(self, article_url: str, source_url: str) -> tuple[bool, str | None]:
        """نتیجه‌ی cache را برمی‌گرداند؛ None هم می‌تواند شکست ذخیره‌شده باشد."""
        if not self.can_fetch(article_url, source_url):
            return False, None
        key = article_url.strip()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return True, cached[1]
            self._cache.pop(key, None)
        return False, None

    def fetch(self, article_url: str, source_url: str) -> str | None:
        is_cached, cached_text = self.cached(article_url, source_url)
        if is_cached:
            return cached_text

        if not _safe_same_site(article_url, source_url):
            return None

        try:
            text = self._read_article(article_url, source_url)
        except Exception as exc:  # noqa: BLE001
            log.debug("صفحه‌ی مقاله واکشی نشد (%s): %s", article_url, exc)
            text = None

        ttl = ARTICLE_CACHE_TTL if text else FAILED_CACHE_TTL
        key = article_url.strip()
        with self._lock:
            if len(self._cache) >= ARTICLE_CACHE_SIZE:
                oldest = min(self._cache, key=lambda item: self._cache[item][0])
                self._cache.pop(oldest, None)
            self._cache[key] = (time.monotonic() + ttl, text)
        return text

    def _read_article(self, article_url: str, source_url: str) -> str | None:
        current_url = article_url
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; IranAirspaceWatch/1.0; "
                "+https://github.com/10to1000k/iran-airspace-watch)"
            ),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        }
        for _ in range(MAX_REDIRECTS + 1):
            if not _safe_same_site(current_url, source_url):
                return None
            with requests.get(
                current_url,
                headers=headers,
                timeout=(5.0, self.timeout),
                stream=True,
                allow_redirects=False,
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location") or response.headers.get("location")
                    if not location:
                        return None
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                content_type = str(
                    response.headers.get("Content-Type", response.headers.get("content-type", ""))
                ).casefold()
                if content_type and not any(
                    kind in content_type for kind in ("text/html", "application/xhtml+xml")
                ):
                    return None
                payload = bytearray()
                for chunk in response.iter_content(chunk_size=32_768):
                    if not chunk:
                        continue
                    payload.extend(chunk)
                    if len(payload) >= self.max_bytes:
                        break
                page = _decode_html(bytes(payload), response)
                text = extract_article_text(page)
                return text or None
        return None
