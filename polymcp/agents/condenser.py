"""Агент 5. Condenser (сжатие ответов в факты) и Critic (проверка полноты и итоговый ответ)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..contracts import StepStatus, StepTrace
from ..llm import MeteredLLM
from ..tokens import count_tokens

CONDENSE_SYSTEM = """You compress raw MCP tool output into facts for the next agents of PolyMCP Router.
Keep every identifier, number, name, date, SHA, path and status exactly as in the output.
Keep only what is relevant to the step goal and the original request. Max ~120 words.
If the output is empty or has no relevant data, say so explicitly. Write in the user's language.
Report only what the output actually shows. Do not stretch it to fit the goal: if the output does not
establish what the goal asks (e.g. it shows the latest item while the goal asks for the earliest),
say what it shows and what remains unknown."""

CRITIC_SYSTEM = """You are the reviewer of PolyMCP Router. Using ONLY the facts gathered by the steps,
write the final answer to the user's request in the user's language.
- Never invent data that is not in the facts. If something could not be obtained, say so.
- If a step was BLOCKED by the safety guard, state clearly that the action was refused and why.
- complete=true when the facts answer the whole request (a refused unsafe action also counts as
  handled). Otherwise complete=false and describe in `missing` what concrete data is still needed."""

PASS_THROUGH_TOKENS = 250


class _Facts(BaseModel):
    facts: str


class Verdict(BaseModel):
    complete: bool
    answer: str = Field(description="Final answer for the user")
    missing: str = Field(description="What data is still missing; empty if complete")


class Condenser:
    def __init__(self, llm: MeteredLLM) -> None:
        self.llm = llm

    async def condense(self, query: str, trace: StepTrace, raw_output: str, closing: str | None = None) -> None:
        if trace.status is StepStatus.ok and closing:
            # Executor уже написал факты завершающим ходом — отдельный вызов модели не нужен.
            trace.facts = closing
        elif trace.status is not StepStatus.ok:
            trace.facts = f"{trace.status.value.upper()}: {trace.error}"
        elif count_tokens(raw_output) <= PASS_THROUGH_TOKENS:
            trace.facts = raw_output.strip() or "(empty result)"
        else:
            result = await self.llm.structured(
                "condenser",
                CONDENSE_SYSTEM,
                f"Original request: {query}\nStep goal: {trace.step.goal}\nRaw tool output:\n{raw_output}",
                _Facts,
            )
            trace.facts = result.facts
        trace.facts_tokens = count_tokens(trace.facts)


class Critic:
    def __init__(self, llm: MeteredLLM) -> None:
        self.llm = llm

    async def review(self, query: str, steps: list[StepTrace]) -> Verdict:
        lines = [
            f"[{s.step.id}] ({s.step.server}, {s.status.value}) {s.step.goal}\n    facts: {s.facts}"
            for s in steps
        ]
        user = f"User request:\n{query}\n\nSteps and facts:\n" + "\n".join(lines)
        return await self.llm.structured("critic", CRITIC_SYSTEM, user, Verdict)
