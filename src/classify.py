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

# جداسازی camelCase برای هشتگ: «TehranFIR» → «Tehran FIR»، «MahanAir» → «Mahan Air»
_HASHTAG_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


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
        self._negative_title = (
            self.settings.get("relevance", {}).get("negative_title_keywords") or []
        )
        self._off_topic_title = (
            self.settings.get("relevance", {}).get("off_topic_title_keywords") or []
        )
        # هشتگ‌های پلتفرم‌ها (تلگرام/X) از config/sources.yaml
        hashtags_cfg = (config.sources or {}).get("hashtags") or {}
        self._hashtags_iran = [
            str(tag).lower() for tag in (hashtags_cfg.get("iran") or [])
        ]
        self._hashtags_aviation = [
            str(tag).lower() for tag in (hashtags_cfg.get("aviation") or [])
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
    def _distinct_hits(text: str, keywords: list[str]) -> list[str]:
        """کلیدواژه‌های متمایز؛ flight و flights یک مفهوم‌اند و دو بار شمرده نمی‌شوند."""
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
    def _hashtag_signal(self, item: RawItem) -> str:
        """هشتگ‌های تحت پایش را به متن قابل‌تطبیق تبدیل می‌کند.

        پست‌های شبکه‌های اجتماعی کوتاه‌اند؛ «#TehranFIR» تنها با این تبدیل به
        کلیدواژه‌ی «tehran fir» و «MahanAir» به نام ایرلاین تبدیل می‌شود.
        کالکتورهای تلگرام/X/اینستاگرام فقط هشتگ‌های داخل watchlist را اینجا
        می‌گذارند، پس سیگنال تزریقی همیشه از فهرست تنظیمات آمده است.
        """
        watched = (item.extra or {}).get("watched_hashtags") or []
        parts = []
        for tag in watched:
            text = str(tag).lstrip("#").replace("_", " ")
            if text.strip():
                parts.append(_HASHTAG_SPLIT_RE.sub(" ", text))
        return " ".join(parts)

    def classify(self, item: RawItem) -> Classification:
        # سقف طول متن: صفحه‌های بلند به‌طور تصادفی کلیدواژه‌های ایران و هوانوردی را کنار هم دارند
        raw_text = (item.text or "")[: self._max_match_chars]
        signal = self._hashtag_signal(item)
        if signal:
            raw_text = f"{raw_text} {signal}"
        # سرصفحه‌ی «تهران-ایرنا» نباید به‌عنوان نشانه‌ی ایران شمرده شود.
        content_raw = _strip_dateline(raw_text)
        text = util.normalize(content_raw)
        airports = self._airports_in(raw_text)
        # مهم: متن اصلی (قبل از lowercase شدن) به AirlineIndex داده می‌شود تا کدهای ۳حرفی
        # حروف بزرگ مثل THY، PIA، KLM، IRA و IRM درست تشخیص داده شوند.
        airlines = self.airlines.match(raw_text)

        if not self._is_relevant(text, airports, airlines) and not self._hashtag_pair(text):
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

        publishable = self._is_publishable(
            item=item,
            category=category,
            severity=severity,
            airports=airports,
            airlines=airlines,
            hashtag_pair=self._hashtag_pair(text),
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
            confidence_score=confidence,
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

        ارسال فوری فقط وقتی است که تیتر یا موجودیت‌های خبر خودشان موضوع
        اصلی باشند؛ اشاره‌ی اتفاقی در بدنه ثبت می‌شود ولی پوش نمی‌شود.
        """
        title_raw = item.title or ""
        title_text = util.normalize(title_raw)
        body_text = util.normalize(item.body or "")

        airports_title = self._airports_in(title_raw)
        airlines_title = self.airlines.match(title_raw)
        iran_title = self._hit(title_text, self._relevance)
        # واژه‌های نظامیِ صرف (air force / airbase) به‌تنهایی تیتر را «موضوع
        # اصلی هوانوردی غیرنظامی/حریم هوایی» نمی‌کنند؛ همین حالا در فهرست
        # غیرعملیاتی‌اند و نباید از مسیر strong عبور کنند.
        _nonop_norm = {util.normalize(kw) for kw in self._non_operational}
        strong_title = [
            hit
            for hit in self._distinct_hits(title_text, self._strong)
            if hit not in _nonop_norm
        ]
        weak_title = self._distinct_hits(title_text, self._aviation)
        strong_body = self._distinct_hits(body_text, self._strong)
        signal_body = self._distinct_hits(body_text, self._airspace_signal)

        publishable = False
        if airports or any(self.airlines.is_iranian(name) for name in airlines):
            publishable = True
        elif hashtag_pair:
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

        # وتوی سختِ تیترهای کیفری/دادگاهی/جاسوسی: خبر «متهم‌شدن به کمک
        # اطلاعاتی به ایران در یک پایگاه نظامی خارجی» خبر حریم هوایی نیست،
        # حتی اگر بدنه پر از aircraft/flights باشد. ثبت می‌شود ولی پوش
        # نمی‌شود؛ مگر اینکه خود تیتر سیگنال عملیاتی واقعی داشته باشد.
        if publishable and self._hit(title_text, self._off_topic_title):
            operational_title = (
                bool(self._distinct_hits(title_text, self._airspace_signal))
                or bool(airports_title)
                or any(self.airlines.is_iranian(name) for name in airlines_title)
            )
            if not operational_title:
                publishable = False
        return publishable

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
