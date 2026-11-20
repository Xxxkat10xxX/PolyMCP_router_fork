"""Агент 2. Selector + Schema Pruner.

1. Отбор кандидатов: BM25 по name/description/параметрам инструментов выбранного сервера.
2. Сжатие схем: оставляем то, что нужно модели для корректного вызова (типы, enum,
   required, короткие описания), и выкидываем остальное.

Работает без LLM: отбор и сжатие сами по себе не тратят токены модели.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from ..contracts import PlanStep, ToolSpec
from ..tokens import count_tokens

_SPLIT_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[a-zа-яё0-9]+")
_STOP = {"the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "with", "by", "is", "be", "from", "this", "that"}

DROP_KEYS = {"default", "examples", "example", "title", "$schema", "additionalProperties", "$comment", "format"}


def tokenize(text: str) -> list[str]:
    text = _SPLIT_CAMEL.sub(" ", text).replace("_", " ").lower()
    return [w for w in _WORD.findall(text) if w not in _STOP and len(w) > 1]


def tool_document(tool: ToolSpec) -> list[str]:
    params = " ".join(tool.input_schema.get("properties", {}).keys())
    # Имя инструмента весит больше всего, поэтому повторяем его.
    return tokenize(f"{tool.name} {tool.name} {tool.name} {tool.description} {params}")


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75) -> None:
        self.docs = [Counter(d) for d in docs]
        self.lengths = [len(d) for d in docs]
        self.avg = sum(self.lengths) / max(len(docs), 1)
        df = Counter(term for d in docs for term in set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def score(self, query: list[str], i: int) -> float:
        doc, length = self.docs[i], self.lengths[i]
        total = 0.0
        for term in query:
            tf = doc.get(term, 0)
            if tf:
                norm = tf + self.k1 * (1 - self.b + self.b * length / max(self.avg, 1))
                total += self.idf.get(term, 0.0) * tf * (self.k1 + 1) / norm
        return total


def _first_sentence(text: str, limit: int) -> str:
    text = " ".join(text.split())
    match = re.search(r"(?<=[.!?])\s", text)
    if match and match.start() <= limit:
        text = text[: match.start()]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def prune_schema(schema: dict[str, Any], desc_limit: int = 90) -> dict[str, Any]:
    """Рекурсивно сжимает JSON Schema, сохраняя всё, что влияет на валидность вызова."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in DROP_KEYS:
            continue
        if key == "description" and isinstance(value, str):
            short = _first_sentence(value, desc_limit)
            if short:
                out[key] = short
        elif key == "properties" and isinstance(value, dict):
            out[key] = {name: prune_schema(sub, desc_limit) for name, sub in value.items()}
        elif key in ("items", "additionalItems") and isinstance(value, dict):
            out[key] = prune_schema(value, desc_limit)
        elif key in ("anyOf", "oneOf", "allOf") and isinstance(value, list):
            out[key] = [prune_schema(v, desc_limit) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


def openai_tool(tool: ToolSpec, pruned: bool) -> dict[str, Any]:
    """Определение инструмента в формате function calling OpenAI."""
    description = _first_sentence(tool.description, 200) if pruned else tool.description
    parameters = prune_schema(tool.input_schema) if pruned else tool.input_schema
    parameters = parameters or {"type": "object", "properties": {}}
    parameters.setdefault("type", "object")
    parameters.setdefault("properties", {})
    return {
        "type": "function",
        "function": {"name": tool.qualified_name, "description": description, "parameters": parameters},
    }


class Selection:
    def __init__(self, tools: list[ToolSpec], scores: list[float]) -> None:
        self.tools = tools
        self.scores = scores
        self.openai_tools = [openai_tool(t, pruned=True) for t in tools]
        self.raw_tokens = count_tokens([openai_tool(t, pruned=False) for t in tools])
        self.pruned_tokens = count_tokens(self.openai_tools)

    def by_qualified_name(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.qualified_name == name), None)


class Selector:
    def __init__(self, tools: list[ToolSpec], top_k: int = 5) -> None:
        self.top_k = top_k
        self.by_server: dict[str, list[ToolSpec]] = {}
        for tool in tools:
            self.by_server.setdefault(tool.server, []).append(tool)
        self.indexes = {
            server: BM25([tool_document(t) for t in server_tools])
            for server, server_tools in self.by_server.items()
        }

    def select(self, step: PlanStep) -> Selection:
        tools = self.by_server.get(step.server, [])
        if not tools:
            return Selection([], [])
        index = self.indexes[step.server]
        query = tokenize(f"{step.tool_hint} {step.tool_hint} {step.goal}")
        ranked = sorted(
            ((index.score(query, i), i) for i in range(len(tools))),
            key=lambda pair: (-pair[0], pair[1]),
        )[: self.top_k]
        return Selection([tools[i] for _, i in ranked], [round(s, 3) for s, _ in ranked])
