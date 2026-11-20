"""MCP-сервер для диагностики ОС в песочнице.

Все пути разрешаются только внутри SHELL_SANDBOX_ROOT. run_command запускает только
команды из белого списка, без shell (никаких пайпов, перенаправлений и подстановок).

Запуск: SHELL_SANDBOX_ROOT=./sandbox python servers/shell_server.py
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from collections import deque
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

# Команды, которые ничего не меняют. Аргументы-пути проверяются отдельно.
ALLOWED_COMMANDS = {"ls", "cat", "head", "tail", "wc", "grep", "df", "du", "uptime", "ps", "date", "free", "uname"}
MAX_OUTPUT_CHARS = 20_000

mcp = FastMCP("shell", log_level="WARNING")


def _root() -> Path:
    return Path(os.environ.get("SHELL_SANDBOX_ROOT", "sandbox")).resolve()


def _safe_path(path: str) -> Path:
    root = _root()
    candidate = (root / path.lstrip("/")).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if candidate != root and root not in candidate.parents:
        raise PermissionError(f"Path {path!r} is outside the sandbox")
    if not candidate.exists():
        raise FileNotFoundError(f"{path!r} does not exist in the sandbox")
    return candidate


def _rel(path: Path) -> str:
    return str(path.relative_to(_root())) or "."


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... [truncated {len(text) - MAX_OUTPUT_CHARS} chars]"


@mcp.tool(annotations=READ_ONLY)
def list_dir(path: str = ".") -> str:
    """List files and directories (with sizes in bytes) at a path inside the sandbox."""
    target = _safe_path(path)
    if not target.is_dir():
        raise NotADirectoryError(f"{path!r} is not a directory")
    entries = [
        {"name": p.name, "type": "dir" if p.is_dir() else "file", "bytes": p.stat().st_size}
        for p in sorted(target.iterdir())
    ]
    return json.dumps({"path": _rel(target), "entries": entries}, ensure_ascii=False)


@mcp.tool(annotations=READ_ONLY)
def tail_file(path: str, lines: int = 50) -> str:
    """Return the last N lines of a text file (e.g. a log in logs/)."""
    target = _safe_path(path)
    lines = max(1, min(int(lines), 1000))
    with target.open(encoding="utf-8", errors="replace") as fh:
        return _clip("".join(deque(fh, maxlen=lines)))


@mcp.tool(annotations=READ_ONLY)
def grep_files(pattern: str, path: str = "logs", ignore_case: bool = True, max_matches: int = 100) -> str:
    """Search files under a path for a regular expression; returns file:line: text matches."""
    target = _safe_path(path)
    regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    files = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
    matches: list[str] = []
    for file in files:
        with file.open(encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, start=1):
                if regex.search(line):
                    matches.append(f"{_rel(file)}:{lineno}: {line.rstrip()}")
                    if len(matches) >= max_matches:
                        return "\n".join(matches) + f"\n... [stopped at {max_matches} matches]"
    return "\n".join(matches) if matches else f"No matches for /{pattern}/ in {_rel(target)}"


@mcp.tool(annotations=READ_ONLY)
def disk_usage() -> str:
    """Report filesystem capacity and the size of each top-level sandbox directory."""
    root = _root()
    total, used, free = shutil.disk_usage(root)
    dirs = {
        p.name: sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        for p in sorted(root.iterdir())
        if p.is_dir()
    }
    return json.dumps(
        {"filesystem": {"total_bytes": total, "used_bytes": used, "free_bytes": free,
                        "used_pct": round(used / total * 100, 1)},
         "sandbox_dirs_bytes": dirs},
        ensure_ascii=False,
    )


@mcp.tool(annotations=READ_ONLY)
def process_list(top: int = 10) -> str:
    """List the top processes by CPU usage (pid, user, cpu%, mem%, command)."""
    top = max(1, min(int(top), 50))
    out = subprocess.run(
        ["ps", "-eo", "pid,user,pcpu,pmem,comm", "--sort=-pcpu"],
        capture_output=True, text=True, timeout=5, check=True,
    ).stdout.splitlines()
    return "\n".join(out[: top + 1])


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def run_command(command: str) -> str:
    """Run one allow-listed read-only command (ls, cat, head, tail, wc, grep, df, du, uptime, ps,
    date, free, uname) with cwd = sandbox. No pipes, redirects or shell syntax."""
    if re.search(r"[|;&><`$]", command):
        raise PermissionError("Shell operators are not allowed")
    argv = shlex.split(command)
    if not argv or argv[0] not in ALLOWED_COMMANDS:
        raise PermissionError(f"Command not allowed. Allowed: {sorted(ALLOWED_COMMANDS)}")
    for arg in argv[1:]:
        if arg.startswith("-"):
            if "/" in arg or ".." in arg:
                raise PermissionError(f"Paths inside options are not allowed: {arg!r}")
        elif "/" in arg or ".." in arg:
            _safe_path(arg)
    result = subprocess.run(argv, cwd=_root(), capture_output=True, text=True, timeout=10)
    output = result.stdout + (f"\n[stderr]\n{result.stderr}" if result.stderr else "")
    if result.returncode not in (0, 1):  # grep returns 1 when nothing matched
        raise RuntimeError(f"exit code {result.returncode}: {output.strip()[:2000]}")
    return _clip(output) or "(no output)"


if __name__ == "__main__":
    mcp.run()
