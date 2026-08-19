from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import health as health_runtime
from collection_manager import CollectionDisposition, CollectionReceipt, CollectorRegistration
from health import resolved
from plugin_runtime import PluginRuntimeError

ROOT = Path(__file__).resolve().parents[3]
ENVELOPE = {"ok", "action", "status", "data", "freshness", "delivery", "error"}


def configure(monkeypatch, tmp_path: Path) -> None:
    for name in (
        "DISPATCH_CONFIG_ROOT",
        "DISPATCH_SECRETS_ROOT",
        "DISPATCH_DATA_ROOT",
        "DISPATCH_STATE_ROOT",
        "DISPATCH_CACHE_ROOT",
        "DISPATCH_LOGS_ROOT",
        "DISPATCH_RUNTIME_ROOT",
        "DISPATCH_BUILD_OUTPUT",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "installed-home"))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(ROOT))


def test_installed_health_and_owner_paths_use_standard_envelopes(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    health = resolved("health")
    paths = resolved("paths", "handbook")

    assert set(health) == ENVELOPE
    assert health["ok"] is True
    assert health["status"] == "setup_incomplete"
    assert health["data"]["installed"] is True
    assert health["data"]["operational"] is True
    assert health["data"]["browser_manager"]["ready"] is False
    assert health["data"]["collection_manager"]["status"] == "no_collectors"
    assert health["data"]["planes"]["collector"] == "ready"
    assert health["error"] is None
    assert set(paths) == ENVELOPE
    owner_data = Path(paths["data"]["paths"]["DISPATCH_HANDBOOK_DATA_ROOT"])
    assert owner_data == tmp_path / "installed-home" / ".dispatch" / "data" / "handbook"
    assert not (tmp_path / "installed-home").exists()


def test_core_only_setup_completion_does_not_require_browser_or_authentication(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    dispatch_home = tmp_path / "installed-home" / ".dispatch"
    dispatch_home.mkdir(mode=0o700, parents=True)
    setup_directory = dispatch_home / "config"
    setup_directory.mkdir(mode=0o700)
    setup = setup_directory / "plugins.json"
    setup.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "product_version": "0.0.1",
                "selected_plugins": [],
                "plugins": [],
                "contains_secrets": False,
            }
        ),
        encoding="utf-8",
    )
    setup.chmod(0o600)

    health = resolved("health")

    assert health["ok"] is True
    assert health["status"] == "ready"
    assert health["data"]["configured"] is True
    assert health["data"]["planes"]["browser"] == "not_applicable"
    assert health["data"]["planes"]["authentication"] == "not_applicable"


def test_authentication_capability_requires_configured_authentication_status(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    home = tmp_path / "installed-home"
    home.mkdir(mode=0o700)
    dispatch_home = home / ".dispatch"
    dispatch_home.mkdir(mode=0o700)
    setup_directory = dispatch_home / "config"
    setup_directory.mkdir(mode=0o700)
    setup = setup_directory / "plugins.json"
    setup.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "product_version": "0.0.1",
                "selected_plugins": ["paycom"],
                "plugins": [{"id": "paycom", "capabilities": ["authentication"]}],
                "contains_secrets": False,
            }
        ),
        encoding="utf-8",
    )
    setup.chmod(0o600)
    monkeypatch.setattr(
        health_runtime,
        "plugin_health",
        lambda selected: {"ready": True, "plugins": {}, "error": None},
    )

    unconfigured = resolved("health")

    assert unconfigured["ok"] is False
    assert unconfigured["data"]["authentication"]["configured"] is False
    assert unconfigured["data"]["planes"]["authentication"] == "unavailable"

    from authentication import AuthenticationManager
    from paths import DispatchPaths

    AuthenticationManager(DispatchPaths.from_environment()).enroll(
        "paycom-client",
        "default",
        {
            "client_code": "client",
            "username": "username",
            "password": "password",
            "security_pin_1": "1",
            "security_pin_2": "2",
            "security_pin_3": "3",
            "security_pin_4": "4",
            "security_pin_5": "5",
        },
    )

    configured = resolved("health")

    assert configured["ok"] is True
    assert configured["data"]["authentication"]["configured"] is True
    assert configured["data"]["planes"]["authentication"] == "ready"


