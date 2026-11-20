"""Интеграционные тесты: настоящие MCP-серверы по stdio, без LLM."""

import json
import os
from pathlib import Path

import psycopg
import pytest

from polymcp.hub import MCPHub, MissingVariable, expand

ROOT = Path(__file__).resolve().parent.parent


def test_expand(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    monkeypatch.delenv("NOPE", raising=False)
    assert expand("${FOO}/x") == "bar/x"
    assert expand("${NOPE:-dflt}") == "dflt"
    with pytest.raises(MissingVariable):
        expand("${NOPE}")


def write_config(tmp_path: Path, servers: dict) -> Path:
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"servers": servers}))
    return path


SHELL = {
    "command": "${PYTHON}",
    "args": [str(ROOT / "servers" / "shell_server.py")],
    "env": {"SHELL_SANDBOX_ROOT": str(ROOT / "sandbox")},
}


async def test_shell_server_roundtrip(tmp_path):
    async with MCPHub(write_config(tmp_path, {"shell": SHELL})) as hub:
        assert {t.name for t in hub.tools_of("shell")} >= {"grep_files", "tail_file", "run_command"}
        assert all(t.read_only for t in hub.tools_of("shell"))

        found = await hub.call("shell", "grep_files", {"pattern": "BUG-107", "path": "logs"})
        assert found.ok and found.text.count("BUG-107") == 2

        escaped = await hub.call("shell", "tail_file", {"path": "/etc/passwd"})
        assert not escaped.ok and "outside the sandbox" in escaped.text

        forbidden = await hub.call("shell", "run_command", {"command": "rm -rf logs"})
        assert not forbidden.ok
        sneaky = await hub.call("shell", "run_command", {"command": "grep -f/etc/passwd x"})
        assert not sneaky.ok
        assert (ROOT / "sandbox" / "logs" / "app.log").exists()


async def test_optional_server_failure_is_reported(tmp_path):
    servers = {"shell": SHELL, "ghost": {"command": "/nonexistent/bin", "optional": True}}
    async with MCPHub(write_config(tmp_path, servers)) as hub:
        assert hub.servers == ["shell"]
        assert "ghost" in hub.unavailable


async def test_required_server_failure_raises(tmp_path):
    with pytest.raises(RuntimeError, match="ghost"):
        async with MCPHub(write_config(tmp_path, {"ghost": {"command": "/nonexistent/bin"}})):
            pass


def _db_available() -> bool:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return False
    try:
        psycopg.connect(url, connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


@pytest.mark.skipif(not _db_available(), reason="нужен DATABASE_URL с демо-базой (docker compose up)")
async def test_postgres_server_is_read_only(tmp_path):
    pg = {
        "command": "${PYTHON}",
        "args": [str(ROOT / "servers" / "postgres_server.py")],
        "env": {"DATABASE_URL": "${DATABASE_URL}"},
    }
    async with MCPHub(write_config(tmp_path, {"postgres": pg})) as hub:
        rows = await hub.call("postgres", "run_select", {"sql": "SELECT count(*) FROM bugs WHERE status = 'open'"})
        assert rows.ok and json.loads(rows.text)["rows"] == [[7]]

        drop = await hub.call("postgres", "run_select", {"sql": "DROP TABLE users"})
        assert not drop.ok and "read-only" in drop.text

        multi = await hub.call("postgres", "run_select", {"sql": "SELECT 1; DELETE FROM users"})
        assert not multi.ok

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 60
