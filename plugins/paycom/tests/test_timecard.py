from __future__ import annotations

from datetime import date, datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "plugins" / "paycom" / "src"
CORE = ROOT / "dispatch-core"
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(SOURCE))

from dispatch_paycom.roster.browser import ROSTER_URL
from dispatch_paycom.roster.collector import LANDING_URL, collect_roster
from dispatch_paycom.roster.models import HEADERS
from dispatch_paycom.timecards.browser import TimecardBrowserError, capture_timecard
from dispatch_paycom.timecards.collector import TimecardCollectorError, collect_timecards, verify_timecard_publication
from dispatch_paycom.timecards.extraction import EXTRACTION_SCRIPT
from dispatch_paycom.timecards.models import HEADERS as TIMECARD_HEADERS
from dispatch_paycom.timecards.models import HEADERS_NO_WAIVER, TimecardRecordError, validate_timecard_record
from dispatch_paycom.timecards.period import build_timecard_url, period_from_end
from collection_manager import CollectionRequest, PublicationVerification


ROSTER_BYTES = (",".join(HEADERS) + "\r\n" + ",".join(["A001", "Alpha Driver", "A", "00004", "Driver", "STA", "Station", "Driver", "DOT4", "DOT4", "Hourly", "Supervisor", "0", "40", "0", "100", "100"]) + "\r\n").encode()


def _timecard_record(period, *, source_url: str, headers=TIMECARD_HEADERS, dates=None):
    rendered_dates = tuple(dates or period.dates)
    return {
        "version": 1,
        "sourceFormat": "paycom-timecard-dom.v1",
        "employeeCode": "A001",
        "periodStart": period.start,
        "periodEnd": period.end,
        "periodKey": period.key,
        "sourceUrl": source_url,
        "pageTitle": "Timecard Editor",
        "headers": list(headers),
        "days": [
            {
                "date": value,
                "label": ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")[index % 7],
                "payCode": "",
                "allocation1": "",
                "allocation2": "",
                "hours": 0,
                "totalHours": 0,
                "dollars": 0,
                "exceptionText": "",
                "waiverChecked": None,
                "comments": [],
                "unresolvedSlots": [],
                "missingPunch": False,
                "punches": [],
            }
            for index, value in enumerate(rendered_dates)
        ],
        "additionalRows": [],
        "weeklyTotals": [0, 0],
        "periodTotalHours": 0,
        "approvals": [],
        "attestations": [],
        "mealWaivers": [],
    }


def _timecard_table_fixture(*, rendered_days=None, continuation=False, unknown=False, headers=TIMECARD_HEADERS):
    period = period_from_end("2026-08-22")
    if rendered_days is None:
        rendered_days = [f'{date.fromisoformat(value).strftime("%a").upper()} ({value[5:]}'.replace("-", "/") + ")" for value in period.dates]

    def row(values, *, class_name=""):
        class_attribute = f' class="{class_name}"' if class_name else ""
        cells = "".join(f"<td>{values.get(header, '')}</td>" for header in headers)
        return f"<tr{class_attribute}>{cells}</tr>"

    day_rows = []
    for rendered in rendered_days:
        values = {
            "date": rendered,
            "paycode": "REG",
            "allocation1": "Main Yard",
            "allocation2": "Operations",
            "hours": "8.00",
            "total_hours": "8.00",
            "amount": "$120.00",
            "waiver": '<input type="checkbox" checked>',
            "comment": '<span title="Comment: routine shift">note</span>',
        }
        day_rows.append(row(values))

    body = list(day_rows)
    if continuation:
        body.append(row({"paycode": "OT", "allocation1": "Main Yard", "allocation2": "Operations", "hours": "1.25", "total_hours": "1.25", "amount": "$18.75"}, class_name="continuation-row"))
    body.extend(["<tr><td>Weekly Totals</td><td>56.00</td></tr>", "<tr><td>Weekly Totals</td><td>56.00</td></tr>"])
    if unknown:
        body.insert(7, f'<tr class="unexpected-row"><td colspan="{len(headers)}">Unexpected approval state</td></tr>')

    header_cells = "".join(f'<th data-column="{header}">{header}</th>' for header in headers)
    return f'<html><head><title>Timecard Editor</title></head><body><table id="tbltimesheet"><thead><tr>{header_cells}</tr></thead><tbody>{"".join(body)}</tbody></table><div id="periodtotals"></div></body></html>'


