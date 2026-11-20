"""Клиент LLM поверх OpenAI-совместимого API (aitunnel).

Никаких офлайн-заглушек: если модель недоступна или вернула мусор, выбрасывается
LLMError, и это видно в трассе и в UI.
"""

from __future__ import annotations

import json
import time
from typing import Any, TypeVar

from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from .config import Settings
from .contracts import AgentUsage

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.model
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout,
            max_retries=settings.max_retries,
        )

    def metered(self, usage: dict[str, AgentUsage]) -> "MeteredLLM":
        return MeteredLLM(self, usage)


class MeteredLLM:
    """Обёртка, которая пишет расход токенов каждого агента в трассу конкретного запуска."""

    def __init__(self, llm: LLM, usage: dict[str, AgentUsage]) -> None:
        self._llm = llm
        self.usage = usage
        self.model = llm.model

    def _record(self, agent: str, response: Any, started: float) -> None:
        u = self.usage.setdefault(agent, AgentUsage())
        u.calls += 1
        u.latency_ms += int((time.perf_counter() - started) * 1000)
        if response is not None and response.usage is not None:
            u.prompt_tokens += response.usage.prompt_tokens or 0
            u.completion_tokens += response.usage.completion_tokens or 0
            extra = response.usage.model_extra or {}
            if isinstance(extra.get("cost_rub"), (int, float)):
                u.cost_rub += float(extra["cost_rub"])

    async def structured(self, agent: str, system: str, user: str, schema: type[T]) -> T:
        """Structured output: модель обязана вернуть JSON по схеме pydantic-модели."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error = ""
        for _ in range(2):
            started = time.perf_counter()
            try:
                response = await self._llm._client.chat.completions.parse(
                    model=self.model, messages=messages, response_format=schema
                )
            except (OpenAIError, ValidationError, json.JSONDecodeError) as exc:
                self._record(agent, None, started)
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            self._record(agent, response, started)
            message = response.choices[0].message
            if message.parsed is not None:
                return message.parsed
            last_error = message.refusal or "модель не вернула JSON по схеме"
        raise LLMError(f"[{agent}] {last_error}")

    async def chat(
        self,
        agent: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> Any:
        """Обычный chat completion (с инструментами или без). Возвращает message."""
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        started = time.perf_counter()
        try:
            response = await self._llm._client.chat.completions.create(**kwargs)
        except OpenAIError as exc:
            self._record(agent, None, started)
            raise LLMError(f"[{agent}] {type(exc).__name__}: {exc}") from exc
        self._record(agent, response, started)
        return response.choices[0].message


def parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Модель вернула невалидный JSON аргументов: {raw[:200]}") from exc
    if not isinstance(value, dict):
        raise LLMError(f"Аргументы инструмента должны быть объектом, получено: {raw[:200]}")
    return value
