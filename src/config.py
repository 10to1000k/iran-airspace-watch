"""بارگذاری تنظیمات و محیط."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


def _read_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"فایل تنظیمات پیدا نشد: {path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


@dataclass(frozen=True)
class LlmProvider:
    """یک سرویس سازگار با OpenAI به‌همراه مدل‌هایش، به‌ترتیب اولویت."""

    name: str
    base_url: str
    api_key: str | None
    models: tuple[str, ...]


@dataclass
class Config:
    settings: dict
    sources: dict
    airlines: list[dict]
    glossary: dict
    dry_run: bool = False
    _cache: dict = field(default_factory=dict)

    def env_get(self, name: str, default: str | None = None) -> str | None:
        return os.getenv(name) or default

    # ── دسترسی راحت به تنظیمات ────────────────────────────────
    def get(self, dotted: str, default=None):
        node = self.settings
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def rss_sources(self) -> list[dict]:
        return [
            s for s in self.sources.get("sources", [])
            if s.get("type") == "rss" and s.get("enabled", True)
        ]

    @property
    def telegram_channels(self) -> list[dict]:
        return [
            c for c in self.sources.get("telegram_channels", [])
            if c.get("enabled", False) and c.get("username")
        ]

    # ── محیط ─────────────────────────────────────────────────
    @property
    def telegram_token(self) -> str | None:
        return os.getenv("TELEGRAM_BOT_TOKEN") or None

    @property
    def telegram_chat_id(self) -> str | None:
        return os.getenv("TELEGRAM_CHAT_ID") or None

    @property
    def llm_api_key(self) -> str | None:
        return os.getenv("LLM_API_KEY") or None

    @property
    def llm_base_url(self) -> str:
        return (os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")

    @property
    def llm_model(self) -> str:
        return os.getenv("LLM_MODEL") or "gpt-4o-mini"

    @property
    def llm_fallback_models(self) -> list[str]:
        """مدل‌های جانشین؛ با کاما جدا می‌شوند. اگر مدل اصلی شلوغ/حذف‌شده بود امتحان می‌شوند."""
        raw = os.getenv("LLM_FALLBACK_MODELS")
        if raw:
            return _csv(raw)
        configured = self.get("llm.fallback_models", []) or []
        if isinstance(configured, str):
            configured = configured.split(",")
        return [str(item).strip() for item in configured if str(item).strip()]

    # ── زنجیره‌ی سرویس‌های رایگان ─────────────────────────────
    # ترتیب ثابت است: Gemini ← Groq ← OpenRouter. هر سرویسی که کلیدش
    # در محیط نباشد، بی‌سروصدا از زنجیره حذف می‌شود.
    _FREE_PROVIDERS = (
        # (نام، متغیرهای کلید، متغیر آدرس، آدرس پیش‌فرض، پیشوند متغیر مدل، مدل‌های پیش‌فرض)
        (
            "gemini",
            ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            "GEMINI_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "GEMINI",
            ("gemini-2.5-flash", "gemini-2.5-flash-lite"),
        ),
        (
            "groq",
            ("GROQ_API_KEY",),
            "GROQ_BASE_URL",
            "https://api.groq.com/openai/v1",
            "GROQ",
            ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
        ),
        (
            "openrouter",
            ("OPENROUTER_API_KEY",),
            "OPENROUTER_BASE_URL",
            "https://openrouter.ai/api/v1",
            "OPENROUTER",
            (
                "meta-llama/llama-3.3-70b-instruct:free",
                "deepseek/deepseek-chat-v3-0324:free",
            ),
        ),
    )

    @property
    def llm_providers(self) -> list[LlmProvider]:
        """همه‌ی سرویس‌های ترجمه به‌ترتیب اولویت.

        اول سرویس‌های رایگانِ دارای کلید (Gemini، Groq، OpenRouter)،
        بعد سرویس تکیِ قدیمی (LLM_*) اگر تنظیم شده باشد. اگر هیچ‌کدام
        نباشند، یک سرویسِ بدون کلید برمی‌گردد تا `enabled` غلط بماند.
        """
        providers: list[LlmProvider] = []
        for name, key_vars, url_var, default_url, prefix, defaults in self._FREE_PROVIDERS:
            api_key = _first_env(*key_vars)
            if not api_key:
                continue
            models = _csv(os.getenv(f"{prefix}_MODELS"))
            if not models:
                primary = os.getenv(f"{prefix}_MODEL")
                models = ([primary] if primary else []) + [
                    model for model in defaults if model != primary
                ]
            providers.append(
                LlmProvider(
                    name=name,
                    base_url=(os.getenv(url_var) or default_url).rstrip("/"),
                    api_key=api_key,
                    models=tuple(models),
                )
            )

        if self.llm_api_key:
            legacy = LlmProvider(
                name="custom",
                base_url=self.llm_base_url,
                api_key=self.llm_api_key,
                models=tuple(dict.fromkeys([self.llm_model, *self.llm_fallback_models])),
            )
            if not any(
                p.base_url == legacy.base_url and p.api_key == legacy.api_key
                for p in providers
            ):
                providers.append(legacy)

        if not providers:
            providers.append(
                LlmProvider(
                    name="custom",
                    base_url=self.llm_base_url,
                    api_key=None,
                    models=tuple(
                        dict.fromkeys([self.llm_model, *self.llm_fallback_models])
                    ),
                )
            )
        return providers

    @property
    def db_path(self) -> Path:
        return DATA_DIR / "watch.db"

    @property
    def health_port(self) -> int:
        """HEALTH_PORT (متغیر محیطی) بر تنظیمات YAML اولویت دارد."""
        raw = os.getenv("HEALTH_PORT") or self.get("health.port", 8080)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 8080


def load_config(dry_run: bool = False) -> Config:
    load_dotenv(ROOT / ".env")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings = _read_yaml("settings.yaml")
    sources = _read_yaml("sources.yaml")
    airlines = _read_yaml("airlines.yaml").get("airlines", [])
    glossary = _read_yaml("glossary.yaml")
    return Config(
        settings=settings,
        sources=sources,
        airlines=airlines,
        glossary=glossary,
        dry_run=dry_run or _flag("DRY_RUN"),
    )
