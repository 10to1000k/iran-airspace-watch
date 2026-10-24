"""تطبیق نام ایرلاین‌ها در چند زبان."""

from __future__ import annotations

import re

from . import util

_UPPER_CODE = re.compile(r"\b[A-Z]{3}\b")


class AirlineIndex:
    """نمایه‌ی جست‌وجوی نام ایرلاین‌ها با همه‌ی املای ممکن.

    بدون این، جست‌وجوی «Turkish Airlines» در رسانه‌ی ترکی هیچ نتیجه‌ای
    نمی‌دهد چون آنجا «THY» می‌نویسند.

    نکته‌ی مهم: کدهای کوتاه فقط وقتی معتبرند که در متن **باکس‌حروف بزرگ**
    آمده باشند. وگرنه «IS» (سپهران) با فعل «is» و «SAW» (شام وینگز) با
    «saw» قاطی می‌شود — و خبر سیاسی بی‌ربط به ایرلاین وصل می‌شود.
    """

    MIN_ALIAS = 4  # «هما»، «IS»، «SAW» و امثالشان دیگر ایندکس نمی‌شوند

    def __init__(self, airlines: list[dict]):
        self.airlines = airlines
        self._names: dict[str, str] = {}
        self._codes: dict[str, str] = {}

        for entry in airlines:
            canonical = entry["name"]
            candidates = [canonical, *(entry.get("aliases") or [])]
            for alias in candidates:
                if not alias:
                    continue
                normalized = util.normalize(alias)
                if len(normalized) < self.MIN_ALIAS:
                    continue
                self._names.setdefault(normalized, canonical)
            for code in (entry.get("icao"), entry.get("iata")):
                if code and len(code) == 3:
                    self._codes[code.upper()] = canonical

        # طولانی‌ترها اول، تا «Turkish Airlines» قبل از «Turkish» بررسی شود
        self._ordered = sorted(
            self._names.items(), key=lambda kv: len(kv[0]), reverse=True
        )
        # مرز واژه لازم است: «ماهان» نباید داخل «ماهانه» پیدا شود
        # (خبر «تولید ماهانه نان» به‌عنوان خبر ماهان ایر منتشر شد).
        self._patterns = {
            alias: re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)")
            for alias in self._names
        }

    def match(self, text: str) -> list[str]:
        if not text:
            return []
        norm = util.normalize(text)
        found: list[str] = []

        for alias, canonical in self._ordered:
            if canonical in found:
                continue
            if self._patterns[alias].search(norm):
                found.append(canonical)

        for code in set(_UPPER_CODE.findall(text)):
            canonical = self._codes.get(code)
            if canonical and canonical not in found:
                found.append(canonical)

        return found

    def is_iranian(self, name: str) -> bool:
        for entry in self.airlines:
            if entry["name"] == name:
                return entry.get("country") == "IR"
        return False

    def scope(self, name: str) -> str:
        for entry in self.airlines:
            if entry["name"] == name:
                return entry.get("scope", "operates")
        return "operates"

    def icao_map(self) -> dict[str, str]:
        return {
            entry["icao"].upper(): entry["name"]
            for entry in self.airlines
            if entry.get("icao")
        }

    def by_country(self, country: str) -> list[dict]:
        return [a for a in self.airlines if a.get("country") == country]
