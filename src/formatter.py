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

    # هشتگ‌های از محتوای منبع (توییت/تلگرام)
    extra = _get(event, "extra", {})
    if isinstance(extra, str):
        import json

        try:
            extra = json.loads(extra)
        except (ValueError, TypeError):
            extra = {}
    if isinstance(extra, dict):
        for tag in extra.get("hashtags", [])[:3]:
            tags.append(tag)

    seen, unique = set(), []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            unique.append(tag)
    return " ".join(unique[:limit])


def format_event(event, config) -> str:
    severity = _get(event, "severity", "context")
    category = _get(event, "category", "context")
    title = _get(event, "title_fa") or "بدون تیتر"
    body = util.truncate(
        _get(event, "body_fa"), int(config.get("app.max_body_chars", 700))
    )
    confidence = _get(event, "confidence", "single")
    source = _get(event, "primary_source") or "نامشخص"
    url = _get(event, "primary_url") or ""
    when = _get(event, "first_seen")

    lines = [
        f"{SEVERITY_ICON.get(severity, '⚪')} <b>{SEVERITY_LABEL.get(severity, 'خبر')}</b>"
        f" | {CATEGORY_LABEL.get(category, 'خبر')}",
        f"🕐 {util.format_moment(when)}",
        "",
        f"<b>{html.escape(title)}</b>",
    ]
    if body:
        lines += ["", html.escape(body)]

    airlines = _split(_get(event, "airlines"))
    if airlines:
        lines += ["", "✈️ " + " — ".join(html.escape(a) for a in airlines)]

    countries = [COUNTRY_LABEL.get(c, c) for c in _split(_get(event, "countries"))]
    if countries:
        lines.append("🌍 " + "، ".join(html.escape(c) for c in countries))

    lines += ["", f"📌 منبع: {html.escape(source)}"]
    if url.startswith("http"):
        lines.append(f'🔗 <a href="{html.escape(url)}">متن اصلی خبر</a>')

    badge = CONFIDENCE_LABEL.get(confidence, "")
    if badge:
        lines.append(badge)
    if not _get(event, "numbers_ok", 1):
        lines.append("⚠️ اعداد این ترجمه نیازمند بازبینی است")

    lines += ["", hashtags(event)]
    return "\n".join(lines).strip()


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
