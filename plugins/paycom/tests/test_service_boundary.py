from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3

from dispatch_paycom.service import handle


def _make_fixture(tmp_path: Path) -> dict[str, Path]:
    paths = {name: tmp_path / f"{name}.sqlite3" for name in ("roster", "timecards", "meals", "identity")}
    def create(name: str, sql: str) -> sqlite3.Connection:
        connection = sqlite3.connect(paths[name])
        connection.executescript(sql)
        connection.commit()
        connection.close()
        os.chmod(paths[name], 0o600)
        return sqlite3.connect(paths[name])

    roster = create("roster", """
        CREATE TABLE collection_runs(run_id TEXT PRIMARY KEY,target TEXT,collected_at TEXT,source_sha256 TEXT,employee_count INTEGER,active_employee_count INTEGER,active_driver_count INTEGER);
        CREATE TABLE active_snapshots(target TEXT PRIMARY KEY,run_id TEXT);
        CREATE TABLE employees(run_id TEXT,employee_code TEXT,employee_name TEXT,status TEXT,department_code TEXT,department_desc TEXT,delivery_station_code TEXT,delivery_station_desc TEXT,position_title TEXT,pay_class TEXT,pay_type TEXT,primary_supervisor TEXT,is_active INTEGER,is_active_driver INTEGER);
    """)
    roster.execute("INSERT INTO collection_runs VALUES(?,?,?,?,?,?,?)", ("r1", "2026-08-05", "2026-08-05T12:00:00Z", "a" * 64, 1, 1, 1))
    roster.execute("INSERT INTO active_snapshots VALUES(?,?)", ("2026-08-05", "r1"))
    roster.execute("INSERT INTO employees VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("r1", "A001", "Smith, Alex", "Active", "D", "Delivery", "S", "Station", "Driver", "H", "Hourly", "Boss", 1, 1))
    roster.commit(); roster.close()

    timecards = create("timecards", """
        CREATE TABLE runs(id INTEGER PRIMARY KEY,period_start TEXT,period_end TEXT,period_key TEXT,roster_revision TEXT,roster_source_sha256 TEXT,employee_count INTEGER,day_count INTEGER,punch_count INTEGER,missing_day_count INTEGER,collected_at TEXT);
        CREATE TABLE active_periods(period_end TEXT PRIMARY KEY,run_id INTEGER);
        CREATE TABLE employee_timecards(run_id INTEGER,employee_code TEXT,employee_name TEXT,period_total_hours REAL);
        CREATE TABLE days(run_id INTEGER,employee_code TEXT,work_date TEXT,hours REAL,total_hours REAL,comments_json TEXT,missing_punch INTEGER,unresolved_slots_json TEXT);
        CREATE TABLE punches(run_id INTEGER,employee_code TEXT,work_date TEXT,ordinal INTEGER,kind TEXT,display_time TEXT,actual_time TEXT,provenance_available INTEGER,change_request_status TEXT,approved INTEGER,comment TEXT);
    """)
    timecards.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (1, "2026-07-26", "2026-08-08", "2026-07-26_2026-08-08", "r1", "a" * 64, 1, 14, 2, 0, "2026-08-08T12:00:00Z"))
    timecards.execute("INSERT INTO active_periods VALUES(?,?)", ("2026-08-08", 1))
    timecards.execute("INSERT INTO employee_timecards VALUES(?,?,?,?)", (1, "A001", "Smith, Alex", 8))
    for index in range(14):
        work_date = (date(2026, 7, 26) + timedelta(days=index)).isoformat()
        timecards.execute("INSERT INTO days VALUES(?,?,?,?,?,?,?,?)", (1, "A001", work_date, 8 if index == 0 else None, 8 if index == 0 else None, "[]", 0, "[]"))
        if index == 0:
            timecards.execute("INSERT INTO punches VALUES(?,?,?,?,?,?,?,?,?,?,?)", (1, "A001", work_date, 1, "OUT LUNCH", "12:00 PM", "12:00 PM", 1, None, 0, ""))
            timecards.execute("INSERT INTO punches VALUES(?,?,?,?,?,?,?,?,?,?,?)", (1, "A001", work_date, 2, "IN LUNCH", "12:30 PM", "12:30 PM", 1, None, 0, ""))
    timecards.commit(); timecards.close()

    meals = create("meals", """
        CREATE TABLE meal_break_gap_reports(id INTEGER PRIMARY KEY,report_date TEXT,collected_at TEXT,driver_count INTEGER,total_driver_count INTEGER,meal_break_count INTEGER,row_count INTEGER,status TEXT);
        CREATE TABLE meal_break_gap_rows(report_id INTEGER,report_date TEXT,transporter_id TEXT,delivery_associate_name TEXT,route_code TEXT,meal_index INTEGER,meal_start_time TEXT,meal_end_time TEXT,meal_length_min INTEGER,parse_warning TEXT);
    """)
    meals.execute("INSERT INTO meal_break_gap_reports VALUES(?,?,?,?,?,?,?,?)", (1, "2026-07-26", "2026-07-26T22:15:00Z", 1, 1, 1, 1, "ok"))
    meals.execute("INSERT INTO meal_break_gap_rows VALUES(?,?,?,?,?,?,?,?,?,?)", (1, "2026-07-26", "flex-1", "Alex Smith", "R1", 1, "12:00 PM", "12:30 PM", 30, None))
    meals.commit(); meals.close()

    identity = create("identity", """
        PRAGMA user_version=1;
        CREATE TABLE identity_crosswalk(transporter_id TEXT PRIMARY KEY,employee_code TEXT UNIQUE,effective_start TEXT,effective_end TEXT,approved_at TEXT,approved_by TEXT,evidence_json TEXT);
    """)
    identity.close()
    return paths