EXACT_HEADERS_FIXTURE = _timecard_table_fixture()
WRONG_DATE_FIXTURE = _timecard_table_fixture(rendered_days=["MON (08/09)"] + [f'{date.fromisoformat(value).strftime("%a").upper()} ({value[5:].replace("-", "/")})' for value in period_from_end("2026-08-22").dates[1:]])
REORDERED_DATE_FIXTURE = _timecard_table_fixture(rendered_days=(lambda values: [values[1], values[0], *values[2:]])([f'{date.fromisoformat(value).strftime("%a").upper()} ({value[5:].replace("-", "/")})' for value in period_from_end("2026-08-22").dates]))
CONTINUATION_ROW_FIXTURE = _timecard_table_fixture(continuation=True)
UNKNOWN_MEANINGFUL_ROW_FIXTURE = _timecard_table_fixture(unknown=True)


class RosterResponse:
    url = ROSTER_URL
    status = 200
    headers = {"content-type": "text/html"}

    def body(self):
        return ROSTER_BYTES


class Page:
    def __init__(self, *, dates=None, headers=TIMECARD_HEADERS):
        self.url = ""
        self.gotos: list[str] = []
        self.dates = dates
        self.headers = headers

    def goto(self, url, *, wait_until, timeout):
        self.url = url
        self.gotos.append(url)
        return RosterResponse() if "timecardsearch" in url else None

    def wait_for_load_state(self, state, *, timeout):
        return None

    def evaluate(self, expression, config=None) -> Any:
        period = period_from_end("2026-08-22")
        record = _timecard_record(period, source_url=self.url, headers=self.headers, dates=self.dates)
        html = '<html><table id="tbltimesheet"></table><div id="periodtotals"></div>' + ("x" * 1_024) + "</html>"
        return {"record": record, "sourceHtml": html}

    def content(self):
        return "<html></html>"


class NullProjectionPage(Page):
    def evaluate(self, expression, config=None) -> Any:
        return None


class Session:
    realm = "paycom-client"
    landing_url = LANDING_URL

    def __init__(self, page):
        self.page = page


def test_timecard_dom_fixtures_cover_exact_headers_dates_and_row_boundaries():
    assert [value.split('"')[0] for value in EXACT_HEADERS_FIXTURE.split('data-column="')[1:]] == list(TIMECARD_HEADERS)
    assert 'data-column="o1">o1</th>' in EXACT_HEADERS_FIXTURE
    assert 'data-column="i2">i2</th>' in EXACT_HEADERS_FIXTURE
    assert EXACT_HEADERS_FIXTURE.index('data-column="i2"') < EXACT_HEADERS_FIXTURE.index('data-column="allocation2"')
    assert "MON (08/09)" in WRONG_DATE_FIXTURE
    assert REORDERED_DATE_FIXTURE.index("MON (08/10)") < REORDERED_DATE_FIXTURE.index("SUN (08/09)")
    assert 'class="continuation-row"' in CONTINUATION_ROW_FIXTURE
    assert 'class="unexpected-row"' in UNKNOWN_MEANINGFUL_ROW_FIXTURE
    assert "expectedByMonthDay" in EXTRACTION_SCRIPT
    assert "if (!meaningful(row)) return null" not in EXTRACTION_SCRIPT
    assert "if (!meaningful(row)) continue" in EXTRACTION_SCRIPT


def test_timecard_headers_keep_i2_order_and_approved_no_waiver_variant():
    assert TIMECARD_HEADERS == ("date", "paycode", "i1", "allocation1", "o1", "i2", "allocation2", "o2", "hours", "total_hours", "amount", "exception-points", "waiver", "comment", "missing-punch", "delete")
    assert HEADERS_NO_WAIVER == tuple(value for value in TIMECARD_HEADERS if value != "waiver")
    period = period_from_end("2026-08-22")
    source_url = build_timecard_url("A001", period, 1)
    record = _timecard_record(period, source_url=source_url, headers=HEADERS_NO_WAIVER)
    assert validate_timecard_record(record, employee_code="A001", period=period, source_url=source_url) is record


def test_timecard_record_requires_exact_date_identity_and_order():
    period = period_from_end("2026-08-22")
    source_url = build_timecard_url("A001", period, 1)
    for dates in (
        period.dates[:-1] + ("2026-08-23",),
        (period.dates[1], period.dates[0], *period.dates[2:]),
    ):
        record = _timecard_record(period, source_url=source_url, dates=dates)
        with pytest.raises(TimecardRecordError):
            validate_timecard_record(record, employee_code="A001", period=period, source_url=source_url)


