"""NOTAM: تجزیه، دیکود فارسی، و کالکتور.

چرا این ماژول مهم است: ترجمه‌ی ماشینی خامِ NOTAM بی‌معنی است.
ما ساختار را تجزیه می‌کنیم و با دیکشنری هوانوردی به فارسی قابل‌فهم تبدیل می‌کنیم،
بعد متن اصلی را هم نگه می‌داریم تا LLM آن را روان کند.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from . import util
from .models import RawItem

log = logging.getLogger(__name__)

_HEADER_RE = re.compile(r"(?P<number>[A-Z]\d{4}/\d{2})\s+(?P<kind>NOTAM[NR]|NOTAMC)", re.I)
_FIELD_RE = re.compile(r"(?:^|\s)(?P<key>[QABCDEFG])\)\s*")
_STAMP_RE = re.compile(r"^\d{10}$")


@dataclass
class Notam:
    number: str
    kind: str
    fir: str = ""
    qcode: str = ""
    location: str = ""
    start: datetime | None = None
    end: datetime | None = None
    schedule: str = ""
    text: str = ""
    lower: str = ""
    upper: str = ""
    raw: str = ""


def _parse_stamp(value: str) -> datetime | None:
    value = util.to_ascii_digits((value or "").strip())
    if not _STAMP_RE.match(value):
        return None
    try:
        return datetime.strptime(value, "%y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fields(raw: str) -> dict[str, str]:
    matches = list(_FIELD_RE.finditer(raw))
    out: dict[str, str] = {}
    for i, match in enumerate(matches):
        key = match.group("key")
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        value = raw[start:end].strip()
        if key == "E":
            # متن E ممکن است چند خطی باشد و با پرانتز تمام شود
            value = value.rstrip(")").strip()
        out[key] = value
    return out


def parse_notam(raw: str) -> Notam | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    header = _HEADER_RE.search(raw)
    if not header:
        return None

    fields = _fields(raw)
    notam = Notam(
        number=header.group("number").upper(),
        kind=header.group("kind").upper(),
        raw=raw,
        text=fields.get("E", ""),
        schedule=fields.get("D", ""),
    )

    q = (fields.get("Q") or "").split("/")
    if len(q) >= 2:
        notam.fir = q[0].strip()
        notam.qcode = q[1].strip().upper()
    if len(q) >= 7:
        notam.lower = q[5].strip()
        notam.upper = q[6].strip()

    notam.location = (fields.get("A") or notam.fir).strip().upper()
    notam.start = _parse_stamp(fields.get("B", ""))
    notam.end = _parse_stamp(fields.get("C", ""))
    return notam


def _airport_label(code: str, glossary: dict) -> str:
    info = (glossary.get("airports") or {}).get(code.upper())
    if not info:
        return code.upper()
    fa = info.get("fa", code)
    iata = info.get("iata")
    return f"{fa} ({iata}/{code.upper()})" if iata else f"{fa} ({code.upper()})"


def _time_span(notam: Notam) -> str:
    if notam.end is None:
        return "زمان پایان اعلام نشده"
    if notam.start is None:
        return f"تا {util.format_moment(notam.end)}"
    return f"از {util.format_moment(notam.start)} تا {util.format_moment(notam.end)}"


def decode_notam(notam: Notam, glossary: dict) -> str:
    """خلاصه‌ی فارسی ساختاریافته — ورودی LLM و همچنین نسخه‌ی پشتیبان."""
    qcodes = glossary.get("qcodes") or {}
    terms = glossary.get("terms") or {}

    reason = qcodes.get(notam.qcode)
    if not reason:
        reason = f"اطلاعیه‌ی هوانوردی (کد {notam.qcode or 'نامشخص'})"

    text_fa = notam.text or ""
    for en, fa in sorted(terms.items(), key=lambda kv: len(kv[0]), reverse=True):
        text_fa = re.sub(re.escape(en), fa, text_fa, flags=re.I)

    parts = [
        f"NOTAM {notam.number}: {reason}.",
        f"محل: {_airport_label(notam.location, glossary)}.",
        f"بازه: {_time_span(notam)}.",
    ]
    if notam.lower or notam.upper:
        if notam.lower in ("000", "0") and notam.upper in ("999", "99999"):
            parts.append("سطح: همه‌ی سطوح (از زمین تا بالا).")
        else:
            parts.append(f"سطح: {notam.lower or '?'} تا {notam.upper or '?'}.")
    if notam.schedule:
        parts.append(f"زمان‌بندی روزانه: {notam.schedule}.")
    if text_fa:
        parts.append(f"متن اصلی: {text_fa}")
    return " ".join(parts)


class NotamCollector:
    """کالکتور API رسمی FAA. بدون اعتبارنامه غیرفعال می‌شود و یک بار هشدار می‌دهد."""

    API = "https://external-api.faa.gov/notamapi/v1/notams"
    LOCATIONS = ("OIIE", "OIII", "OIIX")

    def __init__(self, config):
        self.cfg = config
        self.client_id = config.env_get("FAA_NOTAM_CLIENT_ID")
        self.client_secret = config.env_get("FAA_NOTAM_CLIENT_SECRET")
        self.glossary = config.glossary
        self._warned = False

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def poll(self) -> list[RawItem]:
        if not self.enabled:
            if not self._warned:
                log.warning(
                    "NOTAM غیرفعال است: FAA_NOTAM_CLIENT_ID/SECRET تنظیم نشده. "
                    "ثبت رایگان: https://external-api.faa.gov/notamapi"
                )
                self._warned = True
            return []

        items: list[RawItem] = []
        headers = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "Accept": "application/json",
        }
        for location in self.LOCATIONS:
            try:
                response = requests.get(
                    self.API,
                    params={"icaoLocation": location, "pageSize": 50, "pageNum": 1},
                    headers=headers,
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("خطای NOTAM برای %s: %s", location, exc)
                continue
            for raw in _extract_notam_texts(payload):
                notam = parse_notam(raw)
                if not notam:
                    continue
                if notam.kind == "NOTAMC":
                    continue  # لغو اطلاعیه‌ی قبلی، خودش خبر نیست
                items.append(
                    RawItem(
                        source_id=f"notam_{notam.location.lower()}",
                        source_name=f"NOTAM — {_airport_label(notam.location, self.glossary)}",
                        source_url="https://www.notams.faa.gov/",
                        title=f"NOTAM {notam.number} — {notam.location}",
                        body=decode_notam(notam, self.glossary),
                        url=f"https://www.notams.faa.gov/dinsQueryWeb/",
                        lang="en",
                        country="IR",
                        kind="notam",
                        published_at=notam.start,
                        extra={
                            "tier": 0,
                            "notam_number": notam.number,
                            "raw_notam": notam.raw,
                            "decoded_fa": decode_notam(notam, self.glossary),
                        },
                    )
                )
        return items


def _extract_notam_texts(payload) -> list[str]:
    """استخراج تحمل‌پذیر متن NOTAM از پاسخ API (ساختار پاسخ نسخه‌به‌نسخه فرق می‌کند)."""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in {"text", "notamtext", "rawtext", "notam"} and isinstance(value, str):
                    if _HEADER_RE.search(value):
                        found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    # حذف تکراری‌ها با حفظ ترتیب
    seen, unique = set(), []
    for text in found:
        key = util.fingerprint(text)
        if key not in seen:
            seen.add(key)
            unique.append(text)
    return unique
