"""Strict Paycom roster source parsing.

The browser is only allowed to hand this module the complete export response.  No
employee rows are inferred from page text and no incomplete source is accepted.
"""
from __future__ import annotations

from collections.abc import Mapping
import csv
import io
import json
import math
import re
from typing import Any

HEADERS = (
    "Employee Code",
    "Employee Name",
    "Status",
    "Department Code",
    "Department Desc",
    "Delivery Station Code Code",
    "Delivery Station Code Desc",
    "Position Title",
    "Pay Class",
    "Terminal Group",
    "Pay Type",
    "Primary Supervisor",
    "Missing Punches",
    "Total Hours",
    "Total Overtime Hours",
    "Percent Approved (EE)",
    "Percent Approved (SUP)",
)
_JSON_REQUIRED = (
    "employeeCode",
    "fullName",
    "eestatus",
    "allocation",
    "position",
    "payClassCode",
    "terminalCode",
    "payType",
    "primarySupervisor",
    "missingPunches",
    "totals",
    "approvalPercentages",
)
_CODE = re.compile(r"^[A-Za-z0-9]{4}$")
_DRIVER_DEPARTMENTS = {"Driver", "Driver- Step Van", "Driver-Step Van"}


class RosterSourceError(ValueError):
    """A source is malformed, incomplete, or not a supported Paycom export."""

    def __init__(self, code: str = "roster_source_invalid") -> None:
        super().__init__(code)
        self.code = code


def _invalid(code: str = "roster_source_invalid") -> None:
    raise RosterSourceError(code)


def _text(value: Any) -> str:
    if not isinstance(value, str):
        _invalid()
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        _invalid()
    return value


def _number_text(value: str, *, integer: bool = False) -> None:
    if value == "":
        return
    pattern = r"^-?\d+$" if integer else r"^-?(?:\d+|\d*\.\d+)$"
    if not re.fullmatch(pattern, value):
        _invalid("roster_source_invalid")


def _employee_from_csv(raw: dict[str, str]) -> dict[str, Any]:
    code = raw["Employee Code"]
    name = raw["Employee Name"]
    if not _CODE.fullmatch(code) or not name.strip() or raw["Status"] not in {"A", "I"}:
        _invalid()
    _number_text(raw["Missing Punches"], integer=True)
    for key in ("Total Hours", "Total Overtime Hours", "Percent Approved (EE)", "Percent Approved (SUP)"):
        _number_text(raw[key])
    department = raw["Department Desc"]
    driver_department = department in _DRIVER_DEPARTMENTS
    driver_position = bool(re.search(r"driver|step\s*van", raw["Position Title"], re.I))
    return {
        "employeeCode": code,
        "employeeName": name,
        "status": raw["Status"],
        "departmentCode": raw["Department Code"],
        "departmentDesc": department,
        "deliveryStationCode": raw["Delivery Station Code Code"],
        "deliveryStationDesc": raw["Delivery Station Code Desc"],
        "positionTitle": raw["Position Title"],
        "payClass": raw["Pay Class"],
        "terminalGroup": raw["Terminal Group"],
        "payType": raw["Pay Type"],
        "primarySupervisor": raw["Primary Supervisor"],
        "missingPunches": raw["Missing Punches"],
        "totalHours": raw["Total Hours"],
        "totalOvertimeHours": raw["Total Overtime Hours"],
        "employeeApprovalPercentage": raw["Percent Approved (EE)"],
        "supervisorApprovalPercentage": raw["Percent Approved (SUP)"],
        "isActive": raw["Status"] == "A",
        "isDriverDepartment": driver_department,
        "isDriverPosition": driver_position,
        "isActiveDriver": raw["Status"] == "A" and driver_department,
    }


def parse_roster_csv(source: bytes | bytearray) -> dict[str, Any]:
    if not isinstance(source, (bytes, bytearray)) or not 32 <= len(source) <= 2 * 1024 * 1024:
        _invalid("roster_source_invalid")
    try:
        text = bytes(source).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RosterSourceError("roster_source_invalid") from exc
    try:
        records = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise RosterSourceError("roster_source_invalid") from exc
    if len(records) < 2 or tuple(records[0]) != HEADERS:
        _invalid("roster_source_invalid")
    employees: list[dict[str, Any]] = []
    totals: dict[str, str] | None = None
    seen: set[str] = set()
    for values in records[1:]:
        if len(values) != len(HEADERS):
            _invalid()
        raw = dict(zip(HEADERS, (_text(item) for item in values), strict=True))
        if raw["Employee Code"] == "Grand Totals" and raw["Employee Name"] == "":
            if totals is not None:
                _invalid()
            totals = raw
            continue
        code = raw["Employee Code"].upper()
        if code in seen:
            _invalid("roster_membership_mismatch")
        seen.add(code)
        employees.append(_employee_from_csv(raw))
    if not employees:
        _invalid("roster_membership_mismatch")
    return {
        "sourceFormat": "paycom-timecard-csv.v1",
        "headers": list(HEADERS),
        "rowCount": len(records) - 1,
        "employeeCount": len(employees),
        "activeEmployeeCount": sum(item["isActive"] for item in employees),
        "activeDriverCount": sum(item["isActiveDriver"] for item in employees),
        "sourceBytes": len(source),
        "totals": totals,
        "employees": employees,
    }


