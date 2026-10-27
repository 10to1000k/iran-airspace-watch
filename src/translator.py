import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from . import util
from .models import Translation

log = logging.getLogger(__name__)


# ── مدل‌های چندریختی و cooldown ───────────────────────────────────────


class ModelCooldown:
    """مدیر زمان انتظار برای مدل‌های تعمیری یا مسدود شده.

    کلیک Timeout توسط بذر server APP شروع می‌شود. هر مدل ۳ خطای HTTP
    (503/429/...) ثبت می‌شود؛ اگر ۳ خطاформи شد، مدل ۵ دقیقه
    (وای_DO 300 ثانیه) suspended می‌شود.
    """

    def __init__(self, timeout_seconds: int = 300):
        self.timeout = timeout_seconds
        self.start_times: dict[str, float] = {}
        self.errors: dict[str, int] = {}

    def is_active(self, model: str) -> bool:
        """مدل در حالت cooldown است؟"""
        if model not in self.start_times:
            return False
        elapsed = time.time() - self.start_times[model]
        if elapsed > self.timeout:
            del self.start_times[model]
            self.errors.pop(model, None)
            return False
        return True

    def record_error(self, model: str) -> None:
        """یک خطا را برای مدل ثبت کن؛ پس از ۳ خطا، مدل cooldown می‌رود."""
        count = self.errors.get(model, 0) + 1
        self.errors[model] = count
        if count >= 3:
            self.start_times[model] = time.time()
            log.warning("مدل %s پس از %d خطا در حالت cooldown قرار گرفت", model, count)
            self.errors.pop(model, None)


COOLDOWN = ModelCooldown()


# ── SYSTEM_PROMPT ──────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a professional aviation news translator into Persian (Farsi).

HARD RULES
1. Translate ONLY the provided text. Do not add facts, opinions, or information not present.
2. Keep every number exactly as in the source (۴۲, FL350, 250 passengers).
3. Keep ICAO/IATA codes, NOTAM numbers, flight numbers, airline names in Latin script.
4. Use natural Persian newsroom style. Do NOT summarize, omit, or compress any paragraph.
5. Output plain text only. No markdown, no hashtags, no commentary.

