"""دسته‌بندی، تعیین شدت و فیلتر ربط."""

from __future__ import annotations

import re

from . import util
from .airlines import AirlineIndex
from .models import Classification, RawItem

_CATEGORY_PRIORITY = ["airspace", "airport", "airline", "context"]


def _contains(text: str, keyword: str) -> bool:
    """کلمات کوتاه لاتین باید مرز کلمه داشته باشند تا «THR» با «through» قاطی نشود."""
    kw = util.normalize(keyword)
    if not kw:
        return False
    if len(kw) <= 4 and kw.isascii():
        return re.search(rf"\b{re.escape(kw)}\b", text) is not None
    return kw in text


class Classifier:
    def __init__(self, config):
        self.cfg = config
        self.settings = config.settings
        self.airlines = AirlineIndex(config.airlines)

        self._relevance: list[str] = []
        for words in (self.settings.get("relevance", {}).get("keywords") or {}).values():
            self._relevance.extend(words or [])
        self._aviation = self.settings.get("relevance", {}).get("aviation_keywords") or []
        self._strong = self.settings.get("relevance", {}).get("strong_aviation_keywords") or []
        self._region = self.settings.get("relevance", {}).get("region_keywords") or []
        # قانون منطقه فقط با سیگنال‌های واقعی حریم هوایی فعال می‌شود، نه با «airline».
        self._airspace_signal = (
            self.settings.get("relevance", {}).get("airspace_signal_keywords") or self._strong
        )
        self._include_conflict = bool(
            self.settings.get("relevance", {}).get("include_iran_conflict_context", False)
        )
        self._conflict = self.settings.get("relevance", {}).get("conflict_keywords") or []
        self._airport_codes = self.settings.get("relevance", {}).get("airport_codes") or []

        self._categories: dict[str, list[str]] = {}
        self._base_severity: dict[str, str] = {}
        for name, spec in (self.settings.get("categories") or {}).items():
            self._categories[name] = spec.get("keywords") or []
            self._base_severity[name] = spec.get("severity", "context")

        self._critical = self.settings.get("severity", {}).get("critical") or []
        self._max_match_chars = int(self.settings.get("relevance", {}).get("max_match_chars", 1200))

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

    def _is_relevant(self, text: str, airports: list[str], airlines: list[str]) -> bool:
        """ایران + هوانوردی، هر دو لازم‌اند.

        فقط «ایران» کافی نیست: خبر ورزشی و شهری هم «ایران» دارد.
        «پرواز» تنها هم کافی نیست: «پرواز تیم ملی به فینال» خبر ورزشی است.
        """
        if airports:
            return True
        if airlines and self._hit(text, self._region) and self._hit(text, self._airspace_signal):
            # مثل «Emirates suspends flights across Middle East airspace»
            return True
        if not self._hit(text, self._relevance):
            return False
        if self._include_conflict and self._hit(text, self._conflict):
            # خبر ژئوپلیتیک ایران، فقط اگر خودت روشن کرده باشی
            return True
        if airlines:
            return True
        if self._hit(text, self._strong):
            return True
        weak_hits = sum(1 for kw in self._aviation if _contains(text, kw))
        return weak_hits >= 2

    # ── اصلی ─────────────────────────────────────────────────
    def classify(self, item: RawItem) -> Classification:
        # سقف طول متن: صفحه‌های بلند به‌طور تصادفی کلیدواژه‌های ایران و هوانوردی را کنار هم دارند
        text = util.normalize(item.text)[: self._max_match_chars]
        airports = self._airports_in(text)
        airlines = self.airlines.match(text)

        if not self._is_relevant(text, airports, airlines):
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
            severity = "critical" if scores[category] >= 2 else "important"
        else:
            severity = self._base_severity.get(category, "context")

        # ایران همیشه در کشورها هست
        countries = {"IR"}
        if item.country and item.country not in ("INTL", ""):
            countries.add(item.country)
        country_map = {
            "TR": ["ترکیه", "turkey", "türkiye", "турция"],
            "AE": ["امارات", "uae", "emirates", "оаэ", "الإمارات"],
            "RU": ["روسیه", "russia", "россия", "روس"],
            "CN": ["چین", "china", "китай", "中国"],
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
