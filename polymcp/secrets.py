"""Ключи, сохранённые через UI.

Хранятся в .polymcp/secrets.env (каталог можно сменить через POLYMCP_DATA_DIR), файл в .gitignore
и с правами 600. Значения из этого файла главнее, чем .env и переменные окружения: если ключ
поменяли в интерфейсе, работает новый.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
MANAGED_KEYS = (
    "POLYMCP_PROVIDER", "POLYMCP_MODEL",
    "AITUNNEL_API_KEY", "AITUNNEL_BASE_URL",
    "GIGACHAT_CREDENTIALS", "GIGACHAT_SCOPE",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
)


def secrets_file() -> Path:
    return Path(os.getenv("POLYMCP_DATA_DIR", ROOT / ".polymcp")) / "secrets.env"


def load_into_env() -> None:
    path = secrets_file()
    if not path.exists():
        return
    for key, value in dotenv_values(path).items():
        if key in MANAGED_KEYS and value:
            os.environ[key] = value


def save(updates: dict[str, str]) -> Path:
    """Объединяет с уже сохранёнными значениями и атомарно записывает файл (права 600)."""
    path = secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = {k: v for k, v in dotenv_values(path).items() if v} if path.exists() else {}
    current.update({k: v.strip() for k, v in updates.items() if k in MANAGED_KEYS and v and v.strip()})
    lines = [f'{key}="{_quote(value)}"' for key, value in sorted(current.items())]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".secrets-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("# Сохранено из интерфейса PolyMCP. Не коммитьте этот файл.\n" + "\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    for key, value in current.items():
        os.environ[key] = value
    return path


def forget(key: str) -> None:
    path = secrets_file()
    if path.exists():
        current = {k: v for k, v in dotenv_values(path).items() if v and k != key}
        path.unlink()
        if current:
            save(current)
    os.environ.pop(key, None)


def mask(value: str | None) -> str:
    if not value:
        return "не задан"
    return "•••• " + value[-4:] if len(value) > 8 else "••••"


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def check_aitunnel(api_key: str, base_url: str, model: str) -> tuple[bool, str]:
    """Проверяет ключ запросом списка моделей и наличие выбранной модели."""
    try:
        response = httpx.get(
            base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=15
        )
    except httpx.HTTPError as exc:
        return False, f"Не удалось подключиться к {base_url}: {exc}"
    if response.status_code in (401, 403):
        return False, "Ключ не подошёл: сервер ответил 401/403."
    if response.status_code != 200:
        return False, f"Сервер ответил {response.status_code}: {response.text[:200]}"
    models = {m.get("id") for m in response.json().get("data", [])}
    if models and model not in models:
        return False, f"Ключ рабочий, но модели {model!r} нет в списке провайдера."
    return True, f"Ключ рабочий, модель {model} доступна."


def check_github(token: str) -> tuple[bool, str]:
    try:
        response = httpx.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        return False, f"Не удалось подключиться к GitHub: {exc}"
    if response.status_code == 200:
        return True, f"Токен рабочий, аккаунт {response.json().get('login', '?')}."
    if response.status_code in (401, 403):
        return False, "Токен не подошёл: GitHub ответил 401/403."
    return False, f"GitHub ответил {response.status_code}."


def check_gigachat(credentials: str, scope: str, model: str) -> tuple[bool, str]:
    """Получает токен по ключу авторизации и проверяет, что модель есть в списке."""
    import asyncio

    from .config import GIGACHAT_AUTH_URL, GIGACHAT_BASE_URL, Settings
    from .gigachat import list_models
    from .llm import LLMError

    ca_bundle = os.getenv("GIGACHAT_CA_BUNDLE", "").strip()
    settings = Settings(
        provider="gigachat", api_key=credentials.strip(),
        base_url=os.getenv("GIGACHAT_BASE_URL", "").strip() or GIGACHAT_BASE_URL, model=model, timeout=20, max_retries=0,
        servers_config="", reasoning={}, gigachat_scope=scope,
        gigachat_auth_url=os.getenv("GIGACHAT_AUTH_URL", "").strip() or GIGACHAT_AUTH_URL,
        tls_verify=ca_bundle or os.getenv("GIGACHAT_VERIFY_SSL", "true").lower() != "false",
    )
    try:
        models = asyncio.run(list_models(settings))
    except LLMError as exc:
        return False, str(exc)
    if models and model not in models:
        return False, f"Ключ рабочий, но модели {model!r} нет. Доступны: {', '.join(models)}."
    return True, f"Ключ GigaChat рабочий, модель {model} доступна."
