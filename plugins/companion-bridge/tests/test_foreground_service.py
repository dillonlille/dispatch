import types

from companion_bridge.config import load_settings
from companion_bridge.slack_app import run


class FakeApp:
    def __init__(self, *, token):
        self.token = token
        self.handlers = {}

    def event(self, name):
        def register(handler):
            self.handlers[name] = handler
            return handler
        return register


class FakeHandler:
    def __init__(self, app, token):
        self.app = app
        self.token = token
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True

    def close(self):
        self.closed = True


class ServiceContext:
    def __init__(self):
        self.browser = object()
        self.authentication = object()
        self.paths = object()

    def should_stop(self):
        return True

    def acquire_browser_manager(self):
        return self.browser

    def acquire_authentication_manager(self):
        return self.authentication


def test_foreground_service_uses_core_context_and_closes_socket_handler(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "slack:\n"
        "  allowed_channels: [C1]\n"
        "  allowed_users: [U1]\n"
        "  admin_channel: C9\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    secret = tmp_path / "slack.env"
    secret.write_text("SLACK_BOT_TOKEN=test-bot\nSLACK_APP_TOKEN=test-app\n", encoding="utf-8")
    secret.chmod(0o600)
    database = tmp_path / "threads.sqlite3"
    settings = load_settings(config, secret, database, require_tokens=True)
    monkeypatch.setattr("companion_bridge.slack_app.load_settings", lambda **kwargs: settings)
    observed_paths = []
    monkeypatch.setattr(
        "companion_bridge.slack_app.plugin_paths",
        lambda paths: observed_paths.append(paths) or types.SimpleNamespace(
            config_file=config,
            secret_file=secret,
            database_file=database,
        ),
    )
    handlers = []

    def handler_factory(app, token):
        handler = FakeHandler(app, token)
        handlers.append(handler)
        return handler

    context = ServiceContext()
    run(context, app_factory=FakeApp, handler_factory=handler_factory)

    assert handlers[0].connected is True
    assert handlers[0].closed is True
    assert observed_paths == [context.paths]
    assert set(handlers[0].app.handlers) == {"app_mention", "message"}
