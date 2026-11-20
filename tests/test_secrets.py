import os
import stat

from polymcp import secrets


def test_save_merges_sets_env_and_restricts_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYMCP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AITUNNEL_API_KEY", "from-dotenv")

    path = secrets.save({"AITUNNEL_API_KEY": "sk-first-1234", "POLYMCP_MODEL": "gpt-6-luna"})
    secrets.save({"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_token_9999", "AITUNNEL_API_KEY": "  "})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert 'AITUNNEL_API_KEY="sk-first-1234"' in text  # пустое значение не затирает сохранённое
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in text
    assert os.environ["AITUNNEL_API_KEY"] == "sk-first-1234"  # значение из интерфейса главнее .env


def test_load_into_env_ignores_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYMCP_DATA_DIR", str(tmp_path))
    (tmp_path / "secrets.env").write_text('AITUNNEL_API_KEY="sk-x"\nPATH="/evil"\n')
    monkeypatch.setenv("AITUNNEL_API_KEY", "placeholder")
    path_before = os.environ["PATH"]

    secrets.load_into_env()

    assert os.environ["AITUNNEL_API_KEY"] == "sk-x"
    assert os.environ["PATH"] == path_before


def test_forget_removes_key(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYMCP_DATA_DIR", str(tmp_path))
    # регистрируем переменные в monkeypatch, чтобы после теста они вернулись к исходным значениям
    monkeypatch.setenv("AITUNNEL_API_KEY", "placeholder")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "placeholder")
    secrets.save({"AITUNNEL_API_KEY": "sk-a", "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp-b"})
    secrets.forget("GITHUB_PERSONAL_ACCESS_TOKEN")
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in secrets.secrets_file().read_text()
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in os.environ


def test_mask_never_reveals_short_values():
    assert secrets.mask(None) == "не задан"
    assert secrets.mask("short") == "••••"
    assert secrets.mask("sk-aitunnel-abcdef1234") == "•••• 1234"
