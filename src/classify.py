"""دسته‌بندی، تعیین شدت و فیلتر ربط."""

from __future__ import annotations

import re

from . import util
from .airlines import AirlineIndex
from .models import Classification, RawItem
from .social import expand_hashtags

_CATEGORY_PRIORITY = ["airspace", "airport", "airline", "context"]


def _contains(text: str, keyword: str) -> bool:
    """کلمات کوتاه لاتین باید مرز کلمه داشته باشند تا «THR» با «through» قاطی نشود."""
    kw = util.normalize(keyword)
    if not kw:
        return False
    if len(kw) <= 4 and kw.isascii():
        return re.search(rf"\b{re.escape(kw)}\b", text) is not None
    return kw in text


def _term_spans(text: str, keywords: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for keyword in keywords:
        kw = util.normalize(keyword)
        if not kw:
            continue
        pattern = (
            re.compile(rf"\b{re.escape(kw)}\b")
            if len(kw) <= 4 and kw.isascii()
            else re.compile(re.escape(kw))
        )
        spans.extend(match.span() for match in pattern.finditer(text))
    return spans


def _nearby(text: str, left: list[str], right: list[str], max_distance: int = 240) -> bool:
    """سیگنال‌های دو طرف باید در یک بخش کوتاه باشند، نه دو جای دور از هم در خبر."""
    left_spans = _term_spans(text, left)
    right_spans = _term_spans(text, right)
    for left_start, left_end in left_spans:
        for right_start, right_end in right_spans:
            distance = max(0, max(left_start - right_end, right_start - left_end))
            if distance <= max_distance:
                return True
    return False


class Classifier:
    def __init__(self, config):
        self.cfg = config
        self.settings = config.settings
        self.airlines = AirlineIndex(config.airlines)
        self._iranian_airline_terms: dict[str, list[str]] = {}
        for entry in config.airlines:
            if entry.get("country") != "IR":
                continue
            terms = [entry.get("name", ""), *(entry.get("aliases") or [])]
            self._iranian_airline_terms[entry["name"]] = [
                term for term in terms if len(util.normalize(term)) >= AirlineIndex.MIN_ALIAS
            ]

        self._relevance: list[str] = []
        for words in (self.settings.get("relevance", {}).get("keywords") or {}).values():
            self._relevance.extend(words or [])
        self._service = self.settings.get("relevance", {}).get("service_keywords") or []
        self._direct_service = self.settings.get("relevance", {}).get("direct_service_keywords") or []
        self._non_aviation_title = self.settings.get("relevance", {}).get("non_aviation_title_keywords") or []
        self._airport_codes = self.settings.get("relevance", {}).get("airport_codes") or []

        self._categories: dict[str, list[str]] = {}
        self._base_severity: dict[str, str] = {}
        for name, spec in (self.settings.get("categories") or {}).items():
            self._categories[name] = spec.get("keywords") or []
            self._base_severity[name] = spec.get("severity", "context")

        self._critical = self.settings.get("severity", {}).get("critical") or []
        self._max_match_chars = int(self.settings.get("relevance", {}).get("max_match_chars", 1200))
        self._relevance_window_chars = int(
            self.settings.get("relevance", {}).get("relevance_window_chars", 420)
        )

    # ── کمکی ─────────────────────────────────────────────────
    @staticmethod
    def _hit(text: str, keywords: list[str]) -> bool:
        return any(_contains(text, kw) for kw in keywords)

    def _airports_in(self, text: str) -> list[str]:
        found = []
        for code in self._airport_codes:
            if _contains(text, code):
                found.append(code.upper())
        return found

    def _is_relevant(
        self, text: str, title_text: str, airports: list[str], airlines: list[str]
    ) -> bool:
        """ایران و یک نشانه‌ی مشخص از هوانوردی/شرکت/زیرساخت باید در تیتر/لید نزدیک باشند."""
        # «پرواز تیم ملی به فینال» خبر خدمات هوایی نیست؛ مگر تیتر نشانه‌ی مستقیم عملیاتی داشته باشد.
        if (
            self._hit(title_text, self._non_aviation_title)
            and not self._hit(title_text, self._direct_service)
        ):
            return False
        if not self._hit(text, self._service):
            return False

        # حتی کد فرودگاه/نام ایرلاین ایرانی باید نزدیک به نشانه‌ی خدمت باشد؛
        # اشاره‌های جداگانه در تیتر و انتهای لید نباید با هم مثبت کاذب بسازند.
        if airports and _nearby(text, airports, self._service):
            return True

        iranian_airline_terms = [
            term
            for name in airlines
            if self.airlines.is_iranian(name)
            for term in self._iranian_airline_terms.get(name, [name])
        ]
        if iranian_airline_terms and _nearby(text, iranian_airline_terms, self._service):
            return True

        # خبر ایرلاین خارجی باید ایران را به خدمت هوایی ربط دهد؛ صرف «خاورمیانه» کافی نیست.
        return _nearby(text, self._relevance, self._service)


    # ── اصلی ─────────────────────────────────────────────────
    def classify(self, item: RawItem) -> Classification:
        # فقط تیتر و ابتدای لید برای تصمیم ربط بررسی می‌شوند؛ متن‌های بلند معمولاً
        # کلمات نامرتبط را در نقاط دور از هم دارند و مثبت کاذب می‌سازند.
        lead_chars = min(self._relevance_window_chars, self._max_match_chars)
        raw_text = f"{item.title or ''}\n{(item.body or '')[:lead_chars]}"[: self._max_match_chars]
        expanded_text = expand_hashtags(raw_text)
        text = util.normalize(expanded_text)
        title_text = util.normalize(expand_hashtags(item.title or ""))
        airports = self._airports_in(text)
        # مهم: متن اصلی (قبل از lowercase شدن) به AirlineIndex داده می‌شود تا کدهای ۳حرفی
        # حروف بزرگ مثل THY، PIA، KLM، IRA و IRM درست تشخیص داده شوند.
        airlines = self.airlines.match(expanded_text)

        if not self._is_relevant(text, title_text, airports, airlines):
            return Classification(relevant=False, matched=[])

        scores = {
            name: sum(1 for kw in words if _contains(text, kw))
            for name, words in self._categories.items()
        }
        best = max(scores, key=lambda name: (scores[name], -_CATEGORY_PRIORITY.index(name)))
        category = best if scores[best] > 0 else "context"

        matched = [kw for kw in self._critical if _contains(text, kw)]
        if matched:
            # خبر «زمینه» با کلمه‌ی تند (جنگ/حمله) نباید هم‌ارز بستن حریم هوایی شود
            severity = "important" if category == "context" else "critical"
        elif category in ("airspace", "airport"):
            # تکرار واژه‌ی فرودگاه/FIR، شدت عملیاتی را ثابت نمی‌کند؛ خبر توسعه و مدیریت
            # فرودگاه هم relevant است ولی بدون نشانه‌ی اضطرار، نباید critical شود.
            severity = self._base_severity.get(category, "important")
        else:
            severity = self._base_severity.get(category, "context")

        # ایران همیشه در کشورها هست
        countries = {"IR"}
        if item.country and item.country not in ("INTL", ""):
            countries.add(item.country)
        country_map = {
            "TR": ["ترکیه", "turkey", "türkiye", "турция"],
            "AE": ["امارات", "uae", "emirates", "dubai", "دبی", "оаэ", "الإمارات"],
            "RU": ["روسیه", "russia", "moscow", "مسکو", "россия", "روس"],
            "CN": ["چین", "china", "beijing", "guangzhou", "پکن", "گوانگژو", "китай", "中国"],
            "PK": ["پاکستان", "pakistan", "karachi", "lahore"],
            "TH": ["تایلند", "thailand", "bangkok", "phuket", "بانکوک", "پوکت"],
            "VN": ["ویتنام", "vietnam", "hanoi"],
            "IQ": ["عراق", "iraq", "najaf", "baghdad", "نجف", "بغداد"],
            "AF": ["افغانستان", "afghanistan", "kabul", "کابل"],
            "QA": ["قطر", "qatar", "doha", "دوحه"],
            "OM": ["عمان", "oman", "muscat", "مسقط"],
            "AZ": ["آذربایجان", "azerbaijan", "baku", "باکو"],
            "GE": ["گرجستان", "georgia", "tbilisi", "تفلیس"],
        }
        for code, words in country_map.items():
            if any(_contains(text, w) for w in words):
                countries.add(code)

        return Classification(
            relevant=True,
            category=category,
            severity=severity,
            countries=sorted(countries),
            airlines=airlines,
            airports=airports,
            matched=matched,
        )
