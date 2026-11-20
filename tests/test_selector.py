from polymcp.agents.selector import Selector, prune_schema, tokenize
from polymcp.contracts import PlanStep, ToolSpec


def tool(name: str, description: str, **props) -> ToolSpec:
    return ToolSpec(server="github", name=name, description=description, read_only=True,
                    input_schema={"type": "object", "properties": props, "required": list(props)[:1]})


TOOLS = [
    tool("list_commits", "Get list of commits of a branch in a GitHub repository.", owner={"type": "string"}),
    tool("list_branches", "List branches in a GitHub repository.", owner={"type": "string"}),
    tool("search_issues", "Search for issues in GitHub repositories.", query={"type": "string"}),
    tool("get_file_contents", "Get the contents of a file or directory.", path={"type": "string"}),
    tool("list_workflow_runs", "List workflow runs for a repository workflow.", owner={"type": "string"}),
]


def step(hint: str, goal: str = "") -> PlanStep:
    return PlanStep(id=1, server="github", goal=goal or hint, tool_hint=hint, depends_on=[])


def test_tokenize_splits_snake_and_camel_case():
    assert tokenize("listCommits get_file_contents") == ["list", "commits", "get", "file", "contents"]


def test_bm25_ranks_matching_tool_first():
    selector = Selector(TOOLS, top_k=2)
    assert selector.select(step("list commits branch")).tools[0].name == "list_commits"
    assert selector.select(step("read file contents")).tools[0].name == "get_file_contents"
    assert selector.select(step("search open issues")).tools[0].name == "search_issues"


def test_selection_respects_server_and_top_k():
    selector = Selector(TOOLS, top_k=3)
    assert len(selector.select(step("anything")).tools) == 3
    other = PlanStep(id=1, server="postgres", goal="x", tool_hint="x", depends_on=[])
    assert selector.select(other).tools == []


def test_prune_schema_keeps_contract_drops_noise():
    schema = {
        "type": "object",
        "title": "Args",
        "additionalProperties": False,
        "required": ["state"],
        "properties": {
            "state": {"type": "string", "enum": ["open", "closed"], "default": "open",
                      "description": "Issue state. Long explanation that the model does not need at all here."},
            "labels": {"type": "array", "items": {"type": "string", "description": "Label. More text."}},
        },
    }
    pruned = prune_schema(schema)
    assert pruned["required"] == ["state"]
    assert pruned["properties"]["state"]["enum"] == ["open", "closed"]
    assert pruned["properties"]["state"]["description"] == "Issue state."
    assert "default" not in pruned["properties"]["state"]
    assert "title" not in pruned and "additionalProperties" not in pruned
    assert pruned["properties"]["labels"]["items"]["description"] == "Label."


def test_pruned_selection_is_smaller():
    selection = Selector(TOOLS, top_k=5).select(step("list commits"))
    assert 0 < selection.pruned_tokens <= selection.raw_tokens
