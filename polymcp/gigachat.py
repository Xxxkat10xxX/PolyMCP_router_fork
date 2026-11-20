"""Бэкенд GigaChat (Сбер) поверх REST API v1.

Протокол (по официальному SDK gigachat 0.2.x):
- токен: POST {auth_url} с заголовками Authorization: Basic <ключ авторизации>, RqUID: <uuid>,
  телом scope=GIGACHAT_API_PERS; ответ {access_token, expires_at (мс)}; токен живёт 30 минут;
- чат: POST {base_url}/chat/completions, Authorization: Bearer <токен>;
  инструменты передаются как functions=[{name, description, parameters}],
  function_call: "auto" | "none" | {"name": ...};
  ответ модели — message.function_call = {name, arguments: <объект>};
  результат функции — сообщение {role: "function", name, content: <JSON-строка>}.

Отличия от OpenAI, которые прячет адаптер:
- нет режима «обязательно вызвать какой-нибудь инструмент» — просим повторно, если модель ответила текстом;
- structured output делаем принудительным вызовом функции-ответа со схемой pydantic-модели;
- JSON Schema упрощаем: раскрываем $ref/$defs, убираем anyOf с null и служебные поля.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings
from .llm import Completion, LLMError

T = TypeVar("T", bound=BaseModel)

TOKEN_REFRESH_MARGIN_MS = 60_000
KEEP_SCHEMA_KEYS = {"type", "description", "properties", "required", "items", "enum"}
RESPONSE_FUNCTION = "submit_result"
NUDGE = "Вызови одну из доступных функций, чтобы выполнить шаг. Не отвечай текстом."


class GigaChatAuthError(LLMError):
    pass


def tls_hint(exc: Exception) -> str:
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
        return (
            " — не удалось проверить TLS-сертификат GigaChat. Установите корневой сертификат НУЦ Минцифры "
            "(gosuslugi.ru/crt) и укажите путь к нему в GIGACHAT_CA_BUNDLE."
        )
    return ""


# ── JSON Schema ──────────────────────────────────────────────────────────────

def simplify_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Схема в том подмножестве JSON Schema, которое GigaChat гарантированно принимает."""
    defs = schema.get("$defs") or schema.get("definitions") or {}

    def resolve(node: Any, depth: int = 0) -> Any:
        if not isinstance(node, dict) or depth > 20:
            return node
        if "$ref" in node:
            target = defs.get(node["$ref"].split("/")[-1], {})
            merged = {**deepcopy(target), **{k: v for k, v in node.items() if k != "$ref"}}
            return resolve(merged, depth + 1)
        for key in ("anyOf", "oneOf"):
            if key in node:
                options = [o for o in node[key] if not (isinstance(o, dict) and o.get("type") == "null")]
                base = {k: v for k, v in node.items() if k != key}
                return resolve({**(options[0] if options else {"type": "string"}), **base}, depth + 1)
        if "allOf" in node:
            merged = {k: v for k, v in node.items() if k != "allOf"}
            for part in node["allOf"]:
                merged = {**resolve(part, depth + 1), **merged}
            return resolve(merged, depth + 1)
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key not in KEEP_SCHEMA_KEYS:
                continue
            if key == "type" and isinstance(value, list):
                value = next((t for t in value if t != "null"), "string")
            elif key == "properties" and isinstance(value, dict):
                value = {name: resolve(sub, depth + 1) for name, sub in value.items()}
            elif key == "items":
                value = resolve(value, depth + 1)
            out[key] = value
        if out.get("type") == "array" and "items" not in out:
            out["items"] = {"type": "string"}
        return out

    result = resolve(schema)
    result.setdefault("type", "object")
    result.setdefault("properties", {})
    if result.get("required"):
        result["required"] = [r for r in result["required"] if r in result["properties"]]
    return result


# ── Сообщения ────────────────────────────────────────────────────────────────

