"""Агент 3 (ArgSynthesizer) и агент 4 (Self-Healing Executor).

Synthesizer получает сжатые схемы кандидатов и через function calling выбирает инструмент
и аргументы. Executor выполняет вызов на реальном MCP-сервере и ведёт короткий цикл шага:
- сервер вернул ошибку → текст ошибки уходит модели, и она исправляет вызов (self-healing);
- вызов успешен, но цель шага ещё не достигнута (например, сначала нужно было узнать
  схему) → модель может сделать следующий вызов;
- шаг закрывается, когда модель отвечает без вызова инструмента, то есть цель достигнута.
Перед каждым вызовом работает Policy Guard.
"""

from __future__ import annotations

import datetime as dt
import json

from ..contracts import Attempt, GuardVerdict, StepStatus, StepTrace, ToolCall
from ..hub import MCPHub
from ..llm import LLMError, MeteredLLM, parse_tool_arguments
from ..tokens import clip_tokens, count_tokens
from . import guard
from .selector import Selection

SYSTEM = """You are the tool-calling agent of PolyMCP Router. You execute ONE step of a larger plan
using the provided tools.
- Use concrete values from the step goal, the original request and the facts from previous steps.
- Fill only parameters you need; respect required fields, types and enums.
- Prefer the single call that directly returns the data the step needs.
- If a call failed, read the error and fix it (different arguments or another tool).
- When the tool results already contain what is needed, stop calling tools and reply with text in
  the format given in the latest instruction.
- If the step asks for something the tools cannot do or that would modify or destroy data
  (the tools are read-only), call refuse_step with the reason. Never substitute a different,
  harmless action for the one requested.
Today is {today}."""

# Завершающий ход шага сразу несёт пользу: факты для следующих шагов (вместо отдельного Condenser)
# или финальный ответ, если план из одного шага (вместо отдельного Critic).
CONTINUE_FACTS = (
    "If the results above already contain what the step needs, reply WITHOUT a tool call with the facts "
    "for the next agents: every relevant ID, number, name, date, path and status exactly as in the results, "
    "at most ~120 words, in the user's language. If the results do not establish what the step asks, say "
    "what they show and what remains unknown. Otherwise make the next tool call."
)
INCOMPLETE = "INCOMPLETE:"
CONTINUE_ANSWER = (
    "If the results above answer the original request, reply WITHOUT a tool call with the final answer for "
    "the user, in the user's language, using only these results and never inventing data. If the needed "
    f"data cannot be obtained with these tools, start the reply with {INCOMPLETE} and say what is missing. "
    "Otherwise make the next tool call."
)

REFUSE_TOOL = {
    "type": "function",
    "function": {
        "name": "refuse_step",
        "description": "Refuse the step: it cannot be done with these tools or would modify/destroy data.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Why, in the user's language"}},
            "required": ["reason"],
        },
    },
}

OUTPUT_CLIP_TOKENS = 6000  # сколько сырого ответа сервера хранить и передавать дальше


class Executor:
    def __init__(self, llm: MeteredLLM, hub: MCPHub, max_attempts: int = 3, max_calls: int = 4) -> None:
        self.llm = llm
        self.hub = hub
        self.max_attempts = max_attempts  # сколько ошибок подряд допускается
        self.max_calls = max_calls  # сколько всего вызовов на шаг

    async def run(
        self, query: str, trace: StepTrace, selection: Selection, dep_facts: dict[int, str], final: bool = False
    ) -> tuple[str, str | None]:
        """Выполняет шаг. Возвращает (сырые ответы успешных вызовов, завершающий текст модели или None).

        final=True — шаг единственный в плане: завершающий текст будет финальным ответом пользователю.
        """
        step = trace.step
        continue_prompt = CONTINUE_ANSWER if final else CONTINUE_FACTS
        if not selection.tools:
            trace.status, trace.error = StepStatus.failed, f"Нет инструментов для сервера {step.server!r}"
            return "", None

        facts = "\n".join(f"[step {i}] {f}" for i, f in dep_facts.items()) or "none"
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM.format(today=dt.date.today().isoformat())},
            {
                "role": "user",
                "content": (
                    f"Original request: {query}\n"
                    f"Current step ({step.server}): {step.goal}\n"
                    f"Facts from previous steps:\n{facts}"
                ),
            },
        ]
        outputs: list[str] = []
        consecutive_errors = 0
        closing: str | None = None

        for call_no in range(1, self.max_calls + 1):
            first = call_no == 1
            agent = "synthesizer" if first else "executor"
            message = await self.llm.chat(
                agent, messages, tools=[*selection.openai_tools, REFUSE_TOOL],
                tool_choice="required" if first else "auto",
            )
            if not message.tool_calls:
                if first:
                    raise LLMError(f"[{agent}] модель не вызвала инструмент: {(message.content or '')[:200]}")
                closing = (message.content or "").strip() or None
                break  # модель считает, что цель шага достигнута

            tc = message.tool_calls[0]
            if tc.function.name == REFUSE_TOOL["function"]["name"]:
                try:
                    reason = str(parse_tool_arguments(tc.function.arguments).get("reason", ""))
                except LLMError:
                    reason = ""
                reason = reason or "агент отказался выполнять шаг"
                trace.guard = GuardVerdict(allowed=False, reason=f"Отказ агента: {reason}")
                trace.status, trace.error = StepStatus.blocked, f"Отказ агента: {reason}"
                return "\n\n".join(outputs), None
            spec = selection.by_qualified_name(tc.function.name)
            parse_error = ""
            try:
                arguments = parse_tool_arguments(tc.function.arguments)
            except LLMError as exc:
                arguments, parse_error = {}, str(exc)
            call = ToolCall(
                server=spec.server if spec else step.server,
                tool=spec.name if spec else tc.function.name,
                arguments=arguments,
            )
            messages.append({
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [{
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
                }],
            })

            verdict = guard.check(call, spec, self.hub.policy)
            trace.guard = verdict
            if not verdict.allowed:
                trace.attempts.append(Attempt(call=call, ok=False, output=f"BLOCKED: {verdict.reason}",
                                              output_tokens=0, latency_ms=0))
                trace.status, trace.error = StepStatus.blocked, verdict.reason
                return "", None

            if parse_error:
                ok, output, latency = False, parse_error, 0
            else:
                result = await self.hub.call(call.server, call.tool, call.arguments)
                ok, output, latency = result.ok, result.text, result.latency_ms

            clipped = clip_tokens(output, OUTPUT_CLIP_TOKENS)
            trace.attempts.append(Attempt(call=call, ok=ok, output=clipped,
                                          output_tokens=count_tokens(output), latency_ms=latency))
            if ok:
                consecutive_errors = 0
                outputs.append(clipped)
                content = f"{clipped}\n\n{continue_prompt}"
            else:
                consecutive_errors += 1
                if consecutive_errors >= self.max_attempts:
                    break
                content = f"ERROR from MCP server: {clip_tokens(output, 800)}\nFix the call and try again."
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

        if outputs and consecutive_errors == 0:
            trace.status = StepStatus.ok
        elif outputs:
            # были полезные ответы, но последний вызов так и не удался
            trace.status = StepStatus.ok
            trace.error = f"последний вызов завершился ошибкой: {trace.attempts[-1].output[:200]}"
        else:
            trace.status = StepStatus.failed
            trace.error = f"Не удалось выполнить шаг: {trace.attempts[-1].output[:300]}"
        return "\n\n".join(outputs), closing


def describe_call(call: ToolCall) -> str:
    return f"{call.server}.{call.tool}({json.dumps(call.arguments, ensure_ascii=False)})"
