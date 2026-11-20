from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from . import secrets

load_dotenv()
secrets.load_into_env()  # ключи, сохранённые через UI, главнее .env


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    timeout: float
    max_retries: int
    servers_config: str
    reasoning: dict[str, str]  # агент -> reasoning_effort; "*" — для остальных; пусто — не отправлять

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.getenv("AITUNNEL_API_KEY", "").strip()
        if not api_key or api_key.startswith("your_"):
            raise ConfigError(
                "AITUNNEL_API_KEY не задан. Укажите ключ в интерфейсе (streamlit run app_ui.py) "
                "или в файле .env (см. .env.example)."
            )
        return cls(
            api_key=api_key,
            base_url=os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1/").strip(),
            model=os.getenv("POLYMCP_MODEL", "gpt-6-luna").strip(),
            timeout=float(os.getenv("POLYMCP_LLM_TIMEOUT", "60")),
            max_retries=int(os.getenv("POLYMCP_LLM_RETRIES", "2")),
            servers_config=os.getenv("POLYMCP_SERVERS_CONFIG", "mcp_servers.json"),
            reasoning=parse_reasoning(os.getenv("POLYMCP_REASONING", DEFAULT_REASONING)),
        )


# Рассуждения модели — основная статья расхода: у gpt-6-luna вызов с reasoning_effort=none стоит
# ~0,01 ₽ против ~0,04 ₽ по умолчанию. Planner'у оставляем немного рассуждений: от плана зависит всё.
DEFAULT_REASONING = "planner=minimal,*=none"


def parse_reasoning(value: str) -> dict[str, str]:
    """'planner=minimal,*=none' -> {'planner': 'minimal', '*': 'none'}. Пустая строка — не отправлять."""
    result: dict[str, str] = {}
    for part in value.split(","):
        if "=" in part:
            agent, effort = (x.strip() for x in part.split("=", 1))
            if agent and effort:
                result[agent] = effort
    return result
