"""متن کامل مقاله از لینک خبر.

خیلی از فیدها فقط خلاصه می‌دهند؛ برای دسته‌بندی، خوشه‌بندی و ترجمه‌ی دقیق‌تر
متن کامل مقاله از خود لینک گرفته می‌شود. سقف حجم دانلود و سقف طول متن بسته
است تا حافظه و پهنای‌باند بی‌حد مصرف نشود. هر خطا «متن خالی» برمی‌گرداند —
نبودِ متن کامل هرگز نباید جلوی انتشار خبر خلاصه‌دار را بگیرد.
"""

from __future__ import annotations

import logging
from html.parser import HTMLParser

import requests

from . import util

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# سقف حجم دانلود (حفاظ رم/سوکت، مثل MAX_FEED_BYTES در collectors)
MAX_ARTICLE_BYTES = 1_000_000

# تگ‌هایی که متن‌شان هرگز بخشی از مقاله نیست (اسکریپت، ناوبری، تبلیغ و ...)
_SKIP_TAGS = {
    "script", "style", "noscript", "template", "svg", "iframe", "canvas",
    "form", "input", "button", "select", "option", "label", "nav", "footer",
    "aside", "menu", "dialog",
}

# تگ‌های بلوکی: پایان‌شان خط تازه می‌سازد تا پاراگراف‌ها قاطی نشوند
_BLOCK_TAGS = {
    "p", "div", "article", "section", "main", "h1", "h2", "h3", "h4", "h5",
    "h6", "li", "ul", "ol", "blockquote", "br", "tr", "table", "figcaption",
    "pre", "address",
}


class _ArticleText(HTMLParser):
    """متن قابل‌مشاهده‌ی صفحه را جمع می‌کند؛ بدون وابستگی بیرونی."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "br" and not self._skip_depth:
            self._parts.append("\n")

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif not self._skip_depth and tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):  # noqa: ANN001
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text)
            self._parts.append(" ")

    @property
    def text(self) -> str:
        lines = []
        for line in "".join(self._parts).splitlines():
            line = " ".join(line.split())
            if line:
                lines.append(line)
        return "\n".join(lines)


def extract_article_text(html_text: str, max_chars: int) -> str:
    """متن مقاله را از HTML استخراج و به سقف max_chars می‌برد."""
    parser = _ArticleText()
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception:  # noqa: BLE001 — HTML خراب نباید کل چرخه را ببخشد
        log.debug("HTML مقاله تجزیه نشد")
    return util.truncate(parser.text, max_chars)


def fetch_article_text(
    url: str,
    max_chars: int = 6000,
    timeout: tuple[float, float] = (5.0, 10.0),
) -> str:
    """متن کامل مقاله را از url می‌گیرد؛ هر خطا رشته‌ی خالی برمی‌گرداند."""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    response = None
    try:
        with requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            timeout=timeout,
            stream=True,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if content_type and "html" not in content_type and "xml" not in content_type:
                return ""
            buf = bytearray()
            for chunk in response.iter_content(chunk_size=32768):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) >= MAX_ARTICLE_BYTES:
                    break
    except Exception as exc:  # noqa: BLE001
        log.debug("متن کامل گرفته نشد: %s — %s", (url or "")[:120], exc)
        return ""

    # اگر سرور charset نداده باشد، requests برای text/* ایزو-۸۸۵۹-۱ حدس می‌زند؛
    # برای رسانه‌های فارسی/عربی/روسی utf-8 حدس بهتری است.
    encoding = (response.encoding or "").lower()
    if encoding in ("", "iso-8859-1", "ascii"):
        encoding = "utf-8"
    try:
        html_text = bytes(buf).decode(encoding, errors="replace")
    except LookupError:
        html_text = bytes(buf).decode("utf-8", errors="replace")
    return extract_article_text(html_text, max_chars)