def test_authenticated_collector_requires_its_exact_realm(monkeypatch, tmp_path: Path) -> None:
    _health_setup(monkeypatch, tmp_path)
    setup = tmp_path / "installed-home" / ".dispatch" / "config" / "plugins.json"
    payload = json.loads(setup.read_text(encoding="utf-8"))
    payload["plugins"][0]["capabilities"] = ["collect", "authentication"]
    setup.write_text(json.dumps(payload), encoding="utf-8")
    setup.chmod(0o600)
    registration = CollectorRegistration(
        "paycom-roster",
        "example",
        "1.2.3",
        lambda context: CollectionReceipt(CollectionDisposition.NO_DATA, None, 0, True),
        browser_realm="paycom-client",
        authentication_required=True,
    )
    monkeypatch.setattr(health_runtime, "discover_collector_registrations", lambda: (registration,))

    import authentication as authentication_runtime

    class WrongRealmAuthentication:
        def __init__(self, _paths):
            pass

        def status(self):
            return {
                "configured": True,
                "realms": [
                    {"id": "amazon-operations", "status": "configured"},
                    {"id": "paycom-client", "status": "not_enrolled"},
                ],
            }

    monkeypatch.setattr(authentication_runtime, "AuthenticationManager", WrongRealmAuthentication)
    health = resolved("health")

    assert health["data"]["authentication"]["configured"] is True
    assert health["data"]["planes"]["authentication"] == "unavailable"
    assert health["data"]["operational"] is False


def test_health_fails_closed_on_deeply_nested_plugins_json(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    dispatch_home = tmp_path / "installed-home" / ".dispatch"
    dispatch_home.mkdir(mode=0o700, parents=True)
    setup_directory = dispatch_home / "config"
    setup_directory.mkdir(mode=0o700)
    setup = setup_directory / "plugins.json"
    setup.write_text('{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}", encoding="utf-8")
    setup.chmod(0o600)
    monkeypatch.setattr(
        health_runtime.json,
        "load",
        lambda stream: (_ for _ in ()).throw(RecursionError()),
    )

    health = resolved("health")

    assert health["ok"] is False
    assert health["status"] == "degraded"
    assert health["error"]["code"] == "plugin_config_invalid"
    assert health["data"]["configured"] is False
    assert health["data"]["setup"]["invalid"] is True
    assert health["data"]["planes"]["configuration"] == "invalid"


def test_selected_plugin_must_report_ready_before_setup_is_ready(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    dispatch_home = tmp_path / "installed-home" / ".dispatch"
    dispatch_home.mkdir(mode=0o700, parents=True)
    setup_directory = dispatch_home / "config"
    setup_directory.mkdir(mode=0o700)
    setup = setup_directory / "plugins.json"
    setup.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "product_version": "0.0.1",
                "selected_plugins": ["handbook"],
                "plugins": [{"id": "handbook", "capabilities": []}],
                "contains_secrets": False,
            }
        ),
        encoding="utf-8",
    )
    setup.chmod(0o600)
    monkeypatch.setattr(
        health_runtime,
        "plugin_health",
        lambda selected: {
            "ready": False,
            "plugins": {selected[0]: {"ok": True, "status": "degraded"}},
            "error": None,
        },
    )

    health = resolved("health")

    assert health["ok"] is True
    assert health["status"] == "setup_incomplete"
    assert health["data"]["configured"] is True
    assert health["data"]["ready"] is False
    assert health["data"]["planes"]["query"] == "unavailable"


def test_installed_health_rejects_invalid_private_root(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("DISPATCH_DATA_ROOT", "relative/data")
    health = resolved("health")

    assert set(health) == ENVELOPE
    assert health["ok"] is False
    assert health["status"] == "degraded"
    assert health["error"]["code"] == "invalid_path_configuration"
    assert health["data"]["configured"] is False


def test_health_verify_and_browser_doctor_agree_when_installer_runtime_is_absent(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)

    health = resolved("health")
    verification = resolved("verify")
    doctor = resolved("browser-doctor")

    assert health["ok"] is verification["ok"] is True
    assert doctor["ok"] is False
    assert health["status"] == verification["status"] == "setup_incomplete"
    assert health["error"] is verification["error"] is None
    assert doctor["error"]["code"] == doctor["data"]["browser_manager"]["error_code"]
    assert verification["data"]["application"] == "dispatch-core"
    assert verification["data"]["version"] == "development"


def test_verify_reports_installed_channel_ref_and_commit(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    root = tmp_path / "installed-home" / ".dispatch"
    root.mkdir(mode=0o700, parents=True)
    (root / "installation.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "channel": "stable",
                "ref": "1.2.3",
                "commit": "0123456789abcdef0123456789abcdef01234567",
                "contains_secrets": False,
            }
        )
    )
    verification = resolved("verify")
    assert verification["data"]["version"] == "1.2.3"
    assert verification["data"]["channel"] == "stable"
    assert verification["data"]["commit"] == "0123456789abcdef0123456789abcdef01234567"


