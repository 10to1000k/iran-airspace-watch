"""ابزارهای متنی و زمانی."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    TEHRAN = ZoneInfo("Asia/Tehran")
except ZoneInfoNotFoundError:  # ویندوز بدون پکیج tzdata
    # ایران از ۲۰۲۲ ساعت تابستانی ندارد؛ +۰۳:۳۰ ثابت است
    TEHRAN = timezone(timedelta(hours=3, minutes=30), "Asia/Tehran")
UTC = timezone.utc

# حروف عربی که در فارسی شکل دیگری دارند
_CHAR_MAP = str.maketrans({
    "ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "ؤ": "و",
    "إ": "ا", "أ": "ا", "ٱ": "ا", "ﻻ": "لا",
})
_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670]")
_TAG_RE = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text or "")


def normalize(text: str) -> str:
    """نرمال‌سازی برای مقایسه، نه برای نمایش."""
    text = unicodedata.normalize("NFKC", strip_html(text or ""))
    text = text.translate(_CHAR_MAP).replace("\u200c", " ")
    text = _DIACRITICS.sub("", text)
    # در زبان ترکی «İran».lower() کاراکتر ترکیبی i+\u0307 می‌سازد و با «iran» جور نمی‌شود
    text = text.lower().replace("\u0307", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fingerprint(*parts: str) -> str:
    """اثر انگشت پایدار برای جلوگیری از پردازش دوباره‌ی یک آیتم."""
    joined = "||".join(normalize(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(normalize(text)) if len(t) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def to_ascii_digits(text: str) -> str:
    return (text or "").translate(_DIGITS)


def numbers(text: str) -> list[str]:
    """اعداد متن را برای اعتبارسنجی ترجمه استخراج می‌کند."""
    return re.findall(r"\d+(?:\.\d+)?", to_ascii_digits(text or ""))


def numbers_preserved(source: str, translated: str) -> bool:
    """آیا همه‌ی اعداد مبدأ در ترجمه حاضرند؟ (چک نرم)"""
    src, dst = numbers(source), numbers(translated)
    if not src:
        return True
    remaining = list(dst)
    for num in src:
        if num in remaining:
            remaining.remove(num)
        else:
            return False
    return True


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def to_tehran(value: str | datetime | None) -> datetime | None:
    if isinstance(value, str):
        value = parse_iso(value)
    if value is None:
        return None
    return value.astimezone(TEHRAN)


_FA_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]


def g2j(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """تبدیل میلادی به جلالی (الگوریتم استاندارد، بدون وابستگی خارجی)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) \
        + ((gy2 + 399) // 400) - 80 + gd + g_d_m[gm - 1]
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def fa_digits(value: str | int) -> str:
    return to_ascii_digits(str(value)).translate(
        str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
    )


def format_moment(value: str | datetime | None) -> str:
    """مثل: ۲۱ مهر ۱۴۰۵ — ۱۲:۴۰ تهران (۰۹:۱۰ UTC)"""
    dt = to_tehran(value)
    if dt is None:
        return "زمان نامشخص"
    jy, jm, jd = g2j(dt.year, dt.month, dt.day)
    utc = dt.astimezone(UTC)
    return (
        f"{fa_digits(jd)} {_FA_MONTHS[jm - 1]} {fa_digits(jy)} — "
        f"{fa_digits(dt.strftime('%H:%M'))} تهران "
        f"({fa_digits(utc.strftime('%H:%M'))} UTC)"
    )


def short_time(value: str | datetime | None) -> str:
    dt = to_tehran(value)
    if dt is None:
        return "?"
    return fa_digits(dt.strftime("%H:%M"))


def hours_ago_iso(hours: float) -> str:
    return iso(now_utc() - timedelta(hours=hours))


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"
