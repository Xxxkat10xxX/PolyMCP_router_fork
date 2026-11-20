from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from . import secrets

load_dotenv()
secrets.load_into_env()  # ключи, сохранённые через UI, главнее .env


class ConfigError(RuntimeError):
    pass


PROVIDERS = ("aitunnel", "gigachat")
DEFAULT_MODELS = {"aitunnel": "gpt-6-luna", "gigachat": "GigaChat-2-Pro"}

# Рассуждения модели — основная статья расхода: у gpt-6-luna вызов с reasoning_effort=none стоит
# ~0,01 ₽ против ~0,04 ₽ по умолчанию. Planner'у оставляем немного рассуждений: от плана зависит всё.
# Для GigaChat параметр по умолчанию не отправляется.
DEFAULT_REASONING = {"aitunnel": "planner=minimal,*=none", "gigachat": ""}

GIGACHAT_BASE_URL = "https://api.giga.chat/v1"
GIGACHAT_AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_SCOPES = ("GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP")


@dataclass(frozen=True)
class Settings:
    provider: str
    api_key: str  # aitunnel: API-ключ; gigachat: ключ авторизации (Basic, base64)
    base_url: str
    model: str
    timeout: float
    max_retries: int
    servers_config: str
    reasoning: dict[str, str]  # агент -> reasoning_effort; "*" — для остальных; пусто — не отправлять
    models: dict[str, str] = field(default_factory=dict)  # агент -> модель; "*" — для остальных
    max_concurrency: int = 0  # 0 — без ограничения
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_auth_url: str = GIGACHAT_AUTH_URL
    tls_verify: bool | str = True  # True, False или путь к CA bundle

    def model_for(self, agent: str) -> str:
        return self.models.get(agent) or self.models.get("*") or self.model

    @classmethod
    def from_env(cls) -> "Settings":
        provider = _env("POLYMCP_PROVIDER", "aitunnel").lower()
        if provider not in PROVIDERS:
            raise ConfigError(f"Неизвестный провайдер {provider!r}. Допустимо: {', '.join(PROVIDERS)}.")

        if provider == "gigachat":
            api_key = os.getenv("GIGACHAT_CREDENTIALS", "").strip()
            base_url = _env("GIGACHAT_BASE_URL", GIGACHAT_BASE_URL)
            missing = "GIGACHAT_CREDENTIALS (ключ авторизации GigaChat)"
        else:
            api_key = os.getenv("AITUNNEL_API_KEY", "").strip()
            base_url = _env("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1/")
            missing = "AITUNNEL_API_KEY"
        if not api_key or api_key.startswith("your_"):
            raise ConfigError(
                f"{missing} не задан. Укажите ключ в интерфейсе (streamlit run app_ui.py) "
                "или в файле .env (см. .env.example)."
            )

        ca_bundle = os.getenv("GIGACHAT_CA_BUNDLE", "").strip()
        verify: bool | str = ca_bundle or os.getenv("GIGACHAT_VERIFY_SSL", "true").strip().lower() != "false"
        default_concurrency = "1" if provider == "gigachat" else "0"
        return cls(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=os.getenv("POLYMCP_MODEL", "").strip() or DEFAULT_MODELS[provider],
            timeout=float(_env("POLYMCP_LLM_TIMEOUT", "60")),
            max_retries=int(_env("POLYMCP_LLM_RETRIES", "2")),
            servers_config=_env("POLYMCP_SERVERS_CONFIG", "mcp_servers.json"),
            reasoning=parse_mapping(_env("POLYMCP_REASONING", DEFAULT_REASONING[provider])),
            models=parse_mapping(os.getenv("POLYMCP_MODELS", "")),
            max_concurrency=int(_env("POLYMCP_MAX_CONCURRENCY", default_concurrency)),
            gigachat_scope=_env("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            gigachat_auth_url=_env("GIGACHAT_AUTH_URL", GIGACHAT_AUTH_URL),
            tls_verify=verify,
        )


def _env(name: str, default: str) -> str:
    """Пустое значение (например, «POLYMCP_MODEL=» в .env) считается незаданным."""
    return os.getenv(name, "").strip() or default


def parse_mapping(value: str) -> dict[str, str]:
    """'planner=minimal,*=none' -> {'planner': 'minimal', '*': 'none'}. 'off' или пусто — пустой словарь."""
    result: dict[str, str] = {}
    if value.strip().lower() == "off":
        return result
    for part in value.split(","):
        if "=" in part:
            agent, setting = (x.strip() for x in part.split("=", 1))
            if agent and setting:
                result[agent] = setting
    return result


parse_reasoning = parse_mapping  # прежнее имя
