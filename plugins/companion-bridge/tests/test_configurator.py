from pathlib import Path

from companion_bridge.configurator import configure


def test_configurator_status_does_not_prompt() -> None:
    called = []
    result = configure({"action": "status"}, input_fn=lambda prompt: called.append(prompt) or "", secret_fn=lambda prompt: called.append(prompt) or "")
    assert result["ok"] is True and called == []
    assert "token" not in str(result["data"]).lower() or "present" in str(result["data"]).lower()


def test_configurator_writes_private_fixture_files(monkeypatch, tmp_path) -> None:
    from companion_bridge.config import PluginPaths
    import companion_bridge.configurator as module
    paths = PluginPaths(tmp_path / "config" / "config.yaml", tmp_path / "secrets" / "slack.env", tmp_path / "private" / "threads.sqlite3")
    monkeypatch.setattr(module, "dispatch_paths", lambda: paths)
    prompts = iter([
        "Fixture Companion",
        "C" + "1" * 8 + ",C" + "2" * 8,
        "U" + "1" * 8,
        "T" + "1" * 8,
        "C" + "9" * 8,
    ])
    secrets = iter(["xoxb-" + "a" * 12, "xapp-" + "b" * 12])
    result = configure({"action": "configure"}, input_fn=lambda _: next(prompts), secret_fn=lambda _: next(secrets))
    assert result["ok"] is True
    assert paths.config_file.stat().st_mode & 0o777 == 0o600
    assert paths.secret_file.stat().st_mode & 0o777 == 0o600
    secret_text = paths.secret_file.read_text(encoding="utf-8")
    assert "SLACK_BOT_TOKEN=xoxb-" in secret_text
    assert "SLACK_APP_TOKEN=xapp-" in secret_text


def test_configurator_accepts_trusted_core_context(tmp_path) -> None:
    from paths import DispatchPaths

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    core_paths = DispatchPaths.from_environment(
        {"HOME": str(home), "DISPATCH_HOME": str(home / ".dispatch")},
        code_root=Path(__file__).resolve().parents[3] / "dispatch-core",
    )
    prompts = iter([
        "Fixture Companion",
        "C" + "1" * 8,
        "U" + "1" * 8,
        "T" + "1" * 8,
        "C" + "9" * 8,
    ])
    secrets = iter(["xoxb-" + "a" * 12, "xapp-" + "b" * 12])

    class Context:
        paths = core_paths

        @staticmethod
        def prompt(message):
            return next(prompts)

        @staticmethod
        def prompt_secret(message):
            return next(secrets)

    result = configure(Context())
    assert result["ok"] is True
    assert (core_paths.secrets / "companion-bridge" / "slack.env").is_file()


def test_configurator_rejects_json_secret_values() -> None:
    result = configure({"action": "configure", "bot_token": "should-not-be-accepted"})
    assert result["ok"] is False
