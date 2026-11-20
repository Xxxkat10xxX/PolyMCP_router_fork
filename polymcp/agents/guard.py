"""Policy Guard: детерминированная проверка вызова до отправки на MCP-сервер.

Это код, а не LLM, поэтому обойти guard промпт-инъекцией нельзя. Вторая линия
защиты — сами серверы: read-only транзакции в Postgres, белый список в shell,
--read-only у GitHub.
"""

from __future__ import annotations

import re
from typing import Any

from ..contracts import GuardVerdict, ToolCall, ToolSpec
from ..hub import Policy

_SQL_STRING = re.compile(r"'(?:[^']|'')*'")
_SQL_WRITE = re.compile(
    r"\b(insert|update|delete|merge|drop|truncate|alter|create|grant|revoke|copy|vacuum|reindex|cluster|"
    r"refresh|set\s+role|pg_terminate_backend|pg_cancel_backend)\b",
    re.IGNORECASE,
)
_SHELL_DANGER = re.compile(
    r"(\brm\b|\bmkfs|\bdd\b|\bshutdown\b|\breboot\b|\bkill(all)?\b|\bchmod\b|\bchown\b|\bmv\b|\bcurl\b|\bwget\b|"
    r"[>|;&`]|\$\()",
    re.IGNORECASE,
)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def check(call: ToolCall, tool: ToolSpec | None, policy: Policy) -> GuardVerdict:
    if tool is None:
        return GuardVerdict(allowed=False, reason=f"Инструмент {call.server}.{call.tool} не был предложен для шага")
    if call.tool in policy.deny_tools or tool.qualified_name in policy.deny_tools:
        return GuardVerdict(allowed=False, reason=f"Инструмент {call.tool} запрещён политикой")
    if not policy.allow_write_tools and (tool.destructive or tool.read_only is not True):
        return GuardVerdict(
            allowed=False,
            reason=f"Инструмент {call.tool} не помечен сервером как read-only, а запись запрещена политикой",
        )

    texts = _strings(call.arguments)
    if call.server == "postgres":
        for text in texts:
            match = _SQL_WRITE.search(_SQL_STRING.sub("''", text))
            if match:
                return GuardVerdict(
                    allowed=False,
                    reason=f"SQL содержит изменяющую операцию {match.group(0).upper()!r}, разрешено только чтение",
                )
    if call.server == "shell" and call.tool == "run_command":
        # У остальных shell-инструментов структурированные параметры (regex в grep может содержать "|").
        for text in texts:
            match = _SHELL_DANGER.search(text)
            if match:
                return GuardVerdict(allowed=False, reason=f"Опасная shell-конструкция {match.group(0)!r}")
    return GuardVerdict(allowed=True)
