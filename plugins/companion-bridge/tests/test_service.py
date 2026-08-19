import sys
import types

from companion_bridge.service import handle, health


def test_health_has_exact_envelope_and_all_planes(monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_CODE_ROOT", "/tmp/not-a-dispatch-checkout")
    result = health()
    assert set(result) == {"ok", "action", "status", "data", "freshness", "delivery", "error"}
    assert set(result["data"]) == {"registration", "runtime_integrity", "query", "data", "freshness", "collector", "authentication", "delivery", "overall"}
    assert result["action"] == "health" and result["status"] in {"ready", "degraded"}


def test_plugin_handler_rejects_unknown_or_extra_input() -> None:
    invalid = handle({"action": "health", "extra": True})
    assert invalid["ok"] is False and set(invalid) == {"ok", "action", "status", "data", "freshness", "delivery", "error"}
    assert handle({"action": "unknown"})["error"]["code"] == "invalid_input"


def test_configured_health_does_not_claim_operational_readiness(monkeypatch, tmp_path) -> None:
    settings = types.SimpleNamespace(
        secrets=types.SimpleNamespace(
            slack_bot_token_present=True,
            slack_app_token_present=True,
        ),
        security_errors=[],
        config_errors=[],
        config=types.SimpleNamespace(
            amazon=types.SimpleNamespace(
                auth_account_alias="default",
                companion_url="https://logistics.amazon.com/dspconsolev2",
                context_endpoint="https://logistics.amazon.com/companion/platform/api/context",
                stream_endpoint="https://logistics.amazon.com/companion/platform/api/conversations/stream",
            ),
            driver_names=types.SimpleNamespace(
                enabled=False,
                id_regex=r"\bA[A-Z0-9]{10,24}\b",
                fallback_to_id=True,
            ),
        ),
        database_path=tmp_path / "threads.sqlite3",
    )
    monkeypatch.setattr("companion_bridge.service.load_settings", lambda require_tokens=False: settings)

    class Authentication:
        def __init__(self, paths): pass
        def status(self, realm, alias): return {"configured": True}

    monkeypatch.setitem(sys.modules, "authentication", types.SimpleNamespace(AuthenticationManager=Authentication))
    monkeypatch.setitem(sys.modules, "paths", types.SimpleNamespace(DispatchPaths=types.SimpleNamespace(from_environment=lambda: object())))

    result = health()
    assert result["ok"] is True
    assert result["status"] == "configured"
    assert result["data"]["authentication"] == "configured"
    assert result["data"]["delivery"] == "configured"
    assert "ready" not in {result["data"]["authentication"], result["data"]["delivery"]}
