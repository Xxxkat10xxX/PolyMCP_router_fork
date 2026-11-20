"""Клиент LLM: aitunnel (любой OpenAI-совместимый API) или GigaChat.

Агенты работают с одним интерфейсом — structured() и chat() — и получают сообщения в формате
OpenAI (content, tool_calls[].id/.function.name/.function.arguments). Различия провайдеров
спрятаны в бэкендах.

Никаких офлайн-заглушек: если модель недоступна или вернула мусор, выбрасывается
LLMError, и это видно в трассе и в UI.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, TypeVar

from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from .config import Settings
from .contracts import AgentUsage

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


@dataclass
class Completion:
    message: Any  # объект с .content и .tool_calls в формате OpenAI
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_rub: float = 0.0


class OpenAIBackend:
    """aitunnel и любой другой OpenAI-совместимый API."""

    def __init__(self, settings: Settings) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout,
            max_retries=settings.max_retries,
        )

    @staticmethod
    def _usage(response: Any) -> dict[str, Any]:
        if response.usage is None:
            return {}
        extra = response.usage.model_extra or {}
        cost = extra.get("cost_rub")
        return {
            "prompt_tokens": response.usage.prompt_tokens or 0,
            "completion_tokens": response.usage.completion_tokens or 0,
            "cost_rub": float(cost) if isinstance(cost, (int, float)) else 0.0,
        }

    async def chat(self, model: str, messages: list[dict], tools: list[dict] | None,
                   tool_choice: str | None, extra: dict[str, Any]) -> Completion:
        kwargs: dict[str, Any] = {"model": model, "messages": messages, **extra}
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except OpenAIError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        return Completion(response.choices[0].message, **self._usage(response))

    async def structured(self, model: str, system: str, user: str, schema: type[T],
                         extra: dict[str, Any]) -> tuple[T | None, str, Completion | None]:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            response = await self._client.chat.completions.parse(
                model=model, messages=messages, response_format=schema, **extra
            )
        except (OpenAIError, ValidationError, json.JSONDecodeError) as exc:
            return None, f"{type(exc).__name__}: {exc}", None
        message = response.choices[0].message
        completion = Completion(message, **self._usage(response))
        if message.parsed is not None:
            return message.parsed, "", completion
        return None, message.refusal or "модель не вернула JSON по схеме", completion


class LLM:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.model
        if settings.provider == "gigachat":
            from .gigachat import GigaChatBackend

            self.backend: Any = GigaChatBackend(settings)
        else:
            self.backend = OpenAIBackend(settings)
        limit = settings.max_concurrency
        self._semaphore = asyncio.Semaphore(limit) if limit > 0 else None

    def metered(self, usage: dict[str, AgentUsage]) -> "MeteredLLM":
        return MeteredLLM(self, usage)

    @property
    def label(self) -> str:
        models = {self.settings.model_for(a) for a in ("planner", "synthesizer", "executor", "critic")}
        return f"{self.settings.provider}:{'/'.join(sorted(models))}"


class MeteredLLM:
    """Обёртка, которая пишет расход токенов каждого агента в трассу конкретного запуска."""

    def __init__(self, llm: LLM, usage: dict[str, AgentUsage]) -> None:
        self._llm = llm
        self.usage = usage
        self.model = llm.label
        self._settings = llm.settings

    def _extra(self, agent: str) -> dict[str, Any]:
        reasoning = self._settings.reasoning
        effort = reasoning.get(agent, reasoning.get("*"))
        return {"reasoning_effort": effort} if effort else {}

    def _record(self, agent: str, completion: Completion | None, started: float) -> None:
        u = self.usage.setdefault(agent, AgentUsage())
        u.calls += 1
        u.latency_ms += int((time.perf_counter() - started) * 1000)
        if completion is not None:
            u.prompt_tokens += completion.prompt_tokens
            u.completion_tokens += completion.completion_tokens
            u.cost_rub += completion.cost_rub

    async def _limited(self, coro):
        if self._llm._semaphore is None:
            return await coro
        async with self._llm._semaphore:
            return await coro

    async def structured(self, agent: str, system: str, user: str, schema: type[T]) -> T:
        """Structured output: модель обязана вернуть JSON по схеме pydantic-модели."""
        model = self._settings.model_for(agent)
        last_error = ""
        for _ in range(2):
            started = time.perf_counter()
            try:
                parsed, error, completion = await self._limited(
                    self._llm.backend.structured(model, system, user, schema, self._extra(agent))
                )
            except LLMError as exc:
                self._record(agent, None, started)
                last_error = str(exc)
                continue
            self._record(agent, completion, started)
            if parsed is not None:
                return parsed
            last_error = error
        raise LLMError(f"[{agent}] {last_error}")

    async def chat(
        self,
        agent: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> Any:
        """Обычный chat completion (с инструментами или без). Возвращает message."""
        started = time.perf_counter()
        try:
            completion = await self._limited(
                self._llm.backend.chat(self._settings.model_for(agent), messages, tools, tool_choice,
                                       self._extra(agent))
            )
        except LLMError as exc:
            self._record(agent, None, started)
            raise LLMError(f"[{agent}] {exc}") from exc
        self._record(agent, completion, started)
        return completion.message


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
