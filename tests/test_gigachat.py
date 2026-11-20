"""GigaChat-бэкенд на имитации HTTP API (по протоколу официального SDK), без сети."""

import json
from pathlib import Path

import httpx
import pytest

from polymcp.agents.planner import Plan
from polymcp.config import Settings
from polymcp.contracts import AgentUsage
from polymcp.gigachat import GigaChatBackend, simplify_schema, to_gigachat_messages
from polymcp.hub import MCPHub
from polymcp.llm import LLM, LLMError, MeteredLLM
from polymcp.pipeline import Router

ROOT = Path(__file__).resolve().parent.parent
AUTH_URL = "https://auth.test/api/v2/oauth"
BASE_URL = "https://giga.test/api/v1"


def settings(**overrides) -> Settings:
    base = dict(provider="gigachat", api_key="Y2xpZW50OnNlY3JldA==", base_url=BASE_URL, model="GigaChat-2-Pro",
                timeout=10, max_retries=0, servers_config="", reasoning={}, gigachat_auth_url=AUTH_URL)
    base.update(overrides)
    return Settings(**base)


class FakeGigaChat:
    """Имитация: /oauth выдаёт токены, /chat/completions отвечает через сценарий respond(body)."""

    def __init__(self, respond, reject_first_chat: bool = False):
        self.respond = respond
        self.reject_first_chat = reject_first_chat
        self.auth_calls: list[httpx.Request] = []
        self.chat_bodies: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth"):
            self.auth_calls.append(request)
            return httpx.Response(200, json={"access_token": f"tok{len(self.auth_calls)}",
                                             "expires_at": 4_102_444_800_000})
        if request.url.path.endswith("/chat/completions"):
            if self.reject_first_chat and not self.chat_bodies:
                self.chat_bodies.append({})
                return httpx.Response(401, json={"message": "Token has expired"})
            body = json.loads(request.content)
            self.chat_bodies.append(body)
            assert request.headers["Authorization"].startswith("Bearer tok")
            message = self.respond(body)
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", **message}, "index": 0,
                             "finish_reason": "function_call" if "function_call" in message else "stop"}],
                "created": 0, "model": body["model"], "object": "chat.completion",
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            })
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def test_simplify_schema_inlines_refs_and_drops_nullable_anyof():
    schema = simplify_schema(Plan.model_json_schema())
    dumped = json.dumps(schema)
    assert "$ref" not in dumped and "$defs" not in dumped and "title" not in dumped
    step = schema["properties"]["steps"]["items"]
    assert step["type"] == "object" and {"id", "server", "goal", "depends_on"} <= set(step["properties"])
    nullable = simplify_schema({"type": "object", "properties": {"x": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}})
    assert nullable["properties"]["x"] == {"type": "integer"}


def test_messages_conversion_maps_tool_calls_and_results():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "shell__grep_files", "arguments": '{"pattern": "BUG"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "logs/app.log:3: BUG-101"},
    ]
    out = to_gigachat_messages(messages, {"call_1": "state-42"})
    assert out[2] == {"role": "assistant", "content": "", "function_call": {"name": "shell__grep_files",
                      "arguments": {"pattern": "BUG"}}, "functions_state_id": "state-42"}
    assert out[3]["role"] == "function" and out[3]["name"] == "shell__grep_files"
    assert json.loads(out[3]["content"]) == {"result": "logs/app.log:3: BUG-101"}


