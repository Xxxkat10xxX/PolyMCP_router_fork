"""Подключение к MCP-серверам по stdio и единый реестр их инструментов."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .contracts import ToolSpec

log = logging.getLogger(__name__)

_VAR = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


class MissingVariable(KeyError):
    pass


def expand(value: str) -> str:
    """${VAR} и ${VAR:-default}; ${PYTHON} — текущий интерпретатор."""

    def repl(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name == "PYTHON":
            return sys.executable
        env = os.environ.get(name)
        if env:
            return env
        if default is not None:
            return default
        raise MissingVariable(name)

    return _VAR.sub(repl, value)


@dataclass
class ServerConfig:
    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    description: str = ""
    optional: bool = False


@dataclass
class Policy:
    allow_write_tools: bool = False
    deny_tools: list[str] = field(default_factory=list)


@dataclass
class ToolResult:
    ok: bool
    text: str
    latency_ms: int


def load_config(path: str | Path) -> tuple[list[ServerConfig], Policy, Path]:
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = [
        ServerConfig(
            name=name,
            command=spec["command"],
            args=list(spec.get("args", [])),
            env=dict(spec.get("env", {})),
            description=spec.get("description", ""),
            optional=bool(spec.get("optional", False)),
        )
        for name, spec in data["servers"].items()
    ]
    policy = Policy(**data.get("policy", {}))
    return servers, policy, path.parent


class MCPHub:
    """Держит открытые сессии ко всем серверам. Использовать как async context manager."""

    def __init__(self, config_path: str | Path = "mcp_servers.json", call_timeout: float = 30.0) -> None:
        self.server_configs, self.policy, self.base_dir = load_config(config_path)
        self.call_timeout = call_timeout
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, ToolSpec] = {}  # qualified_name -> spec
        self.descriptions: dict[str, str] = {}
        self.unavailable: dict[str, str] = {}  # сервер -> причина
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "MCPHub":
        await self._stack.__aenter__()
        for cfg in self.server_configs:
            try:
                await asyncio.wait_for(self._connect(cfg), timeout=30)
            except Exception as exc:  # noqa: BLE001 — причина показывается пользователю
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                if isinstance(exc, MissingVariable):
                    reason = f"не задана переменная окружения {exc.args[0]}"
                if not cfg.optional:
                    await self._stack.aclose()
                    raise RuntimeError(f"MCP-сервер {cfg.name!r} не запустился: {reason}") from exc
                self.unavailable[cfg.name] = reason
                log.warning("optional MCP server %s unavailable: %s", cfg.name, reason)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._stack.__aexit__(*exc_info)

    async def _connect(self, cfg: ServerConfig) -> None:
        command = expand(cfg.command)
        env = {**os.environ, **{k: expand(v) for k, v in cfg.env.items()}}
        params = StdioServerParameters(
            command=command,
            args=[expand(a) for a in cfg.args],
            env=env,
            cwd=str(self.base_dir),
        )
        server_stack = AsyncExitStack()
        errlog = server_stack.enter_context(
            tempfile.TemporaryFile("w+", encoding="utf-8", prefix=f"mcp-{cfg.name}-")
        )
        try:
            read, write = await server_stack.enter_async_context(stdio_client(params, errlog=errlog))
            session = await server_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = await self._list_tools(session)
        except BaseException as exc:
            errlog.seek(0)
            stderr_tail = errlog.read()[-800:].strip()
            await server_stack.aclose()
            if stderr_tail and isinstance(exc, Exception):
                raise RuntimeError(f"{exc or type(exc).__name__}; stderr: {stderr_tail}") from exc
            raise
        self._stack.push_async_callback(server_stack.aclose)
        self.sessions[cfg.name] = session
        self.descriptions[cfg.name] = cfg.description
        for tool in tools:
            ann = tool.annotations
            spec = ToolSpec(
                server=cfg.name,
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema or {"type": "object", "properties": {}},
                read_only=getattr(ann, "readOnlyHint", None) if ann else None,
                destructive=getattr(ann, "destructiveHint", None) if ann else None,
            )
            self.tools[spec.qualified_name] = spec

    @staticmethod
    async def _list_tools(session: ClientSession) -> list[Any]:
        tools, cursor = [], None
        while True:
            page = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
            tools.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                return tools

    @property
    def servers(self) -> list[str]:
        return list(self.sessions)

    def tools_of(self, server: str) -> list[ToolSpec]:
        return [t for t in self.tools.values() if t.server == server]

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> ToolResult:
        started = time.perf_counter()
        session = self.sessions.get(server)
        if session is None:
            return ToolResult(False, f"Сервер {server!r} недоступен", 0)
        try:
            result = await asyncio.wait_for(session.call_tool(tool, arguments), timeout=self.call_timeout)
        except asyncio.TimeoutError:
            return ToolResult(False, f"Таймаут {self.call_timeout:.0f} с при вызове {tool}", _ms(started))
        except Exception as exc:  # noqa: BLE001 — ошибка протокола тоже идёт в self-healing
            return ToolResult(False, f"{type(exc).__name__}: {exc}", _ms(started))
        parts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
        if not parts and result.structuredContent is not None:
            parts = [json.dumps(result.structuredContent, ensure_ascii=False)]
        return ToolResult(not result.isError, "\n".join(parts), _ms(started))


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)

