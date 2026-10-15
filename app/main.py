"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import math
import os
import re
import json
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True, name="unique_emp_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="employees_department_code")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="employees_joined_department")
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="unique_attendance_day"
    )
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="attendance_date_code")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)], name="attendance_filters")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be a valid YYYY-MM-DD date") from exc


def parse_shift(value: str) -> time:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("must be a valid HH:MM time")
    return time.fromisoformat(value)


def as_utc_datetime(epoch_millis: Optional[int]) -> Optional[datetime]:
    if epoch_millis is None:
        return None
    if epoch_millis < 100_000_000_000 or epoch_millis > 4_102_444_800_000:
        raise ValueError("must be epoch milliseconds")
    return datetime.fromtimestamp(epoch_millis / 1000, UTC).replace(microsecond=0)


def epoch_millis(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_boundary(attendance_date: str, shift_value: str, overnight: bool = False) -> datetime:
    day = parse_date(attendance_date)
    if overnight:
        day += timedelta(days=1)
    return datetime.combine(day, parse_shift(shift_value), IST).astimezone(UTC)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_shift(shift_end) <= parse_shift(shift_start)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = punch_in.astimezone(IST)
    result = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_shift(shift_end):
        result -= timedelta(days=1)
    return result.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    start = shift_boundary(attendance_date, shift_start)
    minutes = math.floor((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round_half_up((punch_out - punch_in).total_seconds() / 3600)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    end = shift_boundary(date_str, shift_end, is_overnight(shift_start, shift_end))
    minutes = math.floor((punch_out - end).total_seconds() / 60)
    return minutes if minutes >= 30 else 0


def serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return epoch_millis(value)
    if isinstance(value, list):
        return [serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize_value(item) for key, item in value.items()}
    return value


def serialize_document(document: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in document.items() if key != "_id"}
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    return serialize_value(result)


def month_range(month: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("month must be YYYY-MM")
    year, month_number = map(int, month.split("-"))
    first = date(year, month_number, 1)
    next_month = date(year + (month_number == 12), 1 if month_number == 12 else month_number + 1, 1)
    return first.isoformat(), (next_month - timedelta(days=1)).isoformat()


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    return sum((start + timedelta(days=offset)).weekday() < 5 for offset in range((end - start).days + 1))


def round_rate(value: Optional[float]) -> Optional[float]:
    return round_half_up(value, 4) if value is not None else None


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if len(value) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("invalid email")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift(cls, value: str) -> str:
        parse_shift(value)
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def validate_shift_pair(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[str] = None
    punch_in: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}:
            raise ValueError("invalid attendance status")
        return value


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serialize_document(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    )
    return {
        "items": [serialize_document(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = as_utc_datetime(body.punched_at) or datetime.now(UTC).replace(microsecond=0)
    d = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return serialize_document(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    try:
        if date_from:
            parse_date(date_from)
        if date_to:
            parse_date(date_to)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    total = db.attendance_logs.count_documents(q)
    page_docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_document(document) for document in page_docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    punched_at = as_utc_datetime(body.punched_at) or datetime.now(UTC).replace(microsecond=0)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")

    punch_in_value = record.get("punch_in")
    if punched_at <= punch_in_value or punched_at > punch_in_value + timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")

    work_hours = compute_work_hours(punch_in_value, punched_at)
    overtime_minutes = compute_overtime(
        punched_at,
        employee["shift_start"],
        employee["shift_end"],
        record["date"],
    )
    values = {
        "punch_out": punched_at,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": work_hours < 4.50,
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": values},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record is already punched out")
    return serialize_document(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        parse_date(date)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    employee = db.employees.find_one({"emp_code": emp_code})
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if employee is None or record is None:
        raise HTTPException(404, "employee or attendance record not found")

    supplied = body.model_fields_set
    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    new_status = body.status if "status" in supplied else old_status
    new_punch_in = as_utc_datetime(body.punch_in) if "punch_in" in supplied else old_punch_in
    new_punch_out = as_utc_datetime(body.punch_out) if "punch_out" in supplied else old_punch_out

    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "absence and leave records cannot have punch times")
        new_punch_in = None
        new_punch_out = None
    else:
        if new_punch_in is None:
            raise HTTPException(422, "a presence record requires punch_in")
        if attendance_date_for(new_punch_in, employee["shift_start"], employee["shift_end"]) != date:
            raise HTTPException(422, "punch_in must belong to the record date")
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in or new_punch_out > new_punch_in + timedelta(hours=24):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if new_punch_in is None:
        derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        derived = {
            "work_hours": compute_work_hours(new_punch_in, new_punch_out) if new_punch_out else None,
            "late_minutes": compute_late_minutes(new_punch_in, employee["shift_start"], date),
            "overtime_minutes": compute_overtime(
                new_punch_out,
                employee["shift_start"],
                employee["shift_end"],
                date,
            )
            if new_punch_out
            else 0,
            "half_day": bool(new_punch_out and compute_work_hours(new_punch_in, new_punch_out) < 4.50),
        }

    new_values = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        **derived,
    }
    changes = {}
    for field, new_value in new_values.items():
        old_value = record.get(field)
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, "the correction changes nothing")

    history = record.get("history", [])
    update = {**new_values, "history": [*history, {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }]}
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "history": history},
        {"$set": update},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "attendance record was changed concurrently")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_document(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    try:
        first_text, last_text = month_range(month)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    first_day = max(parse_date(first_text), parse_date(employee["joined_on"]))
    last_day = parse_date(last_text)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_text, "$lte": last_text}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    result = next(iter(db.attendance_logs.aggregate(pipeline)), {})
    days = working_days(first_day, last_day)
    percentage = round_rate(result.get("present_days", 0) / days * 100) if days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": result.get("present_days", 0),
        "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": percentage,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    try:
        first_text, last_text = month_range(month)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    employee_filter: dict[str, Any] = {"joined_on": {"$lte": last_text}}
    if department:
        employee_filter["department"] = department
    employees = list(db.employees.find(employee_filter, {"_id": 0, "emp_code": 1, "department": 1}))
    headcounts: dict[str, int] = {}
    employee_codes = set()
    for employee in employees:
        headcounts[employee["department"]] = headcounts.get(employee["department"], 0) + 1
        employee_codes.add(employee["emp_code"])

    pipeline = [
        {"$match": {"emp_code": {"$in": list(employee_codes)}, "date": {"$gte": first_text, "$lte": last_text}}},
        {"$lookup": {
            "from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"
        }},
        {"$unwind": "$employee"},
        {"$group": {
            "_id": "$employee.department",
            "present_days": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0,
            ]}},
            "work_hours": {"$push": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                ]}, "$work_hours", "$$REMOVE"
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    summaries = {item["_id"]: item for item in db.attendance_logs.aggregate(pipeline)}
    items = []
    for name in sorted(headcounts):
        summary = summaries.get(name, {})
        hours = summary.get("work_hours", [])
        items.append({
            "department": name,
            "headcount": headcounts[name],
            "present_days": summary.get("present_days", 0),
            "avg_work_hours": round_half_up(sum(hours) / len(hours)) if hours else None,
            "late_count": summary.get("late_count", 0),
            "total_late_minutes": summary.get("total_late_minutes", 0),
            "leave_count": summary.get("leave_count", 0),
            "on_duty_count": summary.get("on_duty_count", 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    try:
        first_text, last_text = month_range(month)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    pipeline = [
        {"$match": {"date": {"$gte": first_text, "$lte": last_text}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.append({"$sort": {"total_late_minutes": -1, "_id": 1}})
    rows = list(db.attendance_logs.aggregate(pipeline))
    items = []
    previous_total = None
    rank = 0
    for position, row in enumerate(rows, 1):
        if row["total_late_minutes"] != previous_total:
            rank = position
            previous_total = row["total_late_minutes"]
        if rank > limit:
            break
        items.append({
            "rank": rank,
            "emp_code": row["_id"],
            "name": row["employee"]["name"],
            "department": row["employee"]["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    try:
        start = parse_date(from_date)
        end = parse_date(to_date)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if end < start or (end - start).days > 91:
        raise HTTPException(422, "trend range must be between 0 and 92 days")
    employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(404, "department not found")
    trend_pipeline = [
        {"$documents": [{"date_obj": datetime.combine(start, time.min, UTC)}]},
        {"$densify": {
            "field": "date_obj",
            "partitionByFields": [],
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [
                    datetime.combine(start, time.min, UTC),
                    datetime.combine(end + timedelta(days=1), time.min, UTC),
                ],
            },
        }},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj", "timezone": "UTC"}}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"trend_date": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$date", "$$trend_date"]},
                    {"$in": ["$emp_code", [e["emp_code"] for e in employees]]},
                ]}}},
                {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "present_count": {"$reduce": {
                "input": "$logs", "initialValue": 0,
                "in": {"$add": ["$$value", {"$cond": [
                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1]}, 0,
                ]}]},
            }},
            "late_count": {"$reduce": {
                "input": "$logs", "initialValue": 0,
                "in": {"$add": ["$$value", {"$cond": [
                    {"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0,
                ]}]},
            }},
        }},
    ]
    trend_result = db.command("aggregate", 1, pipeline=trend_pipeline, cursor={})
    logs = trend_result["cursor"]["firstBatch"]
    by_date = {item["date"]: item for item in logs}
    rows = []
    for offset in range((end - start).days + 1):
        current = start + timedelta(days=offset)
        current_text = current.isoformat()
        headcount = sum(parse_date(employee["joined_on"]) <= current for employee in employees)
        values = by_date.get(current_text, {})
        is_working = current.weekday() < 5
        present = values.get("present_count", 0)
        rate = round_rate(present / headcount) if is_working and headcount else None
        rows.append({
            "date": current_text,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": present,
            "late_count": values.get("late_count", 0),
            "attendance_rate": rate,
            "moving_avg_7d": None,
        })
        window = [row["attendance_rate"] for row in rows[-7:] if row["attendance_rate"] is not None]
        rows[-1]["moving_avg_7d"] = round_rate(sum(window) / len(window)) if window else None
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, date_from: Optional[str] = None,
                     date_to: Optional[str] = None, status: Optional[str] = None,
                     from_date: Optional[str] = Query(None, alias="from"),
                     to_date: Optional[str] = Query(None, alias="to"),
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        explain = db.command("explain", {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(explain))}
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        try:
            first_text, last_text = month_range(month)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_text, "$lte": last_text}}},
            {"$group": {"_id": None, "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}},
        ]
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        try:
            first_text, last_text = month_range(month)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        employee_filter: dict[str, Any] = {"joined_on": {"$lte": last_text}}
        if department:
            employee_filter["department"] = department
        employee_codes = [item["emp_code"] for item in db.employees.find(employee_filter, {"_id": 0, "emp_code": 1})]
        pipeline = [
            {"$match": {"emp_code": {"$in": employee_codes}, "date": {"$gte": first_text, "$lte": last_text}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$group": {"_id": "$employee.department", "records": {"$sum": 1}}},
        ]
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        try:
            first_text, last_text = month_range(month)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        pipeline = [
            {"$match": {"date": {"$gte": first_text, "$lte": last_text}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
        ]
        if department:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.append({"$sort": {"total_late_minutes": -1, "_id": 1}})
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        try:
            start = parse_date(from_date)
            end = parse_date(to_date)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if end < start or (end - start).days > 91:
            raise HTTPException(422, "trend range must be between 0 and 92 days")
        employee_codes = [item["emp_code"] for item in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
        pipeline = [
            {"$documents": [{"date_obj": datetime.combine(start, time.min, UTC)}]},
            {"$densify": {"field": "date_obj", "partitionByFields": [], "range": {
                "step": 1, "unit": "day", "bounds": [
                    datetime.combine(start, time.min, UTC),
                    datetime.combine(end + timedelta(days=1), time.min, UTC),
                ]
            }}},
            {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj", "timezone": "UTC"}}}},
            {"$lookup": {"from": "attendance_logs", "let": {"trend_date": "$date"}, "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$date", "$$trend_date"]},
                    {"$in": ["$emp_code", employee_codes]},
                ]}}},
            ], "as": "logs"}},
        ]
    else:
        raise HTTPException(422, "unknown endpoint")

    explain_pipeline = pipeline
    if endpoint == "department_trend":
        explain_pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}, "emp_code": {"$in": employee_codes}}},
            {"$group": {"_id": "$date", "records": {"$sum": 1}}},
        ]
    explain = db.command("explain", {
        "aggregate": "attendance_logs",
        "pipeline": explain_pipeline,
        "cursor": {},
    }, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(explain))}

