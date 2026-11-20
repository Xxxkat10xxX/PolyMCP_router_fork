"""Read-only MCP-сервер для PostgreSQL.

Каждое соединение открывается с default_transaction_read_only=on и statement_timeout,
поэтому даже если запрос на запись пройдёт мимо Policy Guard, база его отвергнет.

Запуск: DATABASE_URL=postgresql://... python servers/postgres_server.py
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import os
from typing import Any

import psycopg
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
MAX_ROWS_CAP = 500

mcp = FastMCP("postgres", log_level="WARNING")


def _connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    timeout_ms = int(os.environ.get("PG_STATEMENT_TIMEOUT_MS", "5000"))
    return psycopg.connect(
        url,
        autocommit=False,
        connect_timeout=5,
        options=f"-c default_transaction_read_only=on -c statement_timeout={timeout_ms}",
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return value


def _query(sql: str, params: tuple | None = None, max_rows: int = MAX_ROWS_CAP) -> dict[str, Any]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return {"columns": [], "rows": [], "row_count": 0}
        columns = [c.name for c in cur.description]
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        conn.rollback()
    return {
        "columns": columns,
        "rows": [[_jsonable(v) for v in row] for row in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


def _single_statement(sql: str) -> str:
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise ValueError("SQL is empty")
    if ";" in stripped:
        raise ValueError("Only a single SQL statement is allowed")
    return stripped


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


@mcp.tool(annotations=READ_ONLY)
def list_tables(schema: str = "public") -> str:
    """List tables in a schema with approximate row counts."""
    return _dump(_query(
        """
        SELECT c.relname AS table, c.reltuples::bigint AS approx_rows
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind IN ('r', 'p')
        ORDER BY c.relname
        """,
        (schema,),
    ))


@mcp.tool(annotations=READ_ONLY)
def describe_table(table: str, schema: str = "public") -> str:
    """Show columns, types, nullability, defaults and constraints of a table."""
    columns = _query(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    if not columns["rows"]:
        raise ValueError(f"Table {schema}.{table} not found")
    constraints = _query(
        """
        SELECT conname, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = to_regclass(%s)
        """,
        (f"{schema}.{table}",),
    )
    return _dump({"table": f"{schema}.{table}", "columns": columns, "constraints": constraints})


@mcp.tool(annotations=READ_ONLY)
def run_select(sql: str, max_rows: int = 100) -> str:
    """Run one read-only SQL query (SELECT / WITH ... SELECT) and return rows as JSON.

    The session is read-only: INSERT/UPDATE/DELETE/DDL are rejected by the database.
    """
    max_rows = max(1, min(int(max_rows), MAX_ROWS_CAP))
    return _dump(_query(_single_statement(sql), max_rows=max_rows))


@mcp.tool(annotations=READ_ONLY)
def explain_query(sql: str, analyze: bool = False) -> str:
    """Show the PostgreSQL execution plan for a read-only query (EXPLAIN, optionally ANALYZE)."""
    stmt = _single_statement(sql)
    prefix = "EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " if analyze else "EXPLAIN (FORMAT TEXT) "
    result = _query(prefix + stmt)
    return "\n".join(row[0] for row in result["rows"])


@mcp.tool(annotations=READ_ONLY)
def active_locks() -> str:
    """List current locks with the owning backend PID, query and whether the lock is granted."""
    return _dump(_query(
        """
        SELECT l.pid, l.locktype, l.mode, l.granted,
               COALESCE(c.relname, '') AS relation, a.state, left(a.query, 200) AS query
        FROM pg_locks l
        LEFT JOIN pg_class c ON c.oid = l.relation
        LEFT JOIN pg_stat_activity a ON a.pid = l.pid
        WHERE l.pid <> pg_backend_pid()
        ORDER BY l.granted, l.pid
        """
    ))


@mcp.tool(annotations=READ_ONLY)
def table_sizes(schema: str = "public") -> str:
    """Return total, table and index size in bytes for every table in a schema."""
    return _dump(_query(
        """
        SELECT c.relname AS table,
               pg_total_relation_size(c.oid) AS total_bytes,
               pg_relation_size(c.oid) AS table_bytes,
               pg_indexes_size(c.oid) AS index_bytes
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind = 'r'
        ORDER BY pg_total_relation_size(c.oid) DESC
        """,
        (schema,),
    ))


if __name__ == "__main__":
    mcp.run()
