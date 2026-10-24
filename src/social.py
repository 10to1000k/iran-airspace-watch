"""هشتگ‌های شبکه‌های اجتماعی: استخراج، نرمال‌سازی و گسترش برای جست‌وجو."""

from __future__ import annotations

import re
import unicodedata

from . import util

# حروف فارسی/لاتین، عدد، زیرخط و نیم‌فاصله را در یک هشتگ نگه می‌دارد.
_HASHTAG_RE = re.compile(r"(?<![\w#])#([\w\u200c]{2,64})", re.UNICODE)
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")
_GENERIC_TAGS = {
    "news", "breaking", "breakingnews", "aviation", "aviationnews", "airspace",
    "airtravel", "flight", "flights", "airline", "airlines", "airport", "airports",
    "route", "routes", "notam", "travel", "iran", "iranian", "tehran", "fyp",
    "viral", "trending", "repost", "ad",
}


def normalize_hashtag(value: str) -> str:
    """شناسه‌ی پایدار هشتگ؛ بدون # و مناسب ذخیره در SQLite و Query API."""
    value = unicodedata.normalize("NFKC", value or "").strip().lstrip("#")
    value = value.replace("\u200c", "_")
    value = re.sub(r"[^\w]+", "_", value, flags=re.UNICODE).strip("_")
    value = util.normalize(value).replace(" ", "_").strip("_")
    return value[:64]


def extract_hashtags(text: str) -> list[str]:
    """هشتگ‌ها را یکتا و نرمال‌شده برمی‌گرداند؛ هشتگ‌های عمومی/نویز حذف می‌شوند."""
    found = {
        normalize_hashtag(match.group(1))
        for match in _HASHTAG_RE.finditer(text or "")
    }
    return sorted(
        tag for tag in found
        if len(tag) >= 3 and tag not in _GENERIC_TAGS and not tag.isdigit()
    )


def expand_hashtags(text: str) -> str:
    """هشتگ‌های CamelCase و زیرخط‌دار را برای تطبیق کلیدواژه باز می‌کند.

    نمونه: #IranAirspace → Iran Airspace و #حریم_هوایی → حریم هوایی.
    """
    text = text or ""
    expanded: list[str] = []
    for match in _HASHTAG_RE.finditer(text):
        raw = unicodedata.normalize("NFKC", match.group(1))
        words = _CAMEL_BOUNDARY_RE.sub(" ", raw)
        words = re.sub(r"[_\u200c]+", " ", words)
        if words.strip():
            expanded.append(words.strip())
    return f"{text}\n{' '.join(expanded)}" if expanded else text
