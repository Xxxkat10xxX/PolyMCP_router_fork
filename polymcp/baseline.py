"""Базовая линия для честного сравнения: один агент, которому отдают ВСЕ инструменты
всех серверов с полными схемами (так работает обычный MCP-клиент), и стандартный цикл
function calling. Тот же Policy Guard, та же модель, то же обрезание ответов.
"""

from __future__ import annotations

import datetime as dt
import time

from .agents import guard
from .agents.executor import OUTPUT_CLIP_TOKENS
from .agents.selector import openai_tool
from .contracts import Attempt, PlanStep, RunTrace, StepStatus, StepTrace, ToolCall
from .hub import MCPHub
from .llm import LLM, LLMError, parse_tool_arguments
from .tokens import clip_tokens, count_tokens

SYSTEM = """You are an assistant with access to MCP tools (PostgreSQL, a sandboxed shell, GitHub).
Answer the user's request using the tools. Never invent data. Refuse unsafe or destructive actions.
Answer in the user's language. Today is {today}."""


async def run_baseline(hub: MCPHub, llm: LLM, query: str, max_turns: int = 10) -> RunTrace:
    trace = RunTrace(query=query, mode="baseline", model=llm.model)
    metered = llm.metered(trace.usage)
    tools = [openai_tool(t, pruned=False) for t in hub.tools.values()]
    schema_tokens = count_tokens(tools)
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM.format(today=dt.date.today().isoformat())},
        {"role": "user", "content": query},
    ]
    started = time.perf_counter()
    call_no = 0
    try:
        for _ in range(max_turns):
            message = await metered.chat("baseline", messages, tools=tools)
            if not message.tool_calls:
                trace.answer = message.content or ""
                trace.complete = True
                break
            messages.append({
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                    for tc in message.tool_calls
                ],
            })
            for tc in message.tool_calls:
                call_no += 1
                spec = hub.tools.get(tc.function.name)
                server, _, name = tc.function.name.partition("__")
                parse_error = ""
                try:
                    args = parse_tool_arguments(tc.function.arguments)
                except LLMError as exc:
                    args, parse_error = {}, str(exc)
                call = ToolCall(server=server, tool=name, arguments=args)
                st = StepTrace(
                    step=PlanStep(id=call_no, server=server, goal="(baseline tool call)", tool_hint="", depends_on=[]),
                    candidates=[tc.function.name], schema_tokens_raw=schema_tokens, schema_tokens_pruned=schema_tokens,
                )
                verdict = guard.check(call, spec, hub.policy)
                st.guard = verdict
                if not verdict.allowed:
                    output, ok, latency = f"BLOCKED: {verdict.reason}", False, 0
                    st.status, st.error = StepStatus.blocked, verdict.reason
                elif parse_error:
                    output, ok, latency = parse_error, False, 0
                    st.status, st.error = StepStatus.failed, parse_error
                else:
                    result = await hub.call(server, name, args)
                    output, ok, latency = result.text, result.ok, result.latency_ms
                    st.status = StepStatus.ok if ok else StepStatus.failed
                clipped = clip_tokens(output, OUTPUT_CLIP_TOKENS)
                st.attempts.append(Attempt(call=call, ok=ok, output=clipped,
                                           output_tokens=count_tokens(output), latency_ms=latency))
                trace.steps.append(st)
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": clipped if ok else f"ERROR: {clipped}"})
        else:
            trace.error = f"Агент не закончил за {max_turns} ходов"
    except LLMError as exc:
        trace.error = str(exc)
    trace.wall_ms = int((time.perf_counter() - started) * 1000)
    trace.blocked = any(s.status is StepStatus.blocked for s in trace.steps)
    trace.tools_called = [
        f"{a.call.server}.{a.call.tool}" for s in trace.steps for a in s.attempts if not a.output.startswith("BLOCKED")
    ]
    return trace