def _tool_call(name: str, arguments: dict[str, Any]) -> Any:
    return SimpleNamespace(
        id=f"call_{uuid.uuid4().hex[:12]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    )


def to_gigachat_messages(messages: list[dict[str, Any]], states: dict[str, str]) -> list[dict[str, Any]]:
    """OpenAI-формат → GigaChat. states: id вызова → functions_state_id из ответа модели."""
    names: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg["role"]
        if role == "assistant" and msg.get("tool_calls"):
            call = msg["tool_calls"][0]  # GigaChat возвращает не больше одного вызова за ход
            names[call["id"]] = call["function"]["name"]
            try:
                arguments = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            item: dict[str, Any] = {
                "role": "assistant",
                "content": msg.get("content") or "",
                "function_call": {"name": call["function"]["name"], "arguments": arguments},
            }
            if call["id"] in states:
                item["functions_state_id"] = states[call["id"]]
            out.append(item)
        elif role == "tool":
            name = names.get(msg.get("tool_call_id", ""), "function")
            out.append({"role": "function", "name": name,
                        "content": json.dumps({"result": msg.get("content", "")}, ensure_ascii=False)})
        else:
            out.append({"role": role, "content": msg.get("content") or ""})
    return out


def to_gigachat_functions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    functions = []
    for tool in tools:
        fn = tool["function"]
        functions.append({
            "name": fn["name"],
            "description": (fn.get("description") or "")[:1000],
            "parameters": simplify_schema(fn.get("parameters") or {}),
        })
    return functions


# ── Бэкенд ───────────────────────────────────────────────────────────────────

class GigaChatBackend:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._transport = transport
        self._token: str | None = None
        self._expires_at_ms = 0
        self._lock = asyncio.Lock()
        self._states: dict[str, str] = {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.settings.timeout, verify=self.settings.tls_verify,
                                 transport=self._transport)

    async def _access_token(self, force: bool = False) -> str:
        async with self._lock:
            now_ms = int(time.time() * 1000)
            if not force and self._token and self._expires_at_ms - now_ms > TOKEN_REFRESH_MARGIN_MS:
                return self._token
            self._token, self._expires_at_ms = await fetch_token(
                self.settings.api_key, self.settings.gigachat_scope, self.settings.gigachat_auth_url,
                self.settings.tls_verify, self.settings.timeout, self._transport,
            )
            return self._token

    async def _post_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        for attempt in range(2):
            token = await self._access_token(force=attempt > 0)
            try:
                async with self._client() as client:
                    response = await client.post(url, json=body, headers={
                        "Authorization": f"Bearer {token}", "Accept": "application/json",
                    })
            except httpx.HTTPError as exc:
                raise LLMError(f"GigaChat недоступен: {type(exc).__name__}: {exc}{tls_hint(exc)}") from exc
            if response.status_code == 401 and attempt == 0:
                continue  # токен истёк раньше срока — получаем новый и повторяем
            if response.status_code != 200:
                raise LLMError(f"GigaChat ответил {response.status_code}: {response.text[:300]}")
            return response.json()
        raise LLMError("GigaChat отклонил токен доступа (401)")

    def _completion(self, data: dict[str, Any]) -> tuple[Completion, dict[str, Any]]:
        message = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        tool_calls = None
        if message.get("function_call"):
            fc = message["function_call"]
            arguments = fc.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            call = _tool_call(fc["name"], arguments)
            if message.get("functions_state_id"):
                self._states[call.id] = message["functions_state_id"]
            tool_calls = [call]
        normalized = SimpleNamespace(content=message.get("content") or "", tool_calls=tool_calls)
        completion = Completion(
            normalized,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
        return completion, message

    async def chat(self, model: str, messages: list[dict], tools: list[dict] | None,
                   tool_choice: str | None, extra: dict[str, Any]) -> Completion:
        body: dict[str, Any] = {"model": model, "messages": to_gigachat_messages(messages, self._states), **extra}
        if tools:
            body["functions"] = to_gigachat_functions(tools)
            body["function_call"] = "none" if tool_choice == "none" else "auto"
        completion, _ = self._completion(await self._post_chat(body))
        if tools and tool_choice == "required" and not completion.message.tool_calls:
            # В v1 нет режима «вызови любую функцию»: просим ещё раз явно.
            body["messages"] = body["messages"] + [
                {"role": "assistant", "content": completion.message.content or ""},
                {"role": "user", "content": NUDGE},
            ]
            retry, _ = self._completion(await self._post_chat(body))
            retry.prompt_tokens += completion.prompt_tokens
            retry.completion_tokens += completion.completion_tokens
            completion = retry
        return completion

    async def structured(self, model: str, system: str, user: str, schema: type[T],
                         extra: dict[str, Any]) -> tuple[T | None, str, Completion | None]:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "functions": [{
                "name": RESPONSE_FUNCTION,
                "description": "Верни результат строго по схеме.",
                "parameters": simplify_schema(schema.model_json_schema()),
            }],
            "function_call": {"name": RESPONSE_FUNCTION},
            **extra,
        }
        completion, message = self._completion(await self._post_chat(body))
        call = message.get("function_call") or {}
        arguments = call.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = None
        if not isinstance(arguments, dict):
            # Иногда модель кладёт JSON в текст ответа.
            try:
                arguments = json.loads(message.get("content") or "")
            except json.JSONDecodeError:
                return None, "GigaChat не вернул аргументы функции-ответа", completion
        try:
            return schema.model_validate(arguments), "", completion
        except ValidationError as exc:
            return None, f"ответ не прошёл проверку схемы: {exc.errors()[:3]}", completion


async def fetch_token(credentials: str, scope: str, auth_url: str, verify: bool | str, timeout: float,
                      transport: httpx.AsyncBaseTransport | None = None) -> tuple[str, int]:
    headers = {
        "Authorization": f"Basic {credentials}",
        "RqUID": str(uuid.uuid4()),
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=verify, transport=transport) as client:
            response = await client.post(auth_url, data={"scope": scope}, headers=headers)
    except httpx.HTTPError as exc:
        raise GigaChatAuthError(
            f"Сервер авторизации GigaChat ({auth_url}) недоступен: {type(exc).__name__}: {exc}{tls_hint(exc)}"
        ) from exc
    if response.status_code in (400, 401, 403):
        raise GigaChatAuthError(
            f"Ключ авторизации GigaChat не подошёл ({response.status_code}). Нужен «Authorization key» "
            "из личного кабинета (base64), а не Client Secret; проверьте и scope."
        )
    if response.status_code != 200:
        raise GigaChatAuthError(f"Сервер авторизации GigaChat ответил {response.status_code}: {response.text[:200]}")
    data = response.json()
    token = data.get("access_token") or data.get("tok")
    expires = int(data.get("expires_at") or data.get("exp") or 0)
    if not token:
        raise GigaChatAuthError("Сервер авторизации GigaChat не вернул access_token")
    if expires and expires < 10**12:  # на всякий случай: секунды → миллисекунды
        expires *= 1000
    return token, expires or int(time.time() * 1000) + 25 * 60 * 1000


async def list_models(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> list[str]:
    token, _ = await fetch_token(settings.api_key, settings.gigachat_scope, settings.gigachat_auth_url,
                                 settings.tls_verify, settings.timeout, transport)
    try:
        async with httpx.AsyncClient(timeout=settings.timeout, verify=settings.tls_verify,
                                     transport=transport) as client:
            response = await client.get(settings.base_url.rstrip("/") + "/models",
                                        headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise LLMError(f"GigaChat недоступен: {type(exc).__name__}: {exc}{tls_hint(exc)}") from exc
    if response.status_code != 200:
        raise LLMError(f"GigaChat ответил {response.status_code}: {response.text[:200]}")
    return [m.get("id") for m in response.json().get("data", []) if m.get("id")]
