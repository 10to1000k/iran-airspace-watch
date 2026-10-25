"""ترجمه به فارسی با گلوساری هوانوردی و اعتبارسنجی اعداد."""

from __future__ import annotations

import logging
import time

import requests

from . import util
from .config import LlmProvider
from .models import Translation

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a professional aviation news translator into Persian (Farsi).

HARD RULES — violating any of them makes the output unusable:
1. Translate ONLY. Never add facts, numbers, dates, names, or opinions that are not in the source.
2. Keep these in Latin script, never transliterate: ICAO/IATA airport codes (OIIE, OIII, OIIX, IKA, THR),
   NOTAM numbers, flight numbers, airline brand names, "NOTAM", "FIR", "UTC", "FL".
3. Persian must be natural newsroom Persian, not literal word-by-word translation. Use "می‌" with ZWNJ.
4. Keep every number exactly as in the source.
5. Do not add a headline if none is given. Do not add hashtags.
6. Output plain text only. No markdown, no commentary, no explanation.

Aviation glossary (use these exact Persian equivalents when the term appears):
{terms}
"""


class Translator:
    def __init__(self, config, db=None):
        self.cfg = config
        self.db = db
        self.api_key = config.llm_api_key
        self.base_url = config.llm_base_url
        self.model = config.llm_model
        self.fallbacks = config.llm_fallback_models
        # زنجیره‌ی چندسرویسی (Gemini ← Groq ← OpenRouter ← LLM_* قدیمی).
        # سرویس‌های اختصاصی همان‌هایی‌اند که نامشان «custom» نیست.
        self.providers: list[LlmProvider] = list(
            getattr(config, "llm_providers", []) or []
        )
        self._dedicated_providers = [
            p for p in self.providers if p.name != "custom" and p.api_key
        ]
        first = self._provider_chain()[0] if self._provider_chain() else None
        self.active_provider = first.name if first else "custom"
        self.active_model = first.models[0] if first and first.models else self.model
        self.glossary = config.glossary
        self._memory_cache: dict[str, str] = {}
        self._warned = False

    def _model_chain(self) -> list[str]:
        chain: list[str] = []
        for name in [self.model, *self.fallbacks]:
            if name and name not in chain:
                chain.append(name)
        return chain

    def _provider_chain(self) -> list[LlmProvider]:
        """سرویس‌ها به‌ترتیب اولویت.

        اگر کلید اختصاصیِ سرویس‌های رایگان ست شده باشد، همان زنجیره
        (به‌همراه LLM_* در انتها، اگر باشد) استفاده می‌شود. وگرنه از
        فیلدهای تکیِ فعلی ساخته می‌شود تا رفتار قدیمی — از جمله ست‌کردن
        مستقیم api_key/model/fallbacks روی نمونه — دست نخورد.
        """
        if self._dedicated_providers:
            return list(self.providers)
        return [
            LlmProvider(
                name="custom",
                base_url=self.base_url,
                api_key=self.api_key,
                models=tuple(self._model_chain()),
            )
        ]

    @property
    def enabled(self) -> bool:
        return any(p.api_key for p in self._provider_chain())

    # ── کش ───────────────────────────────────────────────────
    def _cache_get(self, key: str) -> str | None:
        if key in self._memory_cache:
            return self._memory_cache[key]
        if self.db:
            return self.db.kv_get(f"tr:{key}")
        return None

    def _cache_set(self, key: str, value: str) -> None:
        if len(self._memory_cache) < 500:
            self._memory_cache[key] = value
        if self.db:
            self.db.kv_set(f"tr:{key}", value)

    # ── فراخوانی LLM ─────────────────────────────────────────
    def _term_block(self, text: str) -> str:
        norm = util.normalize(text)
        mapping = self.glossary.get("translation_terms", {}).get("fa", {})
        lines = [
            f"- {en} → {fa}"
            for en, fa in mapping.items()
            if util.normalize(en) in norm
        ]
        for code in self.glossary.get("keep_latin", []):
            lines.append(f"- keep `{code}` in Latin")
        return "\n".join(lines) or "- (no specific terms)"

    def _chat(self, user_content: str, system: str) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        last_error: Exception | None = None
        # سرویس‌ها و مدل‌هایشان به ترتیب امتحان می‌شوند: Gemini، بعد Groq،
        # بعد OpenRouter (و در انتها LLM_* قدیمی). سهمیه‌ی روزانه‌ی رایگان
        # (429)، شلوغی (503) یا حذف مدل (404) ترجمه را متوقف نمی‌کند.
        # خطای احراز هویت (401/403) کل آن سرویس را کنار می‌گذارد، چون
        # تکرارش فایده‌ای ندارد.
        for provider in self._provider_chain():
            if not provider.api_key:
                continue
            headers = {
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
            }
            if provider.name == "openrouter":
                # اختیاری اما توصیه‌شده‌ی خود OpenRouter برای شناسایی مصرف‌کننده
                headers["X-Title"] = "Iran Airspace Watch"
            provider_dead = False
            for model in provider.models:
                for attempt in range(2):
                    try:
                        response = requests.post(
                            f"{provider.base_url}/chat/completions",
                            json={
                                "model": model,
                                "temperature": 0.1,
                                "messages": messages,
                            },
                            headers=headers,
                            timeout=60,
                        )
                        if response.status_code == 404:
                            last_error = requests.HTTPError(
                                f"مدل {model} در {provider.name} موجود نیست (404)"
                            )
                            break
                        if response.status_code in (401, 403):
                            last_error = requests.HTTPError(
                                f"کلید {provider.name} رد شد (HTTP {response.status_code})"
                            )
                            provider_dead = True
                            break
                        if response.status_code in (429, 500, 502, 503, 504):
                            raise requests.HTTPError(
                                f"HTTP {response.status_code} از {provider.name}/{model}"
                            )
                        response.raise_for_status()
                        data = response.json()
                        content = (
                            data["choices"][0]["message"]["content"] or ""
                        ).strip()
                        if not content:
                            raise ValueError(
                                f"پاسخ خالی از {provider.name}/{model}"
                            )
                        if (provider.name, model) != (
                            self.active_provider,
                            self.active_model,
                        ):
                            log.info(
                                "سرویس ترجمه به %s/%s تغییر کرد",
                                provider.name,
                                model,
                            )
                            self.active_provider = provider.name
                            self.active_model = model
                        return content
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
                        if attempt == 0:
                            time.sleep(1.0)
                if provider_dead:
                    log.warning(
                        "سرویس %s به‌علت خطای کلید کنار گذاشته شد؛ سرویس بعدی امتحان می‌شود",
                        provider.name,
                    )
                    break
        raise RuntimeError(f"ترجمه ناموفق بود: {last_error}")

    # ── API عمومی ────────────────────────────────────────────
    def translate_item(
        self,
        title: str,
        body: str,
        lang: str,
        kind: str = "news",
        decoded_fa: str = "",
        raw: str = "",
    ) -> Translation:
        if lang == "fa":
            return Translation(text_fa=body or title, title_fa=title, engine="passthrough-fa")

        if not self.enabled:
            if not self._warned:
                log.warning("هیچ کلید LLM تنظیم نشده (GEMINI_API_KEY/GROQ_API_KEY/OPENROUTER_API_KEY/LLM_API_KEY) — ترجمه غیرفعال است، متن اصلی ارسال می‌شود")
                self._warned = True
            return Translation(
                text_fa=f"[ترجمه نشده — کلید LLM تنظیم نیست]\n{title}\n{body}".strip(),
                title_fa=title,
                engine="disabled",
                numbers_ok=True,
            )

        if kind == "notam":
            source_text = (
                f"NOTAM number: {title}\n"
                f"Raw NOTAM:\n{raw}\n\n"
                f"Pre-decoded facts (already in Persian, do not contradict): {decoded_fa}"
            )
            cache_key = _hash(f"notam::{title}::{raw}")
        else:
            source_text = f"HEADLINE:\n{title}\n\nBODY:\n{body}"
            cache_key = _hash(f"{lang}::{title}::{body}")

        cached = self._cache_get(cache_key)
        if cached:
            parts = cached.split("\n", 1)
            return Translation(
                text_fa=parts[1] if len(parts) > 1 else cached,
                title_fa=parts[0],
                engine="cache",
                numbers_ok=util.numbers_preserved(f"{title} {body}", cached),
            )

        system = SYSTEM_PROMPT.format(terms=self._term_block(f"{title} {body} {raw}"))
        instruction = (
            "Translate the following into Persian. Return exactly two parts separated by a line "
            "containing only ---: first the headline in Persian, then the body in Persian. "
            "If the source has no headline, produce a short factual Persian headline "
            "(max 12 words) that contains no new information.\n\n" + source_text
        )

        try:
            output = self._chat(instruction, system)
        except Exception as exc:  # noqa: BLE001
            log.error("%s", exc)
            return Translation(
                text_fa=f"[خطای ترجمه — متن اصلی]\n{title}\n{body}".strip(),
                title_fa=title,
                engine="failed",
                numbers_ok=False,
            )

        if "---" in output:
            head, body_fa = output.split("---", 1)
        else:
            lines = [line for line in output.splitlines() if line.strip()]
            head = lines[0] if lines else title
            body_fa = "\n".join(lines[1:]) or output
        head = head.strip().strip("#").strip()
        body_fa = body_fa.strip()

        self._cache_set(cache_key, f"{head}\n{body_fa}")
        ok = util.numbers_preserved(f"{title} {body} {raw}", f"{head} {body_fa}")
        if not ok:
            log.warning("اعداد در ترجمه حفظ نشدند: %s", title[:80])
        engine = (
            self.active_model
            if self.active_provider == "custom"
            else f"{self.active_provider}/{self.active_model}"
        )
        return Translation(text_fa=body_fa, title_fa=head, engine=engine, numbers_ok=ok)


def _hash(text: str) -> str:
    return util.fingerprint(text)[:24]
