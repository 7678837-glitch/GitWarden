"""Small, request-scoped translation catalog used by the server-rendered UI."""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

LANG_COOKIE = "gw_lang"
CSRF_COOKIE = "gw_csrf"
DEFAULT_LANG = "en"
SUPPORTED_LANGS = ("en", "zh_CN")
_LANGUAGE = ContextVar("gitwarden_language", default=DEFAULT_LANG)
_CATALOG_DIR = Path(__file__).with_name("i18n")


def normalize_lang(value: str | None) -> str:
    return value if value in SUPPORTED_LANGS else DEFAULT_LANG


@lru_cache(maxsize=len(SUPPORTED_LANGS))
def catalog(lang: str) -> dict[str, str]:
    path = _CATALOG_DIR / f"{normalize_lang(lang)}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def translate(key: str, lang: str = DEFAULT_LANG, **values: object) -> str:
    english = catalog(DEFAULT_LANG)
    text = catalog(normalize_lang(lang)).get(key, english.get(key, key))
    if values:
        text = text.format(**values)
    return text


def t(key: str, **values: object) -> str:
    """Jinja global; the current request language is kept in a ContextVar."""
    return translate(key, _LANGUAGE.get(), **values)


def set_language(lang: str):
    """Set the current language and return a token suitable for reset()."""
    return _LANGUAGE.set(normalize_lang(lang))


def reset_language(token) -> None:
    _LANGUAGE.reset(token)


def language_for_cookie(value: str | None) -> str:
    return normalize_lang(value)


_MARK = re.compile(r"\x02([\w.]+)(?:\x1f(.*?))?\x03", re.DOTALL)


def msg(key: str, **params: object) -> str:
    """生成写入数据库的稳定消息：页面渲染时再按当前语言翻译，旧的中文原文不受影响。"""
    body = json.dumps(params, default=str, ensure_ascii=False) if params else ""
    return f"\x02{key}" + (f"\x1f{body}" if body else "") + "\x03"


def redact(text: str, secrets: list[str]) -> str:
    """对消息参数先解码再脱敏，避免泄漏转义后的密钥或损坏 JSON 结构。"""
    values = sorted({value for value in secrets if value}, key=len, reverse=True)

    def clean(value):
        if isinstance(value, str):
            for secret in values:
                value = value.replace(secret, "***")
        elif isinstance(value, list):
            value = [clean(item) for item in value]
        elif isinstance(value, dict):
            value = {key: clean(item) for key, item in value.items()}
        return value

    parts = []
    end = 0
    for match in _MARK.finditer(text):
        parts.append(clean(text[end : match.start()]))
        params = json.loads(match.group(2)) if match.group(2) else {}
        parts.append(msg(match.group(1), **clean(params)))
        end = match.end()
    parts.append(clean(text[end:]))
    return "".join(parts)


def render(text: str | None, lang: str | None = None) -> str:
    if not text or "\x02" not in text:
        return text or ""
    language = lang or _LANGUAGE.get()

    def _one(match: re.Match[str]) -> str:
        params = json.loads(match.group(2)) if match.group(2) else {}
        return translate(match.group(1), language, **params)

    return _MARK.sub(_one, text)
