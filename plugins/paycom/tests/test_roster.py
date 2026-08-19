from __future__ import annotations

from datetime import datetime, timezone
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
from dispatch_paycom.roster.collector import LANDING_URL, collect_roster
from dispatch_paycom.roster.models import HEADERS, parse_roster_source
from dispatch_paycom.roster.period import period_containing


SOURCE_BYTES = (",".join(HEADERS) + "\r\n" + ",".join(["A001", "Alpha Driver", "A", "00004", "Driver", "STA", "Station", "Driver", "DOT4", "DOT4", "Hourly", "Supervisor", "0", "40", "0", "100", "100"]) + "\r\n").encode()


class Response:
    url = ROSTER_URL
    status = 200
    headers = {"content-type": "text/html"}

    def body(self):
        return SOURCE_BYTES


class Page:
    def __init__(self):
        self.url = ""
        self.gotos: list[str] = []

    def goto(self, url, *, wait_until, timeout):
        self.gotos.append(url)
        self.url = url
        return Response()


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
    assert not [path for path in (tmp_path / "roster-data").rglob("*") if path.is_file() and path.name != ".collector.lock"]


def test_roster_capture_does_not_accept_redirected_page():
    class RedirectPage(Page):
        def goto(self, url, *, wait_until, timeout):
            self.url = "https://www.paycomonline.net/v4/cl/other"
            return Response()

    page = RedirectPage()
    with pytest.raises(Exception, match="roster_navigation_policy_violation"):
        capture_roster_export(page, period=__import__("dispatch_paycom.roster.period", fromlist=["period_containing"]).period_containing("2026-08-05"))


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
