"""Модели данных, которыми обмениваются агенты, и трасса выполнения."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    """Инструмент так, как его отдал MCP-сервер в list_tools."""

    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    read_only: bool | None = None  # annotations.readOnlyHint
    destructive: bool | None = None  # annotations.destructiveHint

    @property
    def qualified_name(self) -> str:
        return f"{self.server}__{self.name}"


class PlanStep(BaseModel):
    id: int = Field(description="Step number starting from 1")
    server: str = Field(description="Exactly one server name from the list of available servers")
    goal: str = Field(description="What this step must find out, self-contained, in the user's language")
    tool_hint: str = Field(
        description="3-8 English keywords describing the operation, e.g. 'list commits branch' or 'grep log errors'"
    )
    depends_on: list[int] = Field(description="ids of earlier steps whose results this step needs")


class Plan(BaseModel):
    reasoning: str = Field(description="One or two sentences on how the request decomposes")
    steps: list[PlanStep]


class StepStatus(str, Enum):
    pending = "pending"
    ok = "ok"
    blocked = "blocked"
    failed = "failed"
    skipped = "skipped"


class ToolCall(BaseModel):
    server: str
    tool: str
    arguments: dict[str, Any]


class Attempt(BaseModel):
    call: ToolCall
    ok: bool
    output: str  # сырой ответ сервера или текст ошибки (обрезан для трассы)
    output_tokens: int
    latency_ms: int


class GuardVerdict(BaseModel):
    allowed: bool
    reason: str = ""


class StepTrace(BaseModel):
    step: PlanStep
    candidates: list[str] = Field(default_factory=list)  # qualified names
    schema_tokens_raw: int = 0
    schema_tokens_pruned: int = 0
    guard: GuardVerdict | None = None
    attempts: list[Attempt] = Field(default_factory=list)
    facts: str = ""
    facts_tokens: int = 0
    status: StepStatus = StepStatus.pending
    error: str = ""


class AgentUsage(BaseModel):
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_rub: float = 0.0  # если провайдер (aitunnel) возвращает стоимость в usage


class RunTrace(BaseModel):
    query: str
    mode: str = "polymcp"
    model: str = ""
    plan_reasoning: str = ""
    steps: list[StepTrace] = Field(default_factory=list)
    replans: int = 0
    answer: str = ""
    complete: bool = False
    critic_note: str = ""
    usage: dict[str, AgentUsage] = Field(default_factory=dict)
    tools_called: list[str] = Field(default_factory=list)
    blocked: bool = False
    error: str = ""
    wall_ms: int = 0

    @property
    def prompt_tokens(self) -> int:
        return sum(u.prompt_tokens for u in self.usage.values())

    @property
    def completion_tokens(self) -> int:
        return sum(u.completion_tokens for u in self.usage.values())

    @property
    def cost_rub(self) -> float:
        return round(sum(u.cost_rub for u in self.usage.values()), 4)

    @property
    def llm_calls(self) -> int:
        return sum(u.calls for u in self.usage.values())
