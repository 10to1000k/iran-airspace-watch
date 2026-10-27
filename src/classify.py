"""دسته‌بندی، تعیین شدت و فیلتر ربط.

فیلتر سخت‌گیرانه: یک خبر باید هم «piš» (ای런 یاazi 존재) داشته باشد
و هم «دقت هوانوردی» (басار aviation signal) داشته باشد تا در نظر گرفته شود.
"""

from __future__ import annotations

import re

from . import util
from .airlines import AirlineIndex
from .models import Classification, RawItem

_CATEGORY_PRIORITY = ["airspace", "airport", "airline", "context"]

# کلماتی که در متن فارسی ظاهر می‌شوند اما هرگز نشان‌دهنده‌ی ایران نیستند
# (جلوگیری از مثبت کاذب مثل «ماهانه» → ماهان ایر)
_PERSIAN_FALSE_POSITIVES = [
    "ماهانه",
    "هفته‌ای",
    "روزانه",
    "سالانه",
    "آسمانی",
    "هواشناسی",
    "آب‌وهوایی",
]

# حداقل امتیاز لازم برای tether relevance (0 تا 1)
_MIN_RELEVANCE_SCORE = 0.35

# «تهران-ایرنا» و «TEHRAN (IRNA)» ابتدای خبر، محل انتشار را نشان می‌دهند
# نه محل رخداد. اگر حذف نشوند، خبر فرودگاه عربستان «ایران‌محور» می‌شود.
_DATELINE_RE = re.compile(
    r"^\s*(?:"
    r"تهران\s*[-–—]\s*(?:ایرنا|مهر|ایسنا|تسنیم|فارس)\s*[-–—]\s*"
    r"|(?:TEHRAN|Tehran)\s*[\(\,]\s*(?:IRNA|Tasnim|Mehr|ISNA|FNA|MNA)\s*[\)]?\s*[-–—]?\s*"
    r")",
    re.IGNORECASE,
)


