"""قالب‌بندی پست فارسی."""

from __future__ import annotations

import html

from . import util

SEVERITY_ICON = {"critical": "🔴", "important": "🟠", "context": "🟡"}
SEVERITY_LABEL = {"critical": "فوری", "important": "مهم", "context": "زمینه"}
CATEGORY_LABEL = {
    "airspace": "حریم هوایی",
    "airport": "فرودگاه",
    "airline": "ایرلاین",
    "context": "زمینه",
}
COUNTRY_LABEL = {
    "IR": "ایران",
    "TR": "ترکیه",
    "AE": "امارات",
    "RU": "روسیه",
    "CN": "چین",
    "PK": "پاکستان",
    "TH": "تایلند",
    "VN": "ویتنام",
    "QA": "قطر",
    "OM": "عمان",
    "KW": "کویت",
    "BH": "بحرین",
    "SA": "عربستان",
    "IQ": "عراق",
    "SY": "سوریه",
    "LB": "لبنان",
    "AF": "افغانستان",
    "AZ": "آذربایجان",
    "GE": "گرجستان",
    "AM": "ارمنستان",
    "KZ": "قزاقستان",
    "UZ": "ازبکستان",
    "TM": "ترکمنستان",
    "DE": "آلمان",
    "AT": "اتریش",
    "NL": "هلند",
    "GB": "بریتانیا",
    "FR": "فرانسه",
    "IN": "هند",
    "INTL": "بین‌المللی",
}
CONFIDENCE_LABEL = {
    "official": "📢 منبع رسمی",
    "multi": "✅ تأییدشده با دو منبع مستقل",
    "single": "⚠️ تک‌منبع — تأییدنشده",
    "corrected": "❌ این خبر اصلاح شده است",
}


def _get(event, key, default=""):
    try:
        value = event[key]
    except (KeyError, IndexError, TypeError):
        value = getattr(event, key, default)
    return default if value is None else value


def _split(value: str) -> list[str]:
    return [part for part in (value or "").split(",") if part]


def _tag(text: str) -> str:
    return "#" + "_".join(text.split())


def hashtags(event, limit: int = 5) -> str:
    tags = [_tag(CATEGORY_LABEL.get(_get(event, "category"), "خبر"))]
    for code in _split(_get(event, "countries")):
        label = COUNTRY_LABEL.get(code)
        if label:
            tags.append(_tag(label))
    for name in _split(_get(event, "airlines"))[:2]:
        tags.append(_tag(name))
    seen, unique = set(), []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            unique.append(tag)
    return " ".join(unique[:limit])


def _elapsed_label(start, end) -> str:
    start_dt = util.parse_iso(str(start or ""))
    end_dt = util.parse_iso(str(end or ""))
    if not start_dt or not end_dt:
        return ""
    seconds = int((end_dt - start_dt).total_seconds())
    if seconds < 0:
        return ""
    if seconds < 60:
        return "کمتر از یک دقیقه"

    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    parts = []
    if days:
        parts.append(f"{util.fa_digits(days)} روز")
    if hours:
        parts.append(f"{util.fa_digits(hours)} ساعت")
    if minutes and len(parts) < 2:
        parts.append(f"{util.fa_digits(minutes)} دقیقه")
    return " و ".join(parts)


def format_event(event, config) -> str:
    severity = _get(event, "severity", "context")
    category = _get(event, "category", "context")
    title = _get(event, "title_fa") or "بدون تیتر"
    # متن کامل ترجمه‌شده را نگه می‌داریم؛ TelegramBot آن را به پیام‌های مجاز تقسیم می‌کند.
    body = str(_get(event, "body_fa") or "").strip()
    confidence = _get(event, "confidence", "single")
    source = _get(event, "primary_source") or "نامشخص"
    url = _get(event, "primary_url") or ""
    when = _get(event, "first_seen")
    source_published_at = _get(event, "primary_published_at")
    telegram_sent_at = _get(event, "telegram_sent_at")

    lines = [
        f"{SEVERITY_ICON.get(severity, '⚪')} <b>{SEVERITY_LABEL.get(severity, 'خبر')}</b>"
        f" | {CATEGORY_LABEL.get(category, 'خبر')}",
        f"📥 زمان رصد بات: {util.format_moment(when)}",
    ]
    if source_published_at:
        lines.append(
            "📰 نخستین انتشارِ رصدشده: "
            f"{html.escape(str(source))} — {util.format_moment(source_published_at)}"
        )
    else:
        lines.append(f"📌 منبع: {html.escape(str(source))} — زمان انتشار نامشخص")
    if telegram_sent_at:
        lines.append(f"📨 زمان ارسال تلگرام: {util.format_moment(telegram_sent_at)}")
        elapsed = _elapsed_label(source_published_at, telegram_sent_at)
        if elapsed:
            lines.append(f"⏱ فاصله از انتشار تا ارسال: {elapsed}")
    lines += ["", f"<b>{html.escape(title)}</b>"]
    if body:
        lines += ["", html.escape(body)]

    airlines = _split(_get(event, "airlines"))
    if airlines:
        lines += ["", "✈️ " + " — ".join(html.escape(a) for a in airlines)]

    countries = [COUNTRY_LABEL.get(c, c) for c in _split(_get(event, "countries"))]
    if countries:
        lines.append("🌍 " + "، ".join(html.escape(c) for c in countries))

    lines += [""]
    if url.startswith("http"):
        lines.append(f'🔗 <a href="{html.escape(url)}">متن اصلی خبر</a>')

    badge = CONFIDENCE_LABEL.get(confidence, "")
    if badge:
        lines.append(badge)
    if not _get(event, "numbers_ok", 1):
        lines.append("⚠️ اعداد این ترجمه نیازمند بازبینی است")

    lines += ["", hashtags(event)]
    return "\n".join(lines).strip()


def format_source_update(event, config) -> str:
    """نسخه‌ی کامل ترجمه‌شده‌ی منبع تازه در رشته‌ی رخداد."""
    source = html.escape(str(_get(event, "primary_source") or "منبع تازه"))
    return f"📰 <b>گزارش کامل از {source}</b>\n\n{format_event(event, config)}"


def format_update(event, source_name: str, note: str = "") -> str:
    """پیام کوتاه در رشته‌ی رخداد."""
    headline = util.truncate(_get(event, "title_fa"), 90)
    text = f"🔁 <b>به‌روزرسانی</b>\n{html.escape(headline)}"
    if source_name:
        text += f"\n📌 منبع تازه: {html.escape(source_name)}"
    if note:
        text += f"\n{html.escape(note)}"
    url = _get(event, "primary_url")
    if url.startswith("http"):
        text += f'\n🔗 <a href="{html.escape(url)}">متن اصلی</a>'
    return text


def format_publication_notice(event) -> str:
    return (
        f"🔴 <b>انتشار</b>\n{html.escape(util.truncate(_get(event, 'title_fa'), 120))}\n"
        f"{_tag('هشدار')}"
    )
