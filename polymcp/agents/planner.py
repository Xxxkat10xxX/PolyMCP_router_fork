"""Агент 1. Planner: декомпозиция запроса в DAG шагов, по одному MCP-серверу на шаг."""

from __future__ import annotations

import datetime as dt

from ..contracts import Plan, PlanStep
from ..hub import MCPHub
from ..llm import LLMError, MeteredLLM

SYSTEM = """You are the planning agent of PolyMCP Router.
Decompose the user's request into the minimal set of steps. Each step is executed by exactly one
MCP server (choose from the list below) with one tool call, so keep every step atomic.

Rules:
- Use only the listed servers. Never invent servers.
- depends_on lists earlier step ids whose results the step really needs (e.g. it uses an id found
  earlier). Independent steps must have empty depends_on so they can run in parallel.
- Keep the user's intent exactly, including risky or destructive requests: do not silently drop or
  soften them. A separate guard decides what is allowed.
- Do not add steps for formatting or summarising: the final answer is written by another agent.
- Do not add exploratory steps (listing tables, describing schemas, listing files) when the server
  description already tells you what you need; the executing agent can recover from errors itself.
- A server name in the request says WHERE the data lives, it is not a filter: "bugs in PostgreSQL"
  means rows of the bugs table in the PostgreSQL database, not bugs of a "PostgreSQL" component.
- Write `goal` in the user's language and make it self-contained (repeat owner/repo, table names,
  file names, dates from the request).
- Today is {today}.

Available servers:
{servers}"""

REPLAN = """The previous steps did not fully answer the request.
Already executed steps and their facts:
{done}

What is still missing according to the reviewer: {missing}

Plan ONLY the additional steps needed (ids start at {next_id}; they may depend on earlier ids)."""


class PlanError(LLMError):
    pass


def describe_servers(hub: MCPHub) -> str:
    lines = []
    for server in hub.servers:
        names = ", ".join(t.name for t in hub.tools_of(server))
        lines.append(f"- {server}: {hub.descriptions.get(server, '')}\n  tools: {names}")
    return "\n".join(lines)


def validate(plan: Plan, servers: list[str], first_id: int = 1, known_ids: set[int] | None = None) -> list[PlanStep]:
    """Проверяет DAG: уникальные id, существующие серверы, зависимости только назад."""
    known = set(known_ids or ())
    seen: set[int] = set()
    problems: list[str] = []
    if not plan.steps:
        problems.append("plan has no steps")
    for step in plan.steps:
        if step.id < first_id or step.id in seen or step.id in known:
            problems.append(f"step id {step.id} is duplicated or below {first_id}")
        if step.server not in servers:
            problems.append(f"step {step.id}: unknown server {step.server!r}; use one of {servers}")
        bad = [d for d in step.depends_on if d >= step.id or (d not in seen and d not in known)]
        if bad:
            problems.append(f"step {step.id}: depends_on {bad} must reference earlier steps")
        seen.add(step.id)
    if problems:
        raise PlanError("; ".join(problems))
    return plan.steps


class Planner:
    def __init__(self, llm: MeteredLLM, hub: MCPHub) -> None:
        self.llm = llm
        self.hub = hub

    def _system(self) -> str:
        return SYSTEM.format(today=dt.date.today().isoformat(), servers=describe_servers(self.hub))

    async def _ask(self, user: str, first_id: int, known: set[int]) -> Plan:
        error = ""
        for _ in range(2):
            prompt = user if not error else f"{user}\n\nYour previous plan was invalid: {error}. Fix it."
            plan = await self.llm.structured("planner", self._system(), prompt, Plan)
            try:
                validate(plan, self.hub.servers, first_id, known)
                return plan
            except PlanError as exc:
                error = str(exc)
        raise PlanError(f"Planner returned an invalid plan twice: {error}")

    async def plan(self, query: str) -> Plan:
        return await self._ask(f"User request:\n{query}", first_id=1, known=set())

    async def replan(self, query: str, done: list[tuple[PlanStep, str]], missing: str) -> Plan:
        next_id = max(s.id for s, _ in done) + 1
        done_text = "\n".join(f"[{s.id}] ({s.server}) {s.goal}\n    facts: {facts}" for s, facts in done)
        user = f"User request:\n{query}\n\n" + REPLAN.format(done=done_text, missing=missing, next_id=next_id)
        return await self._ask(user, first_id=next_id, known={s.id for s, _ in done})
