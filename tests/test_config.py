import pytest

from polymcp.config import ConfigError, Settings


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("POLYMCP_PROVIDER", "POLYMCP_MODEL", "POLYMCP_MODELS", "POLYMCP_REASONING", "POLYMCP_MAX_CONCURRENCY",
                 "AITUNNEL_API_KEY", "GIGACHAT_CREDENTIALS", "GIGACHAT_SCOPE", "GIGACHAT_CA_BUNDLE"):
        monkeypatch.setenv(name, "")  # как пустые строки из скопированного .env.example
    return monkeypatch


def test_empty_values_fall_back_to_defaults(clean_env):
    clean_env.setenv("AITUNNEL_API_KEY", "sk-real")
    s = Settings.from_env()
    assert (s.provider, s.model, s.max_concurrency) == ("aitunnel", "gpt-6-luna", 0)
    assert s.reasoning == {"planner": "minimal", "*": "none"}


def test_gigachat_defaults_and_per_agent_models(clean_env):
    clean_env.setenv("POLYMCP_PROVIDER", "gigachat")
    clean_env.setenv("GIGACHAT_CREDENTIALS", "Y2xpZW50OnNlY3JldA==")
    clean_env.setenv("POLYMCP_MODELS", "planner=GigaChat-2-Max,*=GigaChat-2")
    s = Settings.from_env()
    assert s.model == "GigaChat-2-Pro" and s.max_concurrency == 1 and s.reasoning == {}
    assert s.model_for("planner") == "GigaChat-2-Max" and s.model_for("executor") == "GigaChat-2"
    assert s.gigachat_scope == "GIGACHAT_API_PERS" and s.tls_verify is True


def test_missing_key_and_unknown_provider(clean_env):
    with pytest.raises(ConfigError, match="AITUNNEL_API_KEY"):
        Settings.from_env()
    clean_env.setenv("POLYMCP_PROVIDER", "gigachat")
    with pytest.raises(ConfigError, match="GIGACHAT_CREDENTIALS"):
        Settings.from_env()
    clean_env.setenv("POLYMCP_PROVIDER", "nope")
    with pytest.raises(ConfigError, match="Неизвестный провайдер"):
        Settings.from_env()


def test_reasoning_off(clean_env):
    clean_env.setenv("AITUNNEL_API_KEY", "sk-real")
    clean_env.setenv("POLYMCP_REASONING", "off")
    assert Settings.from_env().reasoning == {}
