from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "plugins" / "paycom" / "src"
CORE = ROOT / "dispatch-core"
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(SOURCE))

from dispatch_paycom.roster.browser import ROSTER_API_URL, ROSTER_URL, capture_roster_export
from dispatch_paycom.roster.artifacts import stage_roster_artifact
from dispatch_paycom.roster.collector import LANDING_URL, RosterCollectorError, collect_roster, verify_roster_publication
from dispatch_paycom.roster.models import HEADERS, parse_roster_source
from dispatch_paycom.roster.period import period_containing
from dispatch_paycom.roster.storage import RosterStore
from collection_manager import CollectionRequest, PublicationVerification


SOURCE_BYTES = (",".join(HEADERS) + "\r\n" + ",".join(["A001", "Alpha Driver", "A", "00004", "Driver", "STA", "Station", "Driver", "DOT4", "DOT4", "Hourly", "Supervisor", "0", "40", "0", "100", "100"]) + "\r\n").encode()


class Response:
    url = ROSTER_URL
    status = 200
    headers = {"content-type": "text/html"}

    def body(self):
        return SOURCE_BYTES


class Request:
    method = "POST"
    url = ROSTER_API_URL
    post_data_json = {"startDate": "2026-07-26", "endDate": "2026-08-08", "eeCodes": ["A001"]}


API_SOURCE = json.dumps({
    "eeCodes": ["A001"],
    "employees": [{
        "employeeCode": "A001", "fullName": "Alpha Driver", "eestatus": "A",
        "allocation": {"selections": [
            {"categoryName": "Department", "isDepartment": True, "code": "00004", "description": "Driver"},
            {"categoryName": "Delivery Station Code", "isDepartment": False, "code": "STA", "description": "Station"},
        ]},
        "position": "Driver", "payClassCode": "DOT4", "terminalCode": "DOT4", "payType": "Hourly",
        "primarySupervisor": "Supervisor", "missingPunches": 0,
        "totals": {"totalHours": 40, "otHours": 0},
        "approvalPercentages": {"employee": 100, "supervisor": 100},
    }],
}, separators=(",", ":")).encode()


class ApiResponse:
    url = ROSTER_API_URL
    status = 200
    headers = {"content-type": "application/json"}
    request = Request()

    def body(self):
        return API_SOURCE


class ResponseInfo:
    def __init__(self, response):
        self.value = response

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class Page:
    def __init__(self):
        self.url = ""
        self.gotos: list[str] = []

    def goto(self, url, *, wait_until, timeout):
        self.gotos.append(url)
        self.url = url
        return Response()

    def expect_response(self, predicate, *, timeout):
        response = ApiResponse()
        assert timeout == 60_000 and predicate(response)
        return ResponseInfo(response)


class Session:
    realm = "paycom-client"
    landing_url = LANDING_URL

    def __init__(self, page):
        self.page = page


def context(page, **parameters):
    return SimpleNamespace(session=Session(page), parameters=parameters)


def test_roster_source_is_complete_and_strict():
    parsed = parse_roster_source(SOURCE_BYTES)
    assert parsed["employeeCount"] == 1
    assert parsed["activeDriverCount"] == 1
    with pytest.raises(ValueError):
        parse_roster_source(SOURCE_BYTES.replace(b"Employee Code", b"Wrong Header", 1))


def test_roster_runner_uses_context_page_and_publishes_atomically(tmp_path):
    page = Page()
    receipt = collect_roster(
        context(page, target="2026-08-05"),
        now=datetime(2026, 8, 5, 18, tzinfo=timezone.utc),
        root=tmp_path / "roster-data",
        db_path=tmp_path / "roster.sqlite3",
    )
    assert receipt.disposition.value == "published"
    assert receipt.artifact_count == 1 and receipt.domain_complete is True
    assert page.gotos == [ROSTER_URL]
    assert (tmp_path / "roster.sqlite3").stat().st_mode & 0o077 == 0
    # Published runs retain their staged artifact: collection_runs persists
    # artifact_path/manifest_sha256 referencing it for post-hoc audit.
    retained = [path for path in (tmp_path / "roster-data" / "artifacts").rglob("*") if path.is_file()]
    assert retained and any(path.name == "source.json" for path in retained)
    assert not list((tmp_path / "roster-data").rglob(".staging-*"))


def test_roster_rejects_unknown_parameter_and_non_boolean_replace_before_capture(tmp_path):
    page = Page()
    calls = []

    def capture(*_args, **_kwargs):
        calls.append(True)
        return SOURCE_BYTES

    with pytest.raises(RosterCollectorError, match="unknown_parameter"):
        collect_roster(
            context(page, target="2026-08-05", unexpected="x"),
            now=datetime(2026, 8, 5, 18, tzinfo=timezone.utc),
            root=tmp_path / "roster-data",
            db_path=tmp_path / "roster.sqlite3",
            capture=capture,
        )
    with pytest.raises(RosterCollectorError, match="replace_invalid"):
        collect_roster(
            context(page, target="2026-08-05", replace=1),
            now=datetime(2026, 8, 5, 18, tzinfo=timezone.utc),
            root=tmp_path / "roster-data",
            db_path=tmp_path / "roster.sqlite3",
            capture=capture,
        )
    assert calls == []


