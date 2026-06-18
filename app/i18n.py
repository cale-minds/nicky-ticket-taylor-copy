"""
Lightweight i18n for the server-rendered admin UI.

Uses Python contextvars so each async request gets its own locale without
passing a Translator object through every function call.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

SUPPORTED_LOCALES: list[str] = [
    "en", "pt-br", "es", "fr", "ar", "da", "he", "it", "nl", "zh-Hans", "ru", "el",
]
RTL_LOCALES: frozenset[str] = frozenset({"ar", "he"})
DEFAULT_LOCALE = "en"

LOCALE_NAMES: dict[str, str] = {
    "en":      "English",
    "pt-br":   "Português",
    "es":      "Español",
    "fr":      "Français",
    "ar":      "العربية",
    "da":      "Dansk",
    "he":      "עברית",
    "it":      "Italiano",
    "nl":      "Nederlands",
    "zh-Hans": "中文",
    "ru":      "Русский",
    "el":      "Ελληνικά",
}

LOCALE_FLAGS: dict[str, str] = {
    "en":      "🇬🇧",
    "pt-br":   "🇧🇷",
    "es":      "🇪🇸",
    "fr":      "🇫🇷",
    "ar":      "🇸🇦",
    "da":      "🇩🇰",
    "he":      "🇮🇱",
    "it":      "🇮🇹",
    "nl":      "🇳🇱",
    "zh-Hans": "🇨🇳",
    "ru":      "🇷🇺",
    "el":      "🇬🇷",
}

_locale_var: ContextVar[str] = ContextVar("locale", default=DEFAULT_LOCALE)


@lru_cache(maxsize=None)
def _load(locale: str) -> dict[str, dict[str, str]]:
    path = Path(__file__).parent / "translations" / f"{locale}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def get_locale_from_request(request: object) -> str:
    lang = getattr(request, "cookies", {}).get("lang", "")
    return lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE


def set_request_locale(request: object) -> str:
    locale = get_locale_from_request(request)
    _locale_var.set(locale)
    return locale


def current_locale() -> str:
    return _locale_var.get()


def is_rtl() -> bool:
    return _locale_var.get() in RTL_LOCALES


def t(key: str, **kwargs: str) -> str:
    """
    Translate a namespaced key like 'NAV.DASHBOARD'.
    Falls back to English, then to the key itself.
    Extra kwargs replace {{placeholder}} in the value.
    """
    locale = _locale_var.get()
    data = _load(locale)
    en = _load(DEFAULT_LOCALE) if locale != DEFAULT_LOCALE else data

    parts = key.split(".", 1)
    if len(parts) == 2:
        ns, subkey = parts
        value: str = (
            (data.get(ns) or {}).get(subkey)
            or (en.get(ns) or {}).get(subkey)
            or key
        )
    else:
        value = data.get(key) or en.get(key) or key  # type: ignore[assignment]

    for k, v in kwargs.items():
        value = value.replace(f"{{{{{k}}}}}", str(v))
    return value
