from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "dispatch-core"))
sys.path.insert(0, str(ROOT / "plugins" / "paycom" / "src"))

from dispatch_paycom.collector_provider import collector_registrations


def test_provider_exposes_two_picklable_core_registrations():
    registrations = collector_registrations()
    assert [item.collector_id for item in registrations] == ["paycom-roster", "paycom-timecards"]
    assert all(item.browser_realm == "paycom-client" and item.authentication_required for item in registrations)
    assert all(callable(item.publication_verifier) for item in registrations)
    assert [item.execution_timeout_seconds for item in registrations] == [450, 3_600]
    assert all(item.runner.__module__.startswith("dispatch_paycom.") for item in registrations)
