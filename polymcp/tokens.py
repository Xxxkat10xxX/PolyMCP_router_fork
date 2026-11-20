"""Оценка размера в токенах для артефактов, которые мы сами отправляем в модель.

Фактический расход берётся из usage ответа API, а tiktoken нужен для сравнения
частей промпта (например, схем до и после сжатия) до отправки.
"""

from __future__ import annotations

import json
from typing import Any

import tiktoken

_ENC = tiktoken.get_encoding("o200k_base")


def count_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(_ENC.encode(text))


def clip_tokens(text: str, max_tokens: int) -> str:
    ids = _ENC.encode(text)
    if len(ids) <= max_tokens:
        return text
    return _ENC.decode(ids[:max_tokens]) + f"\n... [обрезано: {len(ids) - max_tokens} токенов]"