def test_verify_fails_closed_on_deeply_nested_installation_record(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    root = tmp_path / "installed-home" / ".dispatch"
    root.mkdir(mode=0o700, parents=True)
    (root / "installation.json").write_text(
        '{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}",
        encoding="utf-8",
    )

    verification = resolved("verify")

    assert verification["ok"] is False
    assert verification["status"] == "degraded"
    assert verification["error"]["code"] == "installation_record_invalid"


def test_health_fails_closed_on_unsupported_collection_schema(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    current = tmp_path / "installed-home"
    current.mkdir(mode=0o700)
    for part in (".dispatch", "data", "db", "collection-manager"):
        current /= part
        current.mkdir(mode=0o700)
    database = current / "collection-manager.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    connection.execute("INSERT INTO metadata VALUES('schema_version','999')")
    connection.commit()
    connection.close()
    database.chmod(0o600)

    health = resolved("health")

    assert health["data"]["planes"]["collector"] == "unavailable"
    assert health["data"]["collection_manager"]["durable_queue"]["status"] == "unavailable"
    assert health["error"]["code"] == "unsupported_collection_schema"


def _health_setup(monkeypatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    dispatch_home = tmp_path / "installed-home" / ".dispatch"
    dispatch_home.mkdir(mode=0o700, parents=True)
    setup_directory = dispatch_home / "config"
    setup_directory.mkdir(mode=0o700)
    setup = setup_directory / "plugins.json"
    setup.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "product_version": "0.0.1",
                "selected_plugins": ["example"],
                "plugins": [{"id": "example", "capabilities": ["collect"]}],
                "contains_secrets": False,
            }
        ),
        encoding="utf-8",
    )
    setup.chmod(0o600)
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")
    monkeypatch.setattr(
        health_runtime,
        "plugin_health",
        lambda selected: {"ready": True, "plugins": {}, "error": None},
    )


def test_health_discovers_and_reports_selected_collector_registrations(monkeypatch, tmp_path: Path) -> None:
    _health_setup(monkeypatch, tmp_path)
    registration = CollectorRegistration(
        "example-collector",
        "example",
        "1.2.3",
        lambda context: CollectionReceipt(CollectionDisposition.NO_DATA, None, 0, True),
    )
    monkeypatch.setattr(health_runtime, "discover_collector_registrations", lambda: (registration,))

    health = resolved("health")

    assert health["ok"] is True
    assert health["data"]["planes"]["collector"] == "ready"
    assert health["data"]["collection_manager"]["registered"] == 1


def test_browser_backed_collector_requires_browser_readiness(monkeypatch, tmp_path: Path) -> None:
    _health_setup(monkeypatch, tmp_path)
    registration = CollectorRegistration(
        "example-collector",
        "example",
        "1.2.3",
        lambda context: CollectionReceipt(CollectionDisposition.NO_DATA, None, 0, True),
        browser_realm="paycom-client",
    )
    monkeypatch.setattr(health_runtime, "discover_collector_registrations", lambda: (registration,))

    health = resolved("health")

    assert health["ok"] is False
    assert health["data"]["operational"] is False
    assert health["data"]["planes"]["browser"] == "unavailable"


def test_health_fails_collector_plane_on_invalid_collector_provider(monkeypatch, tmp_path: Path) -> None:
    _health_setup(monkeypatch, tmp_path)

    def invalid_provider():
        raise PluginRuntimeError("plugin_collector_registration_invalid", "stable provider error")

    monkeypatch.setattr(health_runtime, "discover_collector_registrations", invalid_provider)

    health = resolved("health")

    assert health["ok"] is False
    assert health["data"]["operational"] is False
    assert health["data"]["planes"]["collector"] == "unavailable"
    assert health["error"] == {
        "code": "plugin_collector_registration_invalid",
        "message": "stable provider error",
    }
