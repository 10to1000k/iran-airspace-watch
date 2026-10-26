"""مدل‌های داده‌ی مشترک."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawItem:
    """خروجی خام هر کالکتور — همه‌ی کالکتورها این شکل را برمی‌گردانند."""

    source_id: str
    source_name: str
    source_url: str
    title: str
    body: str
    url: str
    lang: str = "en"
    country: str = "INTL"
    kind: str = "rss"          # rss | notam | telegram | x
    published_at: datetime | None = None
    extra: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}".strip()

    @property
    def tier(self) -> int:
        try:
            return int(self.extra.get("tier", 2))
        except (TypeError, ValueError):
            return 2


@dataclass
class Classification:
    relevant: bool
    category: str = "context"
    severity: str = "context"
    countries: list[str] = field(default_factory=list)
    airlines: list[str] = field(default_factory=list)
    airports: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    # موضوع اصلی است یا فقط مرتبطِ حاشیه‌ای؟ فقط موضوع اصلی برای کاربر ارسال
    # می‌شود؛ بقیه در دیتابیس ثبت می‌شوند (برای /last و خوشه‌بندی) ولی پوش نمی‌شوند.
    publishable: bool = False

    @property
    def entity_key(self) -> str:
        """کلید موجودیت برای خوشه‌بندی: ایرلاین + فرودگاه."""
        airlines = "|".join(sorted(a.lower() for a in self.airlines))
        return f"{airlines}#{','.join(sorted(self.airports))}"


@dataclass
class Translation:
    text_fa: str
    engine: str
    numbers_ok: bool = True
    title_fa: str = ""
