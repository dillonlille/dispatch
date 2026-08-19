from pathlib import Path

from companion_bridge.config import BridgeConfig, load_settings, parse_secret_file


def test_config_validation_and_secret_presence(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text("slack:\n  allowed_channels: [C1]\n  allowed_users: [U1]\n", encoding="utf-8")
    config.chmod(0o600)
    settings = load_settings(config_path=config, require_tokens=True)
    assert settings.config.slack.allowed_channels == ["C1"]
    assert settings.missing_required_secrets == ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"]
    assert "Dispatch private paths" not in str(settings.config_errors)


def test_secret_file_values_are_excluded_from_model_serialization(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env"
    bot, app = "synthetic-bot-secret", "synthetic-app-secret"
    path.write_text(f"SLACK_BOT_TOKEN={bot}\nSLACK_APP_TOKEN={app}\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    values = parse_secret_file(path)
    settings = load_settings(config_path=tmp_path / "missing.yaml", secret_path=path, database_path=tmp_path / "db.sqlite3")
    assert values["SLACK_BOT_TOKEN"] == bot
    assert settings.missing_required_secrets == []
    assert bot not in settings.model_dump_json() and app not in settings.model_dump_json()


def test_defaults_contain_only_installed_authorities() -> None:
    config = BridgeConfig()
    assert config.amazon.context_endpoint == "https://logistics.amazon.com/companion/platform/api/context"
    assert config.amazon.stream_endpoint.endswith("/conversations/stream")
    assert not hasattr(config.amazon, "browser_profile")


def test_malformed_or_symlinked_secret_file_degrades_safely(tmp_path) -> None:
    malformed = tmp_path / "malformed.env"
    malformed.write_bytes(b"\xff\xfe")
    malformed.chmod(0o600)
    settings = load_settings(
        config_path=tmp_path / "missing.yaml",
        secret_path=malformed,
        database_path=tmp_path / "db.sqlite3",
    )
    assert settings.slack_bot_token is None
    assert any("decoded" in error for error in settings.config_errors)

    target = tmp_path / "target.env"
    target.write_text("SLACK_BOT_TOKEN=not-read\n", encoding="utf-8")
    target.chmod(0o600)
    linked = tmp_path / "linked.env"
    linked.symlink_to(target)
    linked_settings = load_settings(
        config_path=tmp_path / "missing.yaml",
        secret_path=linked,
        database_path=tmp_path / "db.sqlite3",
    )
    assert linked_settings.slack_bot_token is None
    assert any("unsafe" in error for error in linked_settings.config_errors)
