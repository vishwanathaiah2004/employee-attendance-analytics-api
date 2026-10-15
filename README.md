# Employee Attendance & Analytics API

A FastAPI and MongoDB API for employee attendance, punch-in/punch-out,
manual corrections, audit history, and attendance analytics.

## Features

- Employee creation and paginated listing
- Punch-in and punch-out with IST and overnight-shift handling
- Late minutes, overtime, work-hours, and half-day calculations
- Manual corrections with append-only audit history
- Employee monthly summaries
- Department summaries with zero-log employees included in headcount
- Late-arrival leaderboard with standard competition ranking
- Department daily trends and MongoDB explain plans

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer, or MongoDB Atlas

## Setup

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Create a local `.env` file. Never commit it:

```env
MONGO_URI=mongodb+srv://USERNAME:PASSWORD@CLUSTER.mongodb.net/?retryWrites=true&w=majority
MONGO_DB=attendance_db
```

The application creates its required indexes when it starts.

## Load sample data

This clears and reloads the two sample collections:

```powershell
python sample_seed.py
```

## Run the API

```powershell
python -m uvicorn app.main:app --port 8000 --reload
```

Interactive API documentation is available at:

```text
http://127.0.0.1:8000/docs
```

## Useful checks

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/employees
Invoke-RestMethod "http://127.0.0.1:8000/analytics/employees/EMP0001/monthly?month=2026-07"
```

Compile check:

```powershell
python -m py_compile app\main.py
```

## Design notes

API timestamps are epoch milliseconds. MongoDB stores them as BSON datetimes.
Calendar dates remain `YYYY-MM-DD` strings, and shift times remain `HH:MM`
strings. MongoDB unique indexes protect employee codes and the natural
attendance key `(emp_code, date)` from concurrent duplicates.

See [REVIEW.md](REVIEW.md) for defects found in the starter and
[DECISIONS.md](DECISIONS.md) for the main design decisions.

## Submission

Submit a public repository containing `app/main.py`, `requirements.txt`,
`README.md`, `REVIEW.md`, and `DECISIONS.md`. Sample data files may also be
included. Do not commit `.env`, credentials, connection strings, or a
Dockerfile.