async def test_chat_returns_openai_style_tool_call_and_reuses_token():
    fake = FakeGigaChat(lambda body: {"content": "", "function_call": {"name": body["functions"][0]["name"],
                                                                        "arguments": {"pattern": "BUG-1"}}})
    backend = GigaChatBackend(settings(), transport=fake.transport)
    tools = [{"type": "function", "function": {"name": "shell__grep_files", "description": "grep",
                                                "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}}}}]
    for _ in range(2):
        completion = await backend.chat("GigaChat-2-Pro", [{"role": "user", "content": "x"}], tools, "auto", {})
    call = completion.message.tool_calls[0]
    assert call.function.name == "shell__grep_files" and json.loads(call.function.arguments) == {"pattern": "BUG-1"}
    assert len(fake.auth_calls) == 1  # токен переиспользуется
    auth = fake.auth_calls[0]
    assert auth.headers["Authorization"] == "Basic Y2xpZW50OnNlY3JldA==" and auth.headers["RqUID"]
    assert b"scope=GIGACHAT_API_PERS" in auth.content
    assert fake.chat_bodies[0]["function_call"] == "auto" and fake.chat_bodies[0]["functions"][0]["name"] == "shell__grep_files"
    assert completion.prompt_tokens == 100


async def test_expired_token_is_refreshed_once():
    fake = FakeGigaChat(lambda body: {"content": "ok"}, reject_first_chat=True)
    backend = GigaChatBackend(settings(), transport=fake.transport)
    completion = await backend.chat("GigaChat-2", [{"role": "user", "content": "x"}], None, None, {})
    assert completion.message.content == "ok" and len(fake.auth_calls) == 2


async def test_required_tool_choice_nudges_once():
    replies = iter([{"content": "Сейчас посмотрю."},
                    {"content": "", "function_call": {"name": "shell__tail_file", "arguments": {"path": "logs/app.log"}}}])
    fake = FakeGigaChat(lambda body: next(replies))
    backend = GigaChatBackend(settings(), transport=fake.transport)
    tools = [{"type": "function", "function": {"name": "shell__tail_file", "parameters": {"type": "object", "properties": {}}}}]
    completion = await backend.chat("GigaChat-2", [{"role": "user", "content": "x"}], tools, "required", {})
    assert completion.message.tool_calls[0].function.name == "shell__tail_file"
    assert fake.chat_bodies[1]["messages"][-1]["role"] == "user"  # повтор с явной просьбой вызвать функцию
    assert completion.prompt_tokens == 200


async def test_structured_uses_forced_function():
    plan = {"reasoning": "r", "steps": [{"id": 1, "server": "shell", "goal": "g", "tool_hint": "grep", "depends_on": []}]}
    fake = FakeGigaChat(lambda body: {"content": "", "function_call": {"name": "submit_result", "arguments": plan}})
    llm = MeteredLLM(_llm(fake), {})
    result = await llm.structured("planner", "sys", "user", Plan)
    assert result.steps[0].server == "shell"
    assert fake.chat_bodies[0]["function_call"] == {"name": "submit_result"}


async def test_structured_invalid_answer_raises_after_retry():
    fake = FakeGigaChat(lambda body: {"content": "не знаю"})
    usage: dict[str, AgentUsage] = {}
    with pytest.raises(LLMError):
        await MeteredLLM(_llm(fake), usage).structured("planner", "sys", "user", Plan)
    assert usage["planner"].calls == 2


def _llm(fake: FakeGigaChat) -> LLM:
    llm = LLM(settings())
    llm.backend = GigaChatBackend(settings(), transport=fake.transport)
    return llm


async def test_full_pipeline_with_scripted_gigachat_and_real_shell_server(tmp_path):
    """Весь конвейер на сценарной «модели» GigaChat и настоящем MCP-сервере shell."""

    def respond(body):
        names = [f["name"] for f in body.get("functions", [])]
        last = body["messages"][-1]
        if names == ["submit_result"]:
            props = body["functions"][0]["parameters"]["properties"]
            if "steps" in props:
                return {"content": "", "function_call": {"name": "submit_result", "arguments": {
                    "reasoning": "один шаг", "steps": [{"id": 1, "server": "shell", "goal": "Найти BUG-107 в логах",
                                                        "tool_hint": "grep files pattern", "depends_on": []}]}}}
            raise AssertionError(f"unexpected schema {props}")
        if last["role"] == "function":
            result = json.loads(last["content"])["result"]
            return {"content": f"BUG-107 упоминается дважды: {result.count('BUG-107')} строки в logs/app.log."}
        assert "shell__grep_files" in names
        return {"content": "", "function_call": {"name": "shell__grep_files",
                                                 "arguments": {"pattern": "BUG-107", "path": "logs"}}}

    fake = FakeGigaChat(respond)
    config = tmp_path / "servers.json"
    config.write_text(json.dumps({"servers": {"shell": {
        "command": "${PYTHON}", "args": [str(ROOT / "servers" / "shell_server.py")],
        "env": {"SHELL_SANDBOX_ROOT": str(ROOT / "sandbox")}}}}))
    async with MCPHub(config) as hub:
        trace = await Router(hub, _llm(fake)).run("Сколько раз BUG-107 встречается в логах?")
    assert not trace.error, trace.error
    assert trace.complete and "2 строки" in trace.answer
    assert trace.tools_called == ["shell.grep_files"]
    assert trace.model.startswith("gigachat:")
    assert set(trace.usage) == {"planner", "synthesizer", "executor"}  # один шаг — без Critic
