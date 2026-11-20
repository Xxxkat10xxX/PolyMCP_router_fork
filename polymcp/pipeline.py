"""Оркестрация: Planner → (Selector → Synthesizer/Guard → Executor → Condenser) по DAG → Critic.

Независимые шаги DAG выполняются параллельно волнами. Если Critic считает ответ
неполным, Planner достраивает план (не больше max_replans раз).

Экономия вызовов модели: завершающий ход Executor сразу пишет факты шага (Condenser
вызывается только как запасной путь), а если план из одного шага — финальный ответ,
и тогда Critic не нужен. Critic подключается для многошаговых планов, неполных ответов и сбоев.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from .agents.condenser import Condenser, Critic
from .agents.executor import INCOMPLETE, Executor
from .agents.planner import Planner
from .agents.selector import Selector
from .contracts import PlanStep, RunTrace, StepStatus, StepTrace
from .hub import MCPHub
from .llm import LLM, LLMError

EventHandler = Callable[[str, Any], None]


class Router:
    def __init__(self, hub: MCPHub, llm: LLM, top_k: int = 5, max_replans: int = 1, max_attempts: int = 3) -> None:
        self.hub = hub
        self.llm = llm
        self.selector = Selector(list(hub.tools.values()), top_k=top_k)
        self.max_replans = max_replans
        self.max_attempts = max_attempts

    async def run(self, query: str, on_event: EventHandler | None = None) -> RunTrace:
        emit = on_event or (lambda kind, payload: None)
        trace = RunTrace(query=query, mode="polymcp", model=self.llm.label)
        llm = self.llm.metered(trace.usage)
        planner, executor = Planner(llm, self.hub), Executor(llm, self.hub, self.max_attempts)
        condenser, critic = Condenser(llm), Critic(llm)
        started = time.perf_counter()
        try:
            plan = await planner.plan(query)
            trace.plan_reasoning = plan.reasoning
            emit("plan", plan)
            single = len(plan.steps) == 1
            closings = await self._execute(query, plan.steps, trace, executor, condenser, emit, final=single)

            if single and self._answer_single_step(trace, closings.get(plan.steps[0].id)):
                return trace

            while True:
                verdict = await critic.review(query, trace.steps)
                trace.answer, trace.complete, trace.critic_note = verdict.answer, verdict.complete, verdict.missing
                if verdict.complete or trace.replans >= self.max_replans or not verdict.missing.strip():
                    break
                trace.replans += 1
                extra = await planner.replan(query, [(s.step, s.facts) for s in trace.steps], verdict.missing)
                emit("replan", extra)
                await self._execute(query, extra.steps, trace, executor, condenser, emit)
        except LLMError as exc:
            trace.error = str(exc)
        finally:
            self._finalize(trace, started, emit)
        return trace

    @staticmethod
    def _answer_single_step(trace: RunTrace, closing: str | None) -> bool:
        """План из одного шага: ответ без отдельного вызова Critic. False — нужен Critic."""
        step = trace.steps[0]
        if step.status is StepStatus.blocked:
            reason = step.error.removeprefix("Отказ агента:").strip()
            trace.answer, trace.complete = f"Не выполнено. {reason}", True
            return True
        if step.status is StepStatus.ok and closing and not closing.startswith(INCOMPLETE):
            trace.answer, trace.complete = closing, True
            return True
        return False

    @staticmethod
    def _finalize(trace: RunTrace, started: float, emit: EventHandler) -> None:
        trace.wall_ms = int((time.perf_counter() - started) * 1000)
        trace.blocked = any(s.status is StepStatus.blocked for s in trace.steps)
        trace.tools_called = [
            f"{a.call.server}.{a.call.tool}"
            for s in trace.steps for a in s.attempts if not a.output.startswith("BLOCKED")
        ]
        emit("done", trace)

    async def _execute(
        self, query, steps: list[PlanStep], trace: RunTrace, executor, condenser, emit, final: bool = False
    ) -> dict[int, str | None]:
        """Выполняет шаги волнами. Возвращает завершающие тексты Executor по id шага."""
        by_id = {s.step.id: s for s in trace.steps}
        new = [StepTrace(step=step) for step in steps]
        trace.steps.extend(new)
        by_id.update({s.step.id: s for s in new})
        pending = list(new)
        closings: dict[int, str | None] = {}

        async def run_step(st: StepTrace) -> None:
            emit("step_start", st)
            selection = self.selector.select(st.step)
            st.candidates = [t.qualified_name for t in selection.tools]
            st.schema_tokens_raw, st.schema_tokens_pruned = selection.raw_tokens, selection.pruned_tokens
            dep_facts = {d: by_id[d].facts for d in st.step.depends_on if d in by_id}
            raw, closing = await executor.run(query, st, selection, dep_facts, final=final)
            closings[st.step.id] = closing
            await condenser.condense(query, st, raw, closing)
            emit("step_done", st)

        while pending:
            done_ids = {s.step.id for s in trace.steps if s.status is not StepStatus.pending}
            wave = [s for s in pending if all(d in done_ids for d in s.step.depends_on)]
            if not wave:  # защита от циклов, хотя валидатор плана их не пропускает
                wave = pending[:1]
            await asyncio.gather(*(run_step(s) for s in wave))
            pending = [s for s in pending if s not in wave]
        return closings