def _strip_dateline(text: str) -> str:
    """سرصفحه‌ی خبرگزاری را از ابتدای متن حذف می‌کند."""
    return _DATELINE_RE.sub("", text or "", count=1)


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
        self._foreign_airports = (
            self.settings.get("relevance", {}).get("foreign_airport_keywords") or []
        )
        self._non_operational = (
            self.settings.get("relevance", {}).get("non_operational_keywords") or []
        )
        self._airport_codes = (
            self.settings.get("relevance", {}).get("airport_codes") or []
        )

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

    def _persian_false_positive(self, text: str) -> bool:
        """کلمات فارسی که نشان‌دهنده‌ی ایران نیستند (مثل ماهانه ≠ ماهان)."""
        norm = util.normalize(text)
        for fp in _PERSIAN_FALSE_POSITIVES:
            if re.search(rf"(?<!\w){re.escape(fp)}(?!\w)", norm):
                return True
        return False

    def _is_relevant(self, text: str, airports: list[str], airlines: list[str]) -> bool:
        """فیلتر سخت‌گیرانه‌ی ربط.

        یک خبر باید هم muestran ایران/منطقه داشته باشد و هم سیگنال هوانوردی.
        فقط یکی از آن‌ها کافی نیست.

        returned: True اگر الخبر مربوط است، False در غیر این صورت.
        """
        norm = util.normalize(text)
        non_operational = self._hit(norm, self._non_operational)
        foreign_airport = self._hit(norm, self._foreign_airports)
        airspace_signal = self._hit(norm, self._airspace_signal)
        operational = airspace_signal or (
            self._hit(norm, self._strong) and not non_operational
        )

        # ── ۱. کد فرودگاه ایرانی (OIIE, IKA و غیره) ──
        if airports:
            # کد فرودگاه به‌تنهایی کافی نیست؛ باید یک نشانه‌ی عملیاتی هم باشد.
            if operational:
                return True
            weak_hits = sum(1 for kw in self._aviation if _contains(norm, kw))
            return weak_hits >= 2

        # ── ۲. ایرلاین ایرانی ──
        iranian_airlines = [a for a in airlines if self.airlines.is_iranian(a)]
        if iranian_airlines:
            # نام ایرلاین به‌تنهایی کافی نیست: خبر بازنشستگی/پرسنل/بودجه هم
            # نام «ایران ایر» را دارد. باید نشانه‌ی عملیاتی واقعی باشد.
            if non_operational and not airspace_signal:
                return False
            return operational

        # ── ۳. ایرلاین خارجی + منطقه + سیگنال حریم هوایی ──
        # این قاعده پیش از الزام «واژه‌ی ایران» می‌آید، چون تعطیلی حریم هوایی
        # خاورمیانه حتی بدون نام ایران هم برای مخاطب ایرانی مهم است.
        if airlines and self._hit(norm, self._region) and airspace_signal:
            return True

        # ── ۴. متاضد Kandidaten کلمات relacionadas با ایران ──
        if not self._hit(norm, self._relevance):
            return False

        # جلوگیری از مثبت کاذب فارسی
        if self._persian_false_positive(norm):
            # اگر کلمه‌ی False Positive وجود دارد،航空 سیگنال القوی لازم است
            if not self._hit(norm, self._strong):
                return False

        # ── ۵. سیگنال عملیاتی حریم هوایی ──
        if airspace_signal:
            return True

        # ── ۶. conflicto (اگر فعال شده باشد) ──
        if self._include_conflict and self._hit(norm, self._conflict):
            return True

        # فرودگاه خارجی + اشاره‌ی سیاسی/نظامی به ایران، بدون عملیات هوایی،
        # خبر ایران‌محور نیست (مثلاً حمله به فرودگاه ریاض و متهم کردن ایران).
        if foreign_airport:
            return False

        # ── ۷. کلمات starke aviation (به‌جز واژه‌های غیرعملیاتی) ──
        if operational:
            return True

        # ── ۸. کلمات ضعیف هوانوردی (باید حداقل دو مورد باشد) ──
        weak_hits = sum(1 for kw in self._aviation if _contains(norm, kw))
        return weak_hits >= 2

    # ── اصلی ─────────────────────────────────────────────────
    def classify(self, item: RawItem) -> Classification:
        # سقف طول متن: صفحه‌های بلند به‌طور تصادفی کلیدواژه‌های ایران و هوانوردی را کنار هم دارند
        raw_text = (item.text or "")[: self._max_match_chars]
        # سرصفحه‌ی «تهران-ایرنا» نباید به‌عنوان نشانه‌ی ایران شمرده شود.
        content_raw = _strip_dateline(raw_text)
        text = util.normalize(content_raw)
        airports = self._airports_in(raw_text)
        # مهم: متن اصلی (قبل از lowercase شدن) به AirlineIndex داده می‌شود تا کدهای ۳حرفی
        # حروف بزرگ مثل THY، PIA، KLM، IRA و IRM درست تشخیص داده شوند.
        airlines = self.airlines.match(raw_text)

        if not self._is_relevant(text, airports, airlines):
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

        # کشورها را از سیگنال‌های واقعی ایران بساز؛ «IR» را به همه‌ی رخدادها نچسبان.
        # country در منبع یعنی کشور ناشر، نه کشور رخداد: خبرگزاری ایرانی ممکن است
        # خبری درباره‌ی عربستان منتشر کند. پس IR فقط از محتوا می‌آید.
        countries: set[str] = set()
        iran_signal = (
            bool(airports)
            or any(self.airlines.is_iranian(a) for a in airlines)
            or self._hit(text, self._relevance)
        )
        if iran_signal:
            countries.add("IR")
        if item.country and item.country not in ("INTL", "", "IR"):
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
            "SA": [
                "عربستان",
                "saudi",
                "riyadh",
                "jeddah",
                "dammam",
                "ریاض",
                "جده",
                "دمام",
            ],
        }
        for code, words in country_map.items():
            if any(_contains(text, w) for w in words):
                countries.add(code)

        # ── امتیاز اعتماد (confidence score) ──
        confidence = self._compute_confidence(
            text, category, severity, airports, airlines, matched
        )

        return Classification(
            relevant=True,
            category=category,
            severity=severity,
            countries=sorted(countries),
            airlines=airlines,
            airports=airports,
            matched=matched,
            confidence_score=confidence,
        )

    def _compute_confidence(
        self,
        text: str,
        category: str,
        severity: str,
        airports: list[str],
        airlines: list[str],
        matched: list[str],
    ) -> float:
        """محاسبه‌ی امتیاز اعتماد (0 تا 1) برای یک خبر."""
        score = 0.0
        norm = util.normalize(text)

        # کد فرودگاه → اعتماد بالا
        if airports:
            score += 0.35

        # ایرلاین riconosci شده
        if airlines:
            score += 0.20

        # کلمات mãیseverity críticos
        if severity == "critical":
            score += 0.20
        elif severity == "important":
            score += 0.10

        # دسته‌بندی الهواء ou mwd (دقیق‌تر از context)
        if category in ("airspace", "airport"):
            score += 0.15

        # matched کلمات critically
        if matched:
            score += 0.10

        # منابع رسمی (tier 0) با events già است (در pipeline 다루 می‌شود)
        # اینجا فقط از متن کلماتミها استفاده می‌کنیم

        return min(score, 1.0)
