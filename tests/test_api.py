"""Integration tests; these use the MongoDB selected by MONGO_URI/MONGO_DB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as api


@pytest.fixture
def client():
    with TestClient(api.app) as test_client:
        yield test_client


def employee_payload(code, department="Test", joined_on="2026-10-01", start="09:30", end="18:30"):
    return {"emp_code": code, "name": "Test Employee", "email": f"{code.lower()}@example.com",
            "department": department, "joined_on": joined_on, "shift_start": start, "shift_end": end}


def epoch_ms(local_day, hhmmss, millis=0):
    h, m, s = map(int, hhmmss.split(":"))
    instant = datetime.combine(local_day, time(h, m, s), api.IST) + timedelta(milliseconds=millis)
    return int(instant.timestamp() * 1000)


@pytest.fixture
def employee(client):
    code = "EMP9901"
    api.db.attendance_logs.delete_many({"emp_code": code})
    api.db.employees.delete_many({"emp_code": code})
    assert client.post("/employees", json=employee_payload(code)).status_code == 201
    yield code
    api.db.attendance_logs.delete_many({"emp_code": code})
    api.db.employees.delete_many({"emp_code": code})


def test_timestamp_validation_and_truncation(client, employee):
    t = epoch_ms(date(2026, 10, 14), "09:30:00", 987)
    r = client.post("/attendance/punch-in", json={"emp_code": employee, "punched_at": t})
    assert r.status_code == 201
    assert r.json()["punch_in"] == (t // 1000) * 1000
    for invalid in (t // 1000, float(t), str(t)):
        assert client.post("/attendance/punch-in", json={"emp_code": employee, "punched_at": invalid}).status_code == 422


def test_grace_boundary_overtime_halfday_and_concurrent_transitions(client, employee):
    start = epoch_ms(date(2026, 10, 15), "09:40:01")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post("/attendance/punch-in", json={"emp_code": employee, "punched_at": start}), range(2)))
    assert sorted(r.status_code for r in results) == [201, 409]
    record = next(r.json() for r in results if r.status_code == 201)
    assert record["late_minutes"] == 10
    out = epoch_ms(date(2026, 10, 15), "19:00:00")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post("/attendance/punch-out", json={"emp_code": employee, "punched_at": out}), range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    closed = next(r.json() for r in results if r.status_code == 200)
    assert closed["overtime_minutes"] == 30
    assert closed["half_day"] is False


def test_half_up_work_hours_and_overnight_date():
    d = date(2026, 10, 12)
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    pin = api.shiftdt(d, "09:30")
    pout = pin + timedelta(seconds=16218)  # 4.505 hours rounds half-up to 4.51.
    assert api.calc(pin, pout, emp, d)["work_hours"] == 4.51
    overnight = {"shift_start": "22:00", "shift_end": "06:00"}
    after_midnight = datetime(2026, 10, 13, 0, 30, tzinfo=api.IST).astimezone(timezone.utc)
    assert api.attendance_day(after_midnight, overnight) == d
    assert api.calc(api.shiftdt(d, "09:40"), None, emp, d)["late_minutes"] == 0
    assert api.calc(api.shiftdt(d, "09:40") + timedelta(seconds=1), None, emp, d)["late_minutes"] == 10
    assert api.calc(pin, pin + timedelta(hours=4, minutes=29), emp, d)["half_day"] is True
    assert api.calc(pin, pin + timedelta(hours=4, minutes=29, seconds=59), emp, d)["half_day"] is False
    assert api.calc(pin, pin + timedelta(hours=4, minutes=30), emp, d)["half_day"] is False
    for mins, expected in ((29, 0), (30, 30), (31, 31)):
        assert api.calc(pin, api.shiftdt(d, "18:30") + timedelta(minutes=mins), emp, d)["overtime_minutes"] == expected


def test_employee_duplicate_pagination_and_date_validation(client):
    codes = ["EMP9910", "EMP9911"]
    api.db.employees.delete_many({"emp_code": {"$in": codes}})
    for code in codes:
        assert client.post("/employees", json=employee_payload(code, "PageTeam")).status_code == 201
    try:
        assert client.post("/employees", json=employee_payload(codes[0], "PageTeam")).status_code == 409
        first = client.get("/employees?department=PageTeam&page=1&page_size=1").json()
        second = client.get("/employees?department=PageTeam&page=2&page_size=1").json()
        assert first["total"] == 2 and second["total"] == 2
        assert first["items"][0]["emp_code"] == codes[0]
        assert second["items"][0]["emp_code"] == codes[1]
        assert client.get("/attendance?date_from=not-a-date").status_code == 422
        assert client.get("/employees?page=0").status_code == 422
    finally:
        api.db.employees.delete_many({"emp_code": {"$in": codes}})


def test_leaderboard_competition_tie_includes_rows_past_limit(client):
    codes = ["EMP9920", "EMP9921", "EMP9922", "EMP9923"]
    api.db.attendance_logs.delete_many({"emp_code": {"$in": codes}})
    api.db.employees.delete_many({"emp_code": {"$in": codes}})
    for code in codes:
        assert client.post("/employees", json=employee_payload(code, "TieTeam")).status_code == 201
    scores = [60, 30, 30, 10]
    api.db.attendance_logs.insert_many([{"emp_code": code, "date": f"2026-10-{20+i:02d}", "status": "PRESENT",
        "punch_in": None, "punch_out": None, "work_hours": None, "late_minutes": score,
        "overtime_minutes": 0} for i, (code, score) in enumerate(zip(codes, scores))])
    try:
        rows = client.get("/analytics/leaderboard/late?month=2026-10&department=TieTeam&limit=2").json()["items"]
        assert [(row["emp_code"], row["rank"]) for row in rows] == [(codes[0], 1), (codes[1], 2), (codes[2], 2)]
    finally:
        api.db.attendance_logs.delete_many({"emp_code": {"$in": codes}})
        api.db.employees.delete_many({"emp_code": {"$in": codes}})


def test_regularization_recomputes_fields_and_appends_audit(client, employee):
    d = date(2026, 10, 16)
    pin = api.shiftdt(d, "09:30")
    pout = api.shiftdt(d, "18:30")
    api.db.attendance_logs.insert_one({"emp_code": employee, "date": d.isoformat(), "status": "PRESENT",
        "punch_in": pin, "punch_out": pout, "work_hours": 9.0, "late_minutes": 0,
        "overtime_minutes": 0, "half_day": False, "history": []})
    corrected = epoch_ms(d, "10:15:00")
    response = client.patch(f"/attendance/{employee}/{d.isoformat()}", json={"punch_in": corrected,
        "reason": "corrected biometric time", "regularized_by": "hr.admin"})
    assert response.status_code == 200
    result = response.json()
    assert result["late_minutes"] == 45
    assert result["work_hours"] == 8.25
    assert len(result["history"]) == 1
    assert result["history"][0]["changes"]["punch_in"]["from"] == int(pin.timestamp() * 1000)
    assert result["history"][0]["changes"]["late_minutes"] == {"from": 0, "to": 45}


def test_no_log_employee_and_empty_trend_gap_fill(client):
    code = "EMP9902"
    api.db.attendance_logs.delete_many({"emp_code": code})
    api.db.employees.delete_many({"emp_code": code})
    assert client.post("/employees", json=employee_payload(code, "NoLogs", "2026-10-15")).status_code == 201
    try:
        monthly = client.get(f"/analytics/employees/{code}/monthly?month=2026-10").json()
        assert monthly["working_days"] == 12
        assert monthly["present_days"] == 0
        summary = client.get("/analytics/departments/summary?month=2026-10&department=NoLogs").json()
        assert summary["items"][0]["headcount"] == 1
        trend = client.get("/analytics/departments/NoLogs/trend?from=2026-10-20&to=2026-10-23").json()["items"]
        assert len(trend) == 4
        assert [row["attendance_rate"] for row in trend] == [0.0] * 4
    finally:
        api.db.employees.delete_many({"emp_code": code})


def test_all_explain_routes_return_real_execution_stats(client):
    urls = ["/admin/explain/attendance_list?page=1&page_size=20",
            "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
            "/admin/explain/department_summary?month=2026-07",
            "/admin/explain/late_leaderboard?month=2026-07",
            "/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-10"]
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200
        payload = response.json()
        assert "executionStats" in str(payload["explain"])
        plan = str(payload["explain"])
        assert "IXSCAN" in plan
        assert "COLLSCAN" not in plan