def test_roster_projection_rejects_string_replacement(tmp_path):
    database = tmp_path / "roster.sqlite3"
    collect_roster(
        context(Page(), target="2026-08-05"),
        now=datetime(2026, 8, 5, 18, tzinfo=timezone.utc),
        root=tmp_path / "roster-data",
        db_path=database,
    )
    parsed = parse_roster_source(SOURCE_BYTES)
    store = RosterStore(database)
    try:
        store.db.execute("UPDATE employees SET employee_name='Corrupted Driver'")
        store.db.commit()
        assert not store.verify_projection("2026-08-05", parsed, hashlib.sha256(SOURCE_BYTES).hexdigest())
    finally:
        store.close()


def test_roster_publication_verifier_proves_exact_absence(tmp_path, monkeypatch):
    database = tmp_path / "roster.sqlite3"
    monkeypatch.setattr("dispatch_paycom.roster.collector._paths", lambda: (tmp_path / "cache", database))
    request = CollectionRequest("paycom-roster", parameters={"target": "2026-08-06"})
    assert verify_roster_publication(request, None) is PublicationVerification.ABSENT


def test_roster_capture_does_not_accept_redirected_page():
    class RedirectPage(Page):
        def goto(self, url, *, wait_until, timeout):
            self.url = "https://www.paycomonline.net/v4/cl/other"
            return Response()

    page = RedirectPage()
    with pytest.raises(Exception, match="roster_navigation_policy_violation"):
        capture_roster_export(page, period=__import__("dispatch_paycom.roster.period", fromlist=["period_containing"]).period_containing("2026-08-05"))


def test_roster_capture_rejects_unmanaged_page_without_fallback():
    page = type("UnmanagedPage", (), {})()
    with pytest.raises(Exception, match="roster_export_unavailable"):
        capture_roster_export(page, period=period_containing("2026-08-05"))


def test_roster_stage_cleanup_rejects_symlink_swap_without_touching_target(tmp_path, monkeypatch):
    import dispatch_paycom.roster.artifacts as artifacts

    outside = tmp_path / "outside.csv"
    outside.write_bytes(b"do not touch")
    root = tmp_path / "roster-data"

    def race_rename(source, destination, **kwargs):
        stages = list(root.rglob(".staging-*"))
        assert len(stages) == 1
        victim = stages[0] / "source.csv"
        victim.unlink()
        victim.symlink_to(outside)
        raise OSError("deterministic publish race")

    monkeypatch.setattr(artifacts.os, "rename", race_rename)
    with pytest.raises(ValueError, match="artifact_cleanup_failed"):
        stage_roster_artifact(root, "2026-08-05", SOURCE_BYTES, parse_roster_source(SOURCE_BYTES), "2026-08-05T18:00:00+00:00")
    assert outside.read_bytes() == b"do not touch"


def test_roster_api_response_is_bound_to_exact_request_period_and_membership():
    period = period_containing("2026-08-05")
    payload = {
        "eeCodes": ["A001"],
        "employees": [{
            "employeeCode": "A001", "fullName": "Alpha Driver", "eestatus": "A",
            "allocation": {"selections": [
                {"categoryName": "Department", "isDepartment": True, "code": "00004", "description": "Driver"},
                {"categoryName": "Delivery Station Code", "isDepartment": False, "code": "STA", "description": "Station"},
            ]},
            "position": "Driver", "payClassCode": "DOT4", "terminalCode": "DOT4", "payType": "Hourly",
            "primarySupervisor": "Supervisor", "missingPunches": 0,
            "totals": {"totalHours": 40, "otHours": 0},
            "approvalPercentages": {"employee": 100, "supervisor": 100},
        }],
    }
    source = json.dumps(payload, separators=(",", ":")).encode()

    class Request:
        method = "POST"
        url = ROSTER_API_URL
        post_data_json = {"startDate": period.start, "endDate": period.end, "eeCodes": ["A001"]}

    class ApiResponse:
        url = ROSTER_API_URL
        status = 200
        headers = {"content-type": "application/json"}
        request = Request()

        def body(self):
            return source

    class ResponseInfo:
        value = ApiResponse()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class ApiPage(Page):
        def expect_response(self, predicate, *, timeout):
            assert timeout == 60_000 and predicate(ApiResponse())
            return ResponseInfo()

    page = ApiPage()
    assert capture_roster_export(page, period=period) == source
    Request.post_data_json = {"startDate": period.start, "endDate": period.end, "eeCodes": ["B002"]}
    with pytest.raises(Exception, match="roster_response_invalid"):
        capture_roster_export(ApiPage(), period=period)
