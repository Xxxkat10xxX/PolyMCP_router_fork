from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


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

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.getenv("AITUNNEL_API_KEY", "").strip()
        if not api_key or api_key.startswith("your_"):
            raise ConfigError(
                "AITUNNEL_API_KEY не задан. Скопируйте .env.example в .env и укажите ключ aitunnel."
            )
        return cls(
            api_key=api_key,
            base_url=os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1/").strip(),
            model=os.getenv("POLYMCP_MODEL", "gpt-6-luna").strip(),
            timeout=float(os.getenv("POLYMCP_LLM_TIMEOUT", "60")),
            max_retries=int(os.getenv("POLYMCP_LLM_RETRIES", "2")),
            servers_config=os.getenv("POLYMCP_SERVERS_CONFIG", "mcp_servers.json"),
        )