Translate EVERY paragraph completely. If the body has multiple paragraphs, translate all of them.
Return exactly this format:
---
TITLE: <Persian headline, max 10 words>
---
BODY: <Complete Persian translation of the entire body paragraph>
---
"""


# ── TRANSLATOR ───────────────────────────────────────────────────


class Translator:
    def __init__(self, config, db=None):
        self.cfg = config
        self.db = db
        self.api_key = config.llm_api_key
        self.base_url = config.llm_base_url
        self.model = config.llm_model
        self.fallbacks = config.llm_fallback_models
        self.active_model = self.model
        self.glossary = config.glossary
        # سقف متنی که به LLM داده می‌شود (هزینه‌ی توکن)؛ متن کامل‌تر از این
        # ترجمه نمی‌شود ولی بخش ابتدایی خبر — که منتشر می‌شود — پوشش داده می‌شود.
        self.max_translate_chars = int(config.get("app.max_translate_chars", 4000))
        self._memory_cache: dict[str, str] = {}
        self._warned = False

    def _model_chain(self) -> list[str]:
        chain: list[str] = []
        for name in [self.model, *self.fallbacks]:
            if name and name not in chain:
                chain.append(name)
        return chain

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

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
            f"- {en} → {fa}" for en, fa in mapping.items() if util.normalize(en) in norm
        ]
        for code in self.glossary.get("keep_latin", []):
            lines.append(f"- keep `{code}` in Latin")
        return "\n".join(lines) or "- (no specific terms)"

    def _chat(self, user_content: str, system: str) -> str:
        """برای `self.model` (و fallbackها) یک درخواستPOST ارسال می‌کند.

        اگر MODEL_TIMEOUT_IRRELEVANT بدهد → ۴۰۴ یا ۵۰۳، حالت COOLDOWN می‌رود.
        ۳ خطا متوالی → cooldown ۳۰۰ ثانیه.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        last_error: Exception | None = None
        for model in self._model_chain():
            # مدل‌های Cooldown نمی‌توانند امتحان شوند
            if COOLDOWN.is_active(model):
                log.info("مدل %s در حالت cooldown است — رد می‌کنیم", model)
                continue
            for attempt in range(2):
                try:
                    response = requests.post(
                        f"{self.base_url}/chat/completions",
                        json={"model": model, "temperature": 0.1, "messages": messages},
                        headers=headers,
                        timeout=60,
                    )
                    if response.status_code == 404:
                        last_error = requests.HTTPError(f"مدل {model} وجود ندارد (404)")
                        break
                    if response.status_code in (429, 500, 502, 503, 504):
                        COOLDOWN.record_error(model)
                        raise requests.HTTPError(
                            f"HTTP {response.status_code} از {model}"
                        )
                    response.raise_for_status()
                    data = response.json()
                    content = (data["choices"][0]["message"]["content"] or "").strip()
                    if model != self.active_model:
                        log.info("مدل ترجمه به %s تغییر کرد", model)
                        self.active_model = model
                    return content
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    COOLDOWN.record_error(model)
                    time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"ترجمة ناموفق بود: {last_error}")

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
            return Translation(
                text_fa=body or title, title_fa=title, engine="passthrough-fa"
            )

        if not self.enabled:
            if not self._warned:
                log.warning(
                    "LLM_API_KEY تنظیم نشده — ترجمه غیرفعال است، متن اصلی ارسال می‌شود"
                )
                self._warned = True
            raise RuntimeError("LLM_API_KEY تنظیم نشده — ترجمه انجام نمی‌شود")

        if kind == "notam":
            source_text = (
                f"NOTAM number: {title}\n"
                f"Raw NOTAM:\n{raw}\n\n"
                f"Pre-decoded facts (already in Persian, do not contradict): {decoded_fa}"
            )
            cache_key = util.fingerprint(f"notam::{title}::{raw}")
        else:
            if len(body) > self.max_translate_chars:
                body = util.truncate(body, self.max_translate_chars)
            source_text = f"HEADLINE:\n{title}\n\nBODY:\n{body}"
            cache_key = util.fingerprint(f"{lang}::{title}::{body}")

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
            # ⚠️ translte_item NEVER returns raw English.
            # Pipeline will catch RuntimeError → delete item and retry.
            raise RuntimeError(f"ترجمة ناموفق بود: {exc}") from exc

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
        return Translation(
            text_fa=body_fa, title_fa=head, engine=self.active_model, numbers_ok=ok
        )

    def answer_chat(self, system_prompt: str, user_text: str) -> str:
        """پاسخ به سوال کاربر درباره‌ی یک خبر خاص با LLM."""
        if not self.enabled:
            return "امکان چت با LLM فعال نیست (کلید LLM_API_KEY تنظیم نشده است)."

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        last_error: Exception | None = None
        for model in self._model_chain():
            if COOLDOWN.is_active(model):
                log.info("مدل %s در حالت cooldown است — رد می‌کنیم", model)
                continue
            for attempt in range(2):
                try:
                    response = requests.post(
                        f"{self.base_url}/chat/completions",
                        json={"model": model, "temperature": 0.3, "messages": messages},
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        timeout=60,
                    )
                    if response.status_code == 404:
                        last_error = requests.HTTPError(f"مدل {model} وجود ندارد (404)")
                        break
                    if response.status_code in (429, 500, 502, 503, 504):
                        COOLDOWN.record_error(model)
                        raise requests.HTTPError(
                            f"HTTP {response.status_code} از {model}"
                        )
                    response.raise_for_status()
                    data = response.json()
                    content = (data["choices"][0]["message"]["content"] or "").strip()
                    if model != self.active_model:
                        log.info("مدل چت به %s تغییر کرد", model)
                        self.active_model = model
                    return content
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    COOLDOWN.record_error(model)
                    time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"چت LLM ناموفق بود: {last_error}")


def _hash(text: str) -> str:
    return util.fingerprint(text)[:24]