def test_timecard_browser_fails_closed_when_dom_projection_is_unknown():
    period = period_from_end("2026-08-22")
    with pytest.raises(TimecardBrowserError, match="timecard_projection_unavailable"):
        capture_timecard(NullProjectionPage(), employee_code="A001", period=period)


def test_timecard_browser_rejects_wrong_or_reordered_projection_dates():
    period = period_from_end("2026-08-22")
    for dates in (period.dates[:-1] + ("2026-08-23",), (period.dates[1], period.dates[0], *period.dates[2:])):
        with pytest.raises(TimecardBrowserError, match="timecard_projection_invalid"):
            capture_timecard(Page(dates=dates), employee_code="A001", period=period)


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


def test_timecard_rejects_unknown_parameter_and_non_boolean_replace_before_capture(tmp_path):
    calls = []

    def capture(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("capture must not run")

    with pytest.raises(TimecardCollectorError, match="unknown_parameter"):
        collect_timecards(
            context(period_end="2026-08-22", unexpected="x"),
            root=tmp_path / "timecard-data",
            db_path=tmp_path / "timecards.sqlite3",
            roster_path=tmp_path / "roster.sqlite3",
            capture=capture,
        )
    with pytest.raises(TimecardCollectorError, match="replace_invalid"):
        collect_timecards(
            context(period_end="2026-08-22", replace=1),
            root=tmp_path / "timecard-data",
            db_path=tmp_path / "timecards.sqlite3",
            roster_path=tmp_path / "roster.sqlite3",
            capture=capture,
        )
    assert calls == []


def test_timecard_rejects_live_wal_roster_before_capture(tmp_path):
    roster_db = tmp_path / "roster.sqlite3"
    collect_roster(
        context=SimpleNamespace(session=Session(Page()), parameters={"target": "2026-08-05"}),
        now=datetime(2026, 8, 5, 18, tzinfo=timezone.utc),
        root=tmp_path / "roster-data",
        db_path=roster_db,
    )
    writer = sqlite3.connect(roster_db)
    try:
        writer.executescript("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; BEGIN IMMEDIATE; UPDATE active_snapshots SET target=target; COMMIT;")
        assert os.path.getsize(str(roster_db) + "-wal") > 0
        calls = []

        def capture(*_args, **_kwargs):
            calls.append(True)
            raise AssertionError("capture must not run")

        with pytest.raises(TimecardCollectorError):
            collect_timecards(
                context(period_end="2026-08-22"),
                root=tmp_path / "timecard-data",
                db_path=tmp_path / "timecards.sqlite3",
                roster_path=roster_db,
                capture=capture,
            )
        assert calls == []
    finally:
        writer.close()


def test_timecard_projection_rejects_string_replacement(tmp_path):
    roster_db = tmp_path / "roster.sqlite3"
    collect_roster(
        context=SimpleNamespace(session=Session(Page()), parameters={"target": "2026-08-05"}),
        now=datetime(2026, 8, 5, 18, tzinfo=timezone.utc),
        root=tmp_path / "roster-data",
        db_path=roster_db,
    )
    collect_timecards(
        context=SimpleNamespace(session=Session(Page()), parameters={"period_end": "2026-08-22"}),
        root=tmp_path / "timecard-data",
        db_path=tmp_path / "timecards.sqlite3",
        roster_path=roster_db,
    )
    connection = sqlite3.connect(tmp_path / "timecards.sqlite3")
    try:
        connection.execute("UPDATE days SET pay_code='CORRUPTED'")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(TimecardCollectorError, match="post_verification_failed"):
        collect_timecards(
            context=SimpleNamespace(session=Session(Page()), parameters={"period_end": "2026-08-22", "replace": True}),
            root=tmp_path / "timecard-data",
            db_path=tmp_path / "timecards.sqlite3",
            roster_path=roster_db,
        )


def test_timecard_publication_verifier_proves_exact_absence(tmp_path, monkeypatch):
    database = tmp_path / "timecards.sqlite3"
    monkeypatch.setattr("dispatch_paycom.timecards.collector._paths", lambda: (tmp_path / "cache", database, tmp_path / "roster.sqlite3"))
    request = CollectionRequest("paycom-timecards", parameters={"period-end": "2026-08-22"})
    assert verify_timecard_publication(request, None) is PublicationVerification.ABSENT


def context(**parameters):
    return SimpleNamespace(session=Session(Page()), parameters=parameters)
