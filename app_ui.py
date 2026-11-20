"""Streamlit UI для PolyMCP Router.

    streamlit run app_ui.py

Тема задаётся в .streamlit/config.toml (цвета, шрифт), детали — в ui/style.css.
"""

from __future__ import annotations

import asyncio
import html
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
STATUS_LABEL = {StepStatus.ok: "ГОТОВО", StepStatus.blocked: "ОТКАЗ", StepStatus.failed: "ОШИБКА",
                StepStatus.pending: "ЖДЁТ", StepStatus.skipped: "ПРОПУЩЕН"}
AGENTS = [("Planner", "граф шагов"), ("Selector", "top-5 + сжатие схем"), ("Synthesizer", "вызов + guard"),
          ("Executor", "self-healing"), ("Critic", "факты → ответ")]
PAPER, INK, CORAL, MUTED = "#fdfcf0", "#18181b", "#f26d5b", "#e6e4d5"

st.set_page_config(page_title="PolyMCP Router", page_icon="●", layout="wide")
st.markdown(f"<style>{(ROOT / 'ui' / 'style.css').read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def esc(value: Any) -> str:
    return html.escape(str(value))


def section(title: str, note: str = "") -> None:
    extra = f'<span class="pm-count">{esc(note)}</span>' if note else ""
    st.markdown(f'<div class="pm-section"><h3>{esc(title)}</h3>{extra}</div>', unsafe_allow_html=True)


def fmt_int(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


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


def plan_graph(steps: list[StepTrace]) -> str:
    lines = [
        "digraph {",
        '  rankdir=LR; bgcolor="transparent"; nodesep=0.35; ranksep=0.6;',
        f'  node [shape=box, style="filled", fillcolor="{PAPER}", color="{INK}", penwidth=2,'
        f' fontname="Space Grotesk", fontsize=11, fontcolor="{INK}", margin="0.18,0.1"];',
        f'  edge [color="{INK}", penwidth=1.6, arrowsize=0.7];',
    ]
    for item in steps:
        step, status = item.step, item.status
        fill = {StepStatus.blocked: CORAL, StepStatus.failed: MUTED}.get(status, PAPER)
        style = '"filled,dashed"' if status is StepStatus.failed else '"filled"'
        goal = step.goal[:46] + ("…" if len(step.goal) > 46 else "")
        label = f"{step.id} · {step.server.upper()}\\n{goal}".replace('"', "'")
        lines.append(f'  s{step.id} [label="{label}", fillcolor="{fill}", style={style}];')
        lines += [f"  s{d} -> s{step.id};" for d in step.depends_on]
    lines.append("}")
    return "\n".join(lines)


def run_query(config: str, query: str, mode: str, log) -> RunTrace:
    def on_event(kind: str, payload: Any) -> None:
        if kind in ("plan", "replan") and isinstance(payload, Plan):
            title = "План" if kind == "plan" else "Дополнительные шаги"
            log.write(f"**{title}:** " + "; ".join(f"[{s.id}] {s.server}: {s.goal}" for s in payload.steps))
        elif kind == "step_start":
            log.write(f"● шаг {payload.step.id} ({payload.step.server})…")
        elif kind == "step_done":
            log.write(f"{STATUS_LABEL[payload.status]} — шаг {payload.step.id}")

    async def go() -> RunTrace:
        settings = Settings.from_env()
        llm = LLM(settings)
        async with MCPHub(ROOT / config) as hub:
            if mode == "baseline":
                return await run_baseline(hub, llm, query)
            return await Router(hub, llm).run(query, on_event=on_event)

    return asyncio.run(go())


def metric_cards(trace: RunTrace, band_key: str | None) -> None:
    container = st.container(key=band_key) if band_key else st.container()
    with container:
        c = st.columns(5)
        c[0].metric("LLM-вызовов", trace.llm_calls)
        c[1].metric("Prompt-токенов", fmt_int(trace.prompt_tokens))
        c[2].metric("Completion", fmt_int(trace.completion_tokens))
        c[3].metric("Стоимость, ₽", f"{trace.cost_rub:.2f}")
        c[4].metric("Время, с", f"{trace.wall_ms / 1000:.1f}")


def render_trace(trace: RunTrace, compact: bool) -> None:
    if trace.error:
        st.error(f"Ошибка: {trace.error}")

    with st.container(key=f"pm_answer_{trace.mode}"):
        state = "ПОЛНЫЙ" if trace.complete else "НЕПОЛНЫЙ"
        st.markdown(
            f'<div class="pm-panel-head" style="display:flex;justify-content:space-between">'
            f'<span class="pm-eyebrow"><span class="pm-dot"></span>Ответ</span>'
            f'<span class="pm-eyebrow">{state}</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown(trace.answer or "—")
        if trace.critic_note and not trace.complete:
            st.caption(f"Критик: не хватает — {trace.critic_note}")

    metric_cards(trace, None if compact else "pm_band")

    if trace.mode == "polymcp" and trace.steps:
        section("Граф плана", f"{len(trace.steps)} шагов")
        if trace.plan_reasoning:
            st.caption(trace.plan_reasoning)
        st.graphviz_chart(plan_graph(trace.steps), use_container_width=True)

    section("Шаги" if trace.mode == "polymcp" else "Вызовы", f"{len(trace.tools_called)} вызовов инструментов")
    for s in trace.steps:
        title = f"{STATUS_LABEL[s.status]} · [{s.step.id}] {s.step.server.upper()} — {s.step.goal}"
        with st.expander(title, expanded=s.status is not StepStatus.ok):
            if trace.mode == "polymcp":
                st.caption(
                    f"Кандидаты: {', '.join(c.split('__', 1)[1] for c in s.candidates)} · "
                    f"схемы {s.schema_tokens_raw} → {s.schema_tokens_pruned} ток"
                )
            if s.error:
                st.warning(s.error)
            for i, a in enumerate(s.attempts, 1):
                st.markdown(f"**Вызов {i}** · {'ok' if a.ok else 'ошибка'} · `{describe_call(a.call)}` · {a.latency_ms} мс")
                st.code(a.output[:3000] + ("…" if len(a.output) > 3000 else ""), language="json")
            if s.facts and trace.mode == "polymcp":
                st.markdown(f"**Факты:** {s.facts}")

    if trace.usage:
        section("Расход по агентам")
        st.dataframe(
            pd.DataFrame([{"агент": k, **v.model_dump()} for k, v in trace.usage.items()]),
            hide_index=True, use_container_width=True,
        )


def summary_table(summary: dict[str, Any]) -> pd.DataFrame:
    rows = {}
    for mode, s in summary.items():
        rows["PolyMCP" if mode == "polymcp" else "Baseline"] = {
            "решено": f"{s['passed']}/{s['tasks']}",
            "по категориям": ", ".join(f"{k} {v}" for k, v in s["by_category"].items()),
            "покрытие серверов": f"{s['server_coverage']:.0%}" if s.get("server_coverage") is not None else "—",
            "prompt / задача": fmt_int(s["avg_prompt_tokens"]),
            "completion / задача": fmt_int(s["avg_completion_tokens"]),
            "LLM / задача": f"{s['avg_llm_calls']:.1f}",
            "время, с": f"{s['avg_wall_ms'] / 1000:.1f}",
            "₽ за прогон": f"{s['total_cost_rub']:.2f}",
        }
    return pd.DataFrame(rows).T


def render_benchmarks() -> None:
    files = sorted((ROOT / "bench" / "results").glob("latest-*.json"))
    if not files:
        st.info("Результатов пока нет. Запустите: `python -m bench.run`")
        return
    for idx, path in enumerate(files):
        report = json.loads(path.read_text(encoding="utf-8"))
        section(f"Каталог: {report['tool_count']} инструментов",
                f"{fmt_int(report['catalog_schema_tokens'])} токенов схем · {report['created_at'][:10]} · {report['model']}")
        summary = report["summary"]
        with st.container(key=f"pm_band_bench_{idx}"):
            cols = st.columns(4)
            p, b = summary.get("polymcp", {}), summary.get("baseline", {})
            if p and b:
                cols[0].metric("Решено · PolyMCP", f"{p['passed']}/{p['tasks']}", f"baseline {b['passed']}/{b['tasks']}",
                               delta_color="off", delta_arrow="off")
                cols[1].metric("Prompt на задачу", fmt_int(p["avg_prompt_tokens"]),
                               f"baseline {fmt_int(b['avg_prompt_tokens'])}", delta_color="off", delta_arrow="off")
                cols[2].metric("Стоимость, ₽", f"{p['total_cost_rub']:.2f}", f"baseline {b['total_cost_rub']:.2f}",
                               delta_color="off", delta_arrow="off")
                cols[3].metric("Время на задачу, с", f"{p['avg_wall_ms'] / 1000:.1f}",
                               f"baseline {b['avg_wall_ms'] / 1000:.1f}", delta_color="off", delta_arrow="off")
        st.dataframe(summary_table(summary), use_container_width=True)
        per_task = pd.DataFrame([
            {"задача": r["task_id"], "категория": r["category"], "режим": r["mode"],
             "итог": "ok" if r["passed"] else "провал", "prompt": r["prompt_tokens"], "LLM": r["llm_calls"],
             "₽": r["cost_rub"], "с": round(r["wall_ms"] / 1000, 1), "замечания": "; ".join(r["failures"])}
            for r in report["results"]
        ])
        st.dataframe(per_task, hide_index=True, use_container_width=True)


# ── Страница ─────────────────────────────────────────────────────────────────

try:
    settings = Settings.from_env()
except ConfigError as exc:
    st.error(str(exc))
    st.stop()

st.markdown(
    f"""
<div class="pm-hero">
  <span class="pm-tag"><span class="pm-dot"></span>Реальные MCP-серверы · живая модель</span>
  <h1>Один запрос. Пять агентов. Настоящие серверы.</h1>
  <p class="pm-lead">PolyMCP раскладывает запрос на шаги, выбирает нужные инструменты из каталога
  и отвечает только тем, что серверы действительно вернули.</p>
</div>
<div class="pm-panel">
  <div class="pm-panel-head"><span class="pm-eyebrow"><span class="pm-dot"></span>Как это работает</span></div>
  <div class="pm-panel-body"><div class="pm-flow">
    {'<span class="pm-arrow">→</span>'.join(f'<span class="pm-chip">{a}<small>{d}</small></span>' for a, d in AGENTS)}
  </div></div>
</div>
""",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown('<p class="pm-side-title">PolyMCP</p>'
                '<span class="pm-eyebrow"><span class="pm-dot"></span>Router · 5 агентов</span>',
                unsafe_allow_html=True)
    st.write("")
    configs = [p.name for p in sorted(ROOT.glob("mcp_servers*.json"))]
    config = st.selectbox("Конфиг серверов", configs, index=configs.index("mcp_servers.json"))
    mode_label = st.radio("Режим", ["PolyMCP Router", "Baseline (все инструменты)", "Сравнить оба"])
    try:
        info = inspect_servers(config)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Не удалось подключиться к MCP-серверам: {exc}")
        st.stop()
    rows = [f'<div class="pm-server"><span class="pm-dot"></span><b>{esc(n)}</b><span class="n">{c} инстр.</span></div>'
            for n, c in info["servers"].items()]
    rows += [f'<div class="pm-server off" title="{esc(r)}"><span class="pm-dot off"></span><b>{esc(n)}</b>'
             f'<span class="n">недоступен</span></div>' for n, r in info["unavailable"].items()]
    st.markdown('<label class="pm-eyebrow" style="font-size:.76rem">MCP-серверы</label>'
                f'<div class="pm-servers">{"".join(rows)}</div>', unsafe_allow_html=True)
    for name, reason in info["unavailable"].items():
        st.caption(f"{name}: {reason}")
    st.markdown(
        f'<div class="pm-note">Каталог: {info["tool_count"]} инструментов, {fmt_int(info["catalog_tokens"])} токенов схем.'
        f'<br>Модель: <code>{esc(settings.model)}</code></div>',
        unsafe_allow_html=True,
    )

tab_run, tab_bench = st.tabs(["Запрос", "Бенчмарк"])

with tab_run:
    example = st.selectbox("Пример запроса", ["—"] + EXAMPLES)
    query = st.text_area("Ваш запрос", value="" if example == "—" else example, height=100,
                         placeholder="Например: какие баги в статусе in_progress?")
    clicked = st.button("Запустить", type="primary", disabled=not query.strip())
    st.caption("Каждый запуск — реальные вызовы модели и MCP-серверов. Стоимость показывается после ответа.")
    if clicked:
        modes = {"PolyMCP Router": ["polymcp"], "Baseline (все инструменты)": ["baseline"],
                 "Сравнить оба": ["polymcp", "baseline"]}[mode_label]
        columns = st.columns(len(modes), gap="large")
        for column, mode in zip(columns, modes):
            with column:
                section("PolyMCP Router" if mode == "polymcp" else "Baseline")
                with st.status("Выполняю…", expanded=True) as status:
                    trace = run_query(config, query.strip(), mode, status)
                    status.update(label="Готово" if not trace.error else "Ошибка",
                                  state="complete" if not trace.error else "error", expanded=False)
                render_trace(trace, compact=len(modes) > 1)

with tab_bench:
    render_benchmarks()