def _finite_text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _invalid()
    return str(value)


def parse_roster_api(source: bytes | bytearray | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(source, Mapping):
        value = dict(source)
        source_bytes = len(json.dumps(value, separators=(",", ":")).encode())
    else:
        if not isinstance(source, (bytes, bytearray)) or not 32 <= len(source) <= 2 * 1024 * 1024:
            _invalid("roster_source_invalid")
        try:
            value = json.loads(bytes(source).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise RosterSourceError("roster_source_invalid") from exc
        source_bytes = len(source)
    if not isinstance(value, dict) or not isinstance(value.get("eeCodes"), list) or not isinstance(value.get("employees"), list):
        _invalid("roster_source_invalid")
    employees_raw = value["employees"]
    codes_raw = value["eeCodes"]
    if not 1 <= len(employees_raw) <= 5000 or len(codes_raw) != len(employees_raw):
        _invalid("roster_membership_mismatch")
    seen: set[str] = set()
    employees: list[dict[str, Any]] = []
    for raw in employees_raw:
        if not isinstance(raw, dict) or any(key not in raw for key in _JSON_REQUIRED):
            _invalid()
        code = raw["employeeCode"]
        if not isinstance(code, str) or not _CODE.fullmatch(code) or code.upper() in seen or not isinstance(raw["fullName"], str) or not raw["fullName"].strip() or raw["eestatus"] != "A":
            _invalid("roster_membership_mismatch")
        selections = raw["allocation"].get("selections") if isinstance(raw["allocation"], dict) else None
        if not isinstance(selections, list) or len(selections) != 2:
            _invalid()
        by_name = {item.get("categoryName"): item for item in selections if isinstance(item, dict)}
        department = by_name.get("Department")
        station = by_name.get("Delivery Station Code")
        if not isinstance(department, dict) or not isinstance(station, dict) or department.get("isDepartment") is not True or station.get("isDepartment") is not False:
            _invalid()
        if any(not isinstance(item.get("code"), str) or not isinstance(item.get("description"), str) for item in (department, station)):
            _invalid()
        if any(not isinstance(raw[key], str) for key in ("position", "payClassCode", "terminalCode", "payType", "primarySupervisor")):
            _invalid()
        if not isinstance(raw["missingPunches"], int) or isinstance(raw["missingPunches"], bool) or not isinstance(raw["totals"], dict) or not isinstance(raw["approvalPercentages"], dict):
            _invalid()
        department_desc = department["description"]
        driver_department = department_desc in _DRIVER_DEPARTMENTS
        driver_position = bool(re.search(r"driver|step\s*van", raw["position"], re.I))
        seen.add(code.upper())
        employees.append({
            "employeeCode": code,
            "employeeName": raw["fullName"],
            "status": "A",
            "departmentCode": department["code"],
            "departmentDesc": department_desc,
            "deliveryStationCode": station["code"],
            "deliveryStationDesc": station["description"],
            "positionTitle": raw["position"],
            "payClass": raw["payClassCode"],
            "terminalGroup": raw["terminalCode"],
            "payType": raw["payType"],
            "primarySupervisor": raw["primarySupervisor"],
            "missingPunches": str(raw["missingPunches"]),
            "totalHours": _finite_text(raw["totals"].get("totalHours")),
            "totalOvertimeHours": _finite_text(raw["totals"].get("otHours")),
            "employeeApprovalPercentage": _finite_text(raw["approvalPercentages"].get("employee")),
            "supervisorApprovalPercentage": _finite_text(raw["approvalPercentages"].get("supervisor")),
            "isActive": True,
            "isDriverDepartment": driver_department,
            "isDriverPosition": driver_position,
            "isActiveDriver": driver_department,
        })
    try:
        declared = {str(item).upper() for item in codes_raw}
    except Exception as exc:
        raise RosterSourceError("roster_membership_mismatch") from exc
    if len(declared) != len(seen) or declared != seen:
        _invalid("roster_membership_mismatch")
    return {
        "sourceFormat": "paycom-employees-json.v1",
        "headers": list(HEADERS),
        "rowCount": len(employees),
        "employeeCount": len(employees),
        "activeEmployeeCount": len(employees),
        "activeDriverCount": sum(item["isActiveDriver"] for item in employees),
        "sourceBytes": source_bytes,
        "totals": None,
        "employees": employees,
    }


def parse_roster_source(source: bytes | bytearray | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(source, Mapping):
        return parse_roster_api(source)
    if not isinstance(source, (bytes, bytearray)):
        _invalid()
    prefix = bytes(source)[:64].lstrip()
    if prefix.startswith(b"{"):
        return parse_roster_api(source)
    return parse_roster_csv(source)


__all__ = ["HEADERS", "RosterSourceError", "parse_roster_api", "parse_roster_csv", "parse_roster_source"]
