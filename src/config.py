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
        channels = [
            dict(c) for c in self.sources.get("telegram_channels", [])
            if c.get("enabled", False) and c.get("username")
        ]
        known = {str(c["username"]).strip().lstrip("@").casefold() for c in channels}
        # فهرست قابل‌تغییر بدون ویرایش کد؛ فقط نام کاربری کانال‌های عمومی.
        for raw in (os.getenv("TELEGRAM_CHANNELS") or "").split(","):
            username = raw.strip().lstrip("@").strip()
            key = username.casefold()
            if not username or key in known:
                continue
            channels.append({
                "id": f"tg_{username.lower()}",
                "name": f"Telegram @{username}",
                "username": username,
                "lang": "auto",
                "country": "INTL",
                "tier": 1,
                "enabled": True,
            })
            known.add(key)
        return channels

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
            return [item.strip() for item in raw.split(",") if item.strip()]
        configured = self.get("llm.fallback_models", []) or []
        if isinstance(configured, str):
            configured = configured.split(",")
        return [str(item).strip() for item in configured if str(item).strip()]

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
