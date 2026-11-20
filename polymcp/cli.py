"""CLI: python -m polymcp.cli "запрос" [--baseline] [--json]"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .agents.executor import describe_call
from .baseline import run_baseline
from .config import Settings
from .contracts import RunTrace
from .hub import MCPHub
from .llm import LLM
from .pipeline import Router


def print_trace(trace: RunTrace) -> None:
    print(f"\n=== {trace.mode} | model={trace.model} | {trace.wall_ms} ms ===")
    if trace.plan_reasoning:
        print(f"План: {trace.plan_reasoning}")
    for s in trace.steps:
        deps = f" <- {s.step.depends_on}" if s.step.depends_on else ""
        print(f"\n[{s.step.id}] {s.status.value.upper()} ({s.step.server}){deps} {s.step.goal}")
        if s.candidates and trace.mode == "polymcp":
            print(f"    кандидаты: {', '.join(c.split('__', 1)[1] for c in s.candidates)} "
                  f"| схемы {s.schema_tokens_raw} -> {s.schema_tokens_pruned} ток")
        for i, a in enumerate(s.attempts, 1):
            print(f"    попытка {i}: {describe_call(a.call)} -> {'OK' if a.ok else 'ERR'} ({a.latency_ms} ms)")
            if not a.ok:
                print(f"        {a.output[:200]}")
        if s.facts:
            print(f"    факты: {s.facts[:400]}")
    if trace.error:
        print(f"\nОШИБКА: {trace.error}")
    print(f"\nОтвет ({'полный' if trace.complete else 'неполный'}):\n{trace.answer}")
    usage = ", ".join(f"{k}: {v.calls}x/{v.prompt_tokens}+{v.completion_tokens}" for k, v in trace.usage.items())
    print(f"\nLLM: {trace.llm_calls} вызовов, prompt {trace.prompt_tokens} + completion {trace.completion_tokens} ток"
          f" [{usage}]")


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    llm = LLM(settings)
    async with MCPHub(settings.servers_config) as hub:
        for name, reason in hub.unavailable.items():
            print(f"[!] сервер {name} недоступен: {reason}", file=sys.stderr)
        trace = await (run_baseline(hub, llm, args.query) if args.baseline else Router(hub, llm).run(args.query))
    if args.json:
        print(trace.model_dump_json(indent=2))
    else:
        print_trace(trace)
    return 1 if trace.error else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="PolyMCP Router")
    parser.add_argument("query")
    parser.add_argument("--baseline", action="store_true", help="один агент со всеми инструментами")
    parser.add_argument("--json", action="store_true")
    sys.exit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
