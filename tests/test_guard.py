from polymcp.agents.guard import check
from polymcp.contracts import ToolCall, ToolSpec
from polymcp.hub import Policy

PG = ToolSpec(server="postgres", name="run_select", read_only=True)
SHELL_CMD = ToolSpec(server="shell", name="run_command", read_only=True)
SHELL_GREP = ToolSpec(server="shell", name="grep_files", read_only=True)
POLICY = Policy()


def call(tool: ToolSpec, **args) -> ToolCall:
    return ToolCall(server=tool.server, tool=tool.name, arguments=args)


def test_select_allowed():
    assert check(call(PG, sql="SELECT id, updated_at FROM bugs WHERE status = 'open'"), PG, POLICY).allowed


def test_sql_write_blocked():
    for sql in ["DROP TABLE users", "delete from bugs", "UPDATE bugs SET status='closed'",
                "WITH x AS (DELETE FROM bugs RETURNING *) SELECT * FROM x"]:
        verdict = check(call(PG, sql=sql), PG, POLICY)
        assert not verdict.allowed, sql


def test_keyword_inside_string_literal_is_not_a_write():
    assert check(call(PG, sql="SELECT * FROM bugs WHERE title = 'drop table in report'"), PG, POLICY).allowed


def test_shell_dangerous_command_blocked():
    for cmd in ["rm -rf logs", "cat logs/app.log > /tmp/x", "ls; reboot", "tail $(whoami)"]:
        assert not check(call(SHELL_CMD, command=cmd), SHELL_CMD, POLICY).allowed, cmd


def test_grep_regex_alternation_allowed():
    assert check(call(SHELL_GREP, pattern="BUG-101|BUG-107", path="logs"), SHELL_GREP, POLICY).allowed


def test_tool_without_read_only_annotation_blocked():
    writer = ToolSpec(server="github", name="create_issue", read_only=None)
    verdict = check(call(writer, title="x"), writer, POLICY)
    assert not verdict.allowed
    assert check(call(writer, title="x"), writer, Policy(allow_write_tools=True)).allowed


def test_deny_list_and_unknown_tool():
    assert not check(call(PG, sql="select 1"), PG, Policy(deny_tools=["run_select"])).allowed
    assert not check(call(PG, sql="select 1"), None, POLICY).allowed
