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
        for words in (
            self.settings.get("relevance", {}).get("keywords") or {}
        ).values():
            self._relevance.extend(words or [])
        self._aviation = (
            self.settings.get("relevance", {}).get("aviation_keywords") or []
        )
        self._strong = (
            self.settings.get("relevance", {}).get("strong_aviation_keywords") or []
        )
        self._region = self.settings.get("relevance", {}).get("region_keywords") or []
        # قانون منطقه فقط با سیگنال‌های واقعی حریم هوایی فعال می‌شود، نه با «airline».
        self._airspace_signal = (
            self.settings.get("relevance", {}).get("airspace_signal_keywords")
            or self._strong
        )
        self._include_conflict = bool(
            self.settings.get("relevance", {}).get(
                "include_iran_conflict_context", False
            )
        )
        self._conflict = (
            self.settings.get("relevance", {}).get("conflict_keywords") or []
        )
        self._airport_codes = (
            self.settings.get("relevance", {}).get("airport_codes") or []
        )
        self._negative_title = (
            self.settings.get("relevance", {}).get("negative_title_keywords") or []
        )
        # هشتگ‌های پلتفرم‌ها (تلگرام/X) از config/sources.yaml
        hashtags = (config.sources or {}).get("hashtags") or {}
        self._hashtags_iran = [
            str(tag).lower() for tag in (hashtags.get("iran") or [])
        ]
        self._hashtags_aviation = [
            str(tag).lower() for tag in (hashtags.get("aviation") or [])
        ]

        self._categories: dict[str, list[str]] = {}
        self._base_severity: dict[str, str] = {}
        for name, spec in (self.settings.get("categories") or {}).items():
            self._categories[name] = spec.get("keywords") or []
            self._base_severity[name] = spec.get("severity", "context")

        self._critical = self.settings.get("severity", {}).get("critical") or []
        self._max_match_chars = int(
            self.settings.get("relevance", {}).get("max_match_chars", 1200)
        )

    # ── کمکی ─────────────────────────────────────────────────
    @staticmethod
    def _hit(text: str, keywords: list[str]) -> bool:
        return any(_contains(text, kw) for kw in keywords)

    @staticmethod
    def _distinct_hits(text: str, keywords: list[str]) -> list[str]:
        """کلیدواژه‌های متمایز؛ flight و flights یک مفهوم‌اند و دو بار شمرده نمی‌شوند.

        نسخه‌ی قبلی هر دو را جدا می‌شمرد و یک اشاره‌ی گذرا در بدنه
        (مثل «flights operate normally») به‌تنهایی آستانه‌ی ربط را رد می‌کرد.
        """
        hits: list[str] = []
        for kw in sorted(keywords, key=len, reverse=True):
            norm = util.normalize(kw)
            if not norm or not _contains(text, kw):
                continue
            if any(norm in hit or hit in norm for hit in hits):
                continue
            hits.append(norm)
        return hits

    def _hashtag_pair(self, text: str) -> bool:
        """جفت هشتگ ایران + هوانوردی در پست‌های پلتفرمی نشانه‌ی موضوع اصلی است."""
        if not self._hashtags_iran or not self._hashtags_aviation:
            return False
        return any(tag in text for tag in self._hashtags_iran) and any(
            tag in text for tag in self._hashtags_aviation
        )

    def _airports_in(self, raw_text: str) -> list[str]:
        """کدهای فرودگاه فقط با حروف بزرگ شمرده می‌شوند.

        نسخه‌ی قبلی متن را lowercase می‌کرد و «IKA» با اسم «Ika Kartika»
        قاطی شد؛ نتیجه‌اش انتشار یک خبر اندونزی بود.
        """
        found = []
        for code in self._airport_codes:
            if re.search(rf"\b{re.escape(code.upper())}\b", raw_text):
                found.append(code.upper())
        return found

    def _is_relevant(
        self,
        text: str,
        airports: list[str],
        airlines: list[str],
        hashtag_pair: bool = False,
    ) -> bool:
        """ایران + هوانوردی، هر دو لازم‌اند.

        فقط «ایران» کافی نیست: خبر ورزشی و شهری هم «ایران» دارد.
        «پرواز» تنها هم کافی نیست: «پرواز تیم ملی به فینال» خبر ورزشی است.
        """
        if hashtag_pair:
            return True
        if airports:
            return True
        # ایرلاین ایرانی خودش «موجودیت ایرانی» است: خبر «ماهان ایر در گوانگژو»
        # لزوماً کلمه‌ی ایران را در متن ندارد.
        if any(self.airlines.is_iranian(name) for name in airlines):
            return True
        if (
            airlines
            and self._hit(text, self._region)
            and self._hit(text, self._airspace_signal)
        ):
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
        raw_text = (item.text or "")[: self._max_match_chars]
        text = util.normalize(raw_text)
        airports = self._airports_in(raw_text)
        # مهم: متن اصلی (قبل از lowercase شدن) به AirlineIndex داده می‌شود تا کدهای ۳حرفی
        # حروف بزرگ مثل THY، PIA، KLM، IRA و IRM درست تشخیص داده شوند.
        airlines = self.airlines.match(raw_text)

        hashtag_pair = self._hashtag_pair(text)
        if not self._is_relevant(text, airports, airlines, hashtag_pair):
            return Classification(relevant=False, matched=[])

        scores = {
            name: sum(1 for kw in words if _contains(text, kw))
            for name, words in self._categories.items()
        }
        best = max(
            scores, key=lambda name: (scores[name], -_CATEGORY_PRIORITY.index(name))
        )
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
            "AE": ["امارات", "uae", "emirates", "dubai", "دبی", "оаэ", "الإمارات"],
            "RU": ["روسیه", "russia", "moscow", "مسکو", "россия", "روس"],
            "CN": [
                "چین",
                "china",
                "beijing",
                "guangzhou",
                "پکن",
                "گوانگژو",
                "китай",
                "中国",
            ],
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

        publishable = self._is_publishable(
            item=item,
            category=category,
            severity=severity,
            airports=airports,
            airlines=airlines,
            hashtag_pair=hashtag_pair,
        )

        return Classification(
            relevant=True,
            category=category,
            severity=severity,
            countries=sorted(countries),
            airlines=airlines,
            airports=airports,
            matched=matched,
            publishable=publishable,
        )

    def _is_publishable(
        self,
        *,
        item: RawItem,
        category: str,
        severity: str,
        airports: list[str],
        airlines: list[str],
        hashtag_pair: bool,
    ) -> bool:
        """آیا خبر «موضوع اصلی» است و باید همان لحظه برای کاربر برود؟

        فلسفه: ربط (relevant) یعنی جایی در دنیای ایران+هوانوردی است؛ اما
        ارسال فوری فقط وقتی است که تیتر یا موجودیت‌های خبر خودشان موضوع
        اصلی باشند. اشاره‌ی اتفاقی در بدنه (خبر خودرو/سیاست که یک جمله
        «فرودگاه» دارد) ثبت می‌شود ولی پوش نمی‌شود — شکایت اصلی کاربر
        از خبرهای اشتباهی همین‌ها بودند.
        """
        title_raw = item.title or ""
        title_text = util.normalize(title_raw)
        body_text = util.normalize(item.body or "")

        airports_title = self._airports_in(title_raw)
        airlines_title = self.airlines.match(title_raw)
        iran_title = self._hit(title_text, self._relevance)
        strong_title = self._distinct_hits(title_text, self._strong)
        weak_title = self._distinct_hits(title_text, self._aviation)
        strong_body = self._distinct_hits(body_text, self._strong)
        signal_body = self._distinct_hits(body_text, self._airspace_signal)

        publishable = False
        if airports or any(self.airlines.is_iranian(name) for name in airlines):
            publishable = True
        elif hashtag_pair:
            # پست کوتاه پلتفرمی کلش «تیتر» است؛ جفت هشتگ موضوع را مشخص می‌کند
            publishable = True
        elif (
            airlines
            and self._hit(util.normalize(item.text or ""), self._region)
            and self._hit(util.normalize(item.text or ""), self._airspace_signal)
        ):
            publishable = True
        elif iran_title and (strong_title or airlines_title or len(weak_title) >= 2):
            publishable = True
        elif airlines_title and iran_title:
            publishable = True
        elif (iran_title or airlines_title or airports_title) and signal_body and strong_body:
            # تیتر موضوع را دارد و بدنه سیگنال واقعی حریم هوایی/لغو/تعلیق دارد
            publishable = True
        elif category in ("airspace", "airport", "airline") and severity in (
            "critical",
            "important",
        ) and (strong_title or weak_title or airlines_title or airports_title):
            publishable = True

        # وتوی موضوعات پرت: ورزش/سرگرمی در تیتر، مگر موجودیت هوانوردی در تیتر باشد
        if publishable and self._hit(title_text, self._negative_title):
            if not (airports_title or airlines_title or strong_title):
                publishable = False
        return publishable
