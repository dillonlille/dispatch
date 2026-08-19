from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "plugins" / "paycom" / "src"
CORE = ROOT / "dispatch-core"
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(SOURCE))

from dispatch_paycom.roster.browser import ROSTER_URL
from dispatch_paycom.roster.collector import LANDING_URL, collect_roster
from dispatch_paycom.roster.models import HEADERS
from dispatch_paycom.timecards.browser import TimecardCapture
from dispatch_paycom.timecards.collector import collect_timecards
from dispatch_paycom.timecards.period import build_timecard_url, period_from_end


ROSTER_BYTES = (",".join(HEADERS) + "\r\n" + ",".join(["A001", "Alpha Driver", "A", "00004", "Driver", "STA", "Station", "Driver", "DOT4", "DOT4", "Hourly", "Supervisor", "0", "40", "0", "100", "100"]) + "\r\n").encode()


class RosterResponse:
    url = ROSTER_URL
    status = 200
    headers = {"content-type": "text/html"}

    def body(self):
        return ROSTER_BYTES


class Page:
    def __init__(self):
        self.url = ""
        self.gotos: list[str] = []

    def goto(self, url, *, wait_until, timeout):
        self.url = url
        self.gotos.append(url)
        return RosterResponse() if "timecardsearch" in url else None

    def wait_for_load_state(self, state, *, timeout):
        return None

    def evaluate(self, expression):
        period = period_from_end("2026-08-22")
        record = {
            "version": 1,
            "sourceFormat": "paycom-timecard-dom.v1",
            "employeeCode": "A001",
            "periodStart": period.start,
            "periodEnd": period.end,
            "periodKey": period.key,
            "sourceUrl": self.url,
            "pageTitle": "Timecard Editor",
            "headers": ["date", "paycode", "i1", "allocation1", "o1", "allocation2", "o2", "hours", "total_hours", "amount", "exception-points", "waiver", "comment", "missing-punch", "delete"],
            "days": [
                {"date": value, "label": ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")[index % 7], "payCode": "", "allocation1": "", "allocation2": "", "hours": 0, "totalHours": 0, "dollars": 0, "exceptionText": "", "waiverChecked": None, "comments": [], "unresolvedSlots": [], "missingPunch": False, "punches": []}
                for index, value in enumerate(period.dates)
            ],
            "additionalRows": [],
            "weeklyTotals": [0, 0],
            "periodTotalHours": 0,
            "approvals": [],
            "attestations": [],
            "mealWaivers": [],
        }
        html = '<html><table id="tbltimesheet"></table><div id="periodtotals"></div>' + ("x" * 1_024) + "</html>"
        return {"record": record, "sourceHtml": html}

    def content(self):
        return "<html></html>"


class Session:
    realm = "paycom-client"
    landing_url = LANDING_URL

    def __init__(self, page):
        self.page = page


def test_timecard_url_binds_employee_period_and_rejects_extra_query():
    period = period_from_end("2026-08-22")
    exact = build_timecard_url("A001", period, 2)
    assert "dispatch_timecards=2" in exact
    from dispatch_paycom.timecards.period import is_captured_timecard_url
    assert is_captured_timecard_url(exact, employee_code="A001", period=period)
    assert not is_captured_timecard_url(exact + "&unexpected=1", employee_code="A001", period=period)


def test_timecard_runner_publishes_all_fourteen_days(tmp_path):
    roster_page = Page()
    roster_db = tmp_path / "roster.sqlite3"
    collect_roster(context=SimpleNamespace(session=Session(roster_page), parameters={"target": "2026-08-05"}), now=datetime(2026, 8, 5, 18, tzinfo=timezone.utc), root=tmp_path / "roster-data", db_path=roster_db)
    page = Page()
    receipt = collect_timecards(SimpleNamespace(session=Session(page), parameters={"period_end": "2026-08-22"}), root=tmp_path / "timecard-data", db_path=tmp_path / "timecards.sqlite3", roster_path=roster_db)
    assert receipt.disposition.value == "published" and receipt.artifact_count == 1
    import sqlite3
    with sqlite3.connect(tmp_path / "timecards.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM days").fetchone()[0] == 14
    assert page.gotos == [build_timecard_url("A001", period_from_end("2026-08-22"), 1)]
    assert not [path for path in (tmp_path / "timecard-data").rglob("*") if path.is_file() and path.name != ".collector.lock"]


def context(**parameters):
    return SimpleNamespace(session=Session(Page()), parameters=parameters)