def test_meal_comparison_uses_flex_population_and_exact_name_fallback(tmp_path: Path) -> None:
    paths = _make_fixture(tmp_path)
    result = handle({"action": "meal_comparison", "work_date": "2026-07-26"}, paths=paths, clock=lambda: datetime(2026, 7, 27, 1, tzinfo=timezone.utc))
    assert set(result) == {"ok", "action", "status", "data", "freshness", "delivery", "error"}
    assert result["ok"] is True
    assert result["status"] == "found"
    assert result["data"]["completePopulation"] is True
    assert result["data"]["returnedDrivers"] == 1
    row = result["data"]["results"][0]
    assert row["employeeCode"] == "A001"
    assert row["identitySource"] == "exact_name"
    assert row["comparisonStatus"] == "exact_match"


def test_invalid_and_report_requests_are_closed(tmp_path: Path) -> None:
    paths = _make_fixture(tmp_path)
    invalid = handle({"action": "meal_comparison", "sql": "select 1"}, paths=paths)
    retired = handle({"action": "meal_comparison_report"}, paths=paths)
    assert invalid["ok"] is False and invalid["error"]["code"] == "invalid_input"
    assert retired["ok"] is False and retired["error"]["code"] == "invalid_input"


def test_audit_and_health_are_bounded(tmp_path: Path) -> None:
    paths = _make_fixture(tmp_path)
    clock = lambda: datetime(2026, 7, 27, 1, tzinfo=timezone.utc)
    audit = handle({"action": "audit", "work_date": "2026-07-26"}, paths=paths, clock=clock)
    assert audit["status"] == "verified"
    assert all(audit["data"]["checks"].values())
    health = handle({"action": "health"}, paths=paths)
    assert set(health) == {"ok", "action", "status", "data", "freshness", "delivery", "error"}
    assert health["data"]["overall"] == "ready"


def test_live_wal_is_rejected_without_mutating_the_reader(tmp_path: Path) -> None:
    paths = _make_fixture(tmp_path)
    writer = sqlite3.connect(paths["roster"])
    try:
        writer.executescript("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; BEGIN IMMEDIATE; UPDATE active_snapshots SET target=target; COMMIT;")
        assert paths["roster"].with_name(paths["roster"].name + "-wal").stat().st_size > 0
        result = handle({"action": "health"}, paths=paths)
        assert result["ok"] is False
        assert result["error"]["code"] == "unavailable"
    finally:
        writer.close()


def test_identity_crosswalk_rejects_duplicate_transporter_ids(tmp_path: Path) -> None:
    paths = _make_fixture(tmp_path)
    connection = sqlite3.connect(paths["identity"])
    connection.execute("DROP TABLE identity_crosswalk")
    connection.execute("CREATE TABLE identity_crosswalk(transporter_id TEXT,employee_code TEXT,effective_start TEXT,effective_end TEXT,approved_at TEXT,approved_by TEXT,evidence_json TEXT)")
    values = ("duplicate", "A001", "2026-07-01", None, "2026-07-01T00:00:00Z", "test", '["dcr_transporter_full_name"]')
    connection.execute("INSERT INTO identity_crosswalk VALUES(?,?,?,?,?,?,?)", values)
    connection.execute("INSERT INTO identity_crosswalk VALUES(?,?,?,?,?,?,?)", values[:-6] + ("A002",) + values[-5:])
    connection.commit(); connection.close()
    result = handle({"action": "meal_comparison", "work_date": "2026-07-26"}, paths=paths)
    assert result["ok"] is False and result["error"]["code"] == "schema_invalid"


def test_identity_crosswalk_rejects_non_alphanumeric_employee_codes(tmp_path: Path) -> None:
    paths = _make_fixture(tmp_path)
    connection = sqlite3.connect(paths["identity"])
    connection.execute("INSERT INTO identity_crosswalk VALUES(?,?,?,?,?,?,?)", ("transporter", "A-01", "2026-07-01", None, "2026-07-01T00:00:00Z", "test", '["dcr_transporter_full_name"]'))
    connection.commit(); connection.close()
    result = handle({"action": "meal_comparison", "work_date": "2026-07-26"}, paths=paths)
    assert result["ok"] is False and result["error"]["code"] == "schema_invalid"
