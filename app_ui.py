"""Streamlit UI для PolyMCP Router.

    streamlit run app_ui.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from polymcp.agents.executor import describe_call
from polymcp.agents.selector import openai_tool
from polymcp.baseline import run_baseline
from polymcp.config import ConfigError, Settings
from polymcp.contracts import Plan, RunTrace, StepStatus, StepTrace
from polymcp.hub import MCPHub
from polymcp.llm import LLM
from polymcp.pipeline import Router
from polymcp.tokens import count_tokens

ROOT = Path(__file__).resolve().parent
EXAMPLES = [
    "Найди открытые критические баги в PostgreSQL и найди их упоминания в логах через shell.",
    "Какой сервис и какая версия упали при последнем неудачном деплое, и какая ошибка в logs/app.log объясняет причину?",
    "Сколько открытых багов в PostgreSQL, сколько строк ERROR в logs/app.log и какое сообщение у самого первого "
    "коммита в репозитории Xxxkat10xxX/PolyMCP_router_fork?",
    "Удали таблицу users в PostgreSQL.",
]
STATUS_ICON = {StepStatus.ok: "✅", StepStatus.blocked: "⛔", StepStatus.failed: "❌",
               StepStatus.pending: "⏳", StepStatus.skipped: "⏭️"}

st.set_page_config(page_title="PolyMCP Router", page_icon="🧭", layout="wide")


@st.cache_data(ttl=300, show_spinner="Подключаюсь к MCP-серверам…")
def inspect_servers(config: str) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        async with MCPHub(ROOT / config) as hub:
            return {
                "servers": {s: len(hub.tools_of(s)) for s in hub.servers},
                "unavailable": hub.unavailable,
                "catalog_tokens": count_tokens([openai_tool(t, pruned=False) for t in hub.tools.values()]),
                "tool_count": len(hub.tools),
            }
    return asyncio.run(go())


def plan_graph(steps: list[StepTrace] | list) -> str:
    lines = ["digraph { rankdir=LR; node [shape=box, style=rounded, fontsize=10];"]
    for item in steps:
        step = item.step if isinstance(item, StepTrace) else item
        status = item.status if isinstance(item, StepTrace) else StepStatus.pending
        color = {StepStatus.ok: "#16a34a", StepStatus.blocked: "#dc2626",
                 StepStatus.failed: "#ea580c"}.get(status, "#64748b")
        label = f"{step.id}. {step.server}\\n{step.goal[:48]}{'…' if len(step.goal) > 48 else ''}".replace('"', "'")
        lines.append(f'  s{step.id} [label="{label}", color="{color}"];')
        lines += [f"  s{d} -> s{step.id};" for d in step.depends_on]
    lines.append("}")
    return "\n".join(lines)


def run_query(config: str, query: str, mode: str, log) -> RunTrace:
    def on_event(kind: str, payload: Any) -> None:
        if kind in ("plan", "replan") and isinstance(payload, Plan):
            title = "План" if kind == "plan" else "Дополнительные шаги"
            log.write(f"**{title}:** " + "; ".join(f"[{s.id}] {s.server}: {s.goal}" for s in payload.steps))
        elif kind == "step_start":
            log.write(f"⏳ шаг {payload.step.id} ({payload.step.server})…")
        elif kind == "step_done":
            log.write(f"{STATUS_ICON[payload.status]} шаг {payload.step.id}: {payload.status.value}")

    async def go() -> RunTrace:
        settings = Settings.from_env()
        llm = LLM(settings)
        async with MCPHub(ROOT / config) as hub:
            if mode == "baseline":
                return await run_baseline(hub, llm, query)
            return await Router(hub, llm).run(query, on_event=on_event)

    return asyncio.run(go())


def render_trace(trace: RunTrace) -> None:
    if trace.error:
        st.error(f"Ошибка: {trace.error}")
    st.markdown(f"#### Ответ {'(полный)' if trace.complete else '(неполный)'}")
    st.info(trace.answer or "—")
    if trace.critic_note and not trace.complete:
        st.caption(f"Критик: не хватает — {trace.critic_note}")

    c = st.columns(5)
    c[0].metric("LLM-вызовов", trace.llm_calls)
    c[1].metric("Prompt-токенов", f"{trace.prompt_tokens:,}".replace(",", " "))
    c[2].metric("Completion-токенов", f"{trace.completion_tokens:,}".replace(",", " "))
    c[3].metric("Стоимость, ₽", f"{trace.cost_rub:.2f}")
    c[4].metric("Время, с", f"{trace.wall_ms / 1000:.1f}")

    if trace.mode == "polymcp" and trace.steps:
        st.markdown("#### Граф плана")
        if trace.plan_reasoning:
            st.caption(trace.plan_reasoning)
        st.graphviz_chart(plan_graph(trace.steps))

    st.markdown("#### Шаги")
    for s in trace.steps:
        title = f"{STATUS_ICON[s.status]} [{s.step.id}] {s.step.server}: {s.step.goal}"
        with st.expander(title, expanded=s.status is not StepStatus.ok):
            if trace.mode == "polymcp":
                st.caption(
                    f"Кандидаты (Selector): {', '.join(c.split('__', 1)[1] for c in s.candidates)} · "
                    f"схемы {s.schema_tokens_raw} → {s.schema_tokens_pruned} ток"
                )
            if s.error:
                st.warning(s.error)
            for i, a in enumerate(s.attempts, 1):
                st.markdown(f"**Вызов {i}** {'✅' if a.ok else '❌'} `{describe_call(a.call)}` · {a.latency_ms} мс")
                st.code(a.output[:3000] + ("…" if len(a.output) > 3000 else ""), language="json")
            if s.facts and trace.mode == "polymcp":
                st.markdown(f"**Факты (Condenser):** {s.facts}")

    if trace.usage:
        st.markdown("#### Расход по агентам")
        st.dataframe(
            pd.DataFrame([{"агент": k, **v.model_dump()} for k, v in trace.usage.items()]),
            hide_index=True, use_container_width=True,
        )


def render_benchmarks() -> None:
    files = sorted((ROOT / "bench" / "results").glob("latest-*.json"))
    if not files:
        st.info("Результатов пока нет. Запустите: `python -m bench.run`")
        return
    for path in files:
        report = json.loads(path.read_text(encoding="utf-8"))
        st.markdown(f"### Каталог `{report.get('config', path.stem)}` — {report['tool_count']} инструментов, "
                    f"{report['catalog_schema_tokens']} токенов схем")
        st.caption(f"{report['created_at']} · модель {report['model']}")
        summary = pd.DataFrame(report["summary"]).T
        st.dataframe(summary, use_container_width=True)
        per_task = pd.DataFrame([
            {"задача": r["task_id"], "категория": r["category"], "режим": r["mode"],
             "итог": "✅" if r["passed"] else "❌", "prompt": r["prompt_tokens"], "LLM": r["llm_calls"],
             "₽": r["cost_rub"], "с": round(r["wall_ms"] / 1000, 1), "замечания": "; ".join(r["failures"])}
            for r in report["results"]
        ])
        st.dataframe(per_task, hide_index=True, use_container_width=True)


st.title("🧭 PolyMCP Router")
st.caption("Пять агентов маршрутизируют запрос по реальным MCP-серверам: Planner → Selector/Pruner → "
           "Synthesizer + Policy Guard → Self-Healing Executor → Condenser/Critic")

try:
    settings = Settings.from_env()
except ConfigError as exc:
    st.error(str(exc))
    st.stop()

with st.sidebar:
    configs = [p.name for p in sorted(ROOT.glob("mcp_servers*.json"))]
    config = st.selectbox("Конфиг MCP-серверов", configs, index=configs.index("mcp_servers.json"))
    mode_label = st.radio("Режим", ["PolyMCP Router", "Baseline (все инструменты)", "Сравнить оба"])
    st.caption(f"Модель: `{settings.model}` · {settings.base_url}")
    try:
        info = inspect_servers(config)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Не удалось подключиться к MCP-серверам: {exc}")
        st.stop()
    for name, n in info["servers"].items():
        st.success(f"{name}: {n} инструментов")
    for name, reason in info["unavailable"].items():
        st.warning(f"{name}: недоступен — {reason}")
    st.caption(f"Каталог: {info['tool_count']} инструментов, {info['catalog_tokens']} токенов схем")

tab_run, tab_bench = st.tabs(["Запрос", "Бенчмарк"])

with tab_run:
    example = st.selectbox("Пример", ["—"] + EXAMPLES)
    query = st.text_area("Запрос", value="" if example == "—" else example, height=90)
    if st.button("Запустить", type="primary", disabled=not query.strip()):
        modes = {"PolyMCP Router": ["polymcp"], "Baseline (все инструменты)": ["baseline"],
                 "Сравнить оба": ["polymcp", "baseline"]}[mode_label]
        columns = st.columns(len(modes))
        for column, mode in zip(columns, modes):
            with column:
                st.subheader("PolyMCP Router" if mode == "polymcp" else "Baseline")
                with st.status("Выполняю…", expanded=True) as status:
                    trace = run_query(config, query.strip(), mode, status)
                    status.update(label="Готово" if not trace.error else "Ошибка",
                                  state="complete" if not trace.error else "error", expanded=False)
                render_trace(trace)

with tab_bench:
    render_benchmarks()
