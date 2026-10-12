# Employee Attendance and Analytics API

FastAPI service for employee creation, attendance punches and corrections, and MongoDB-backed attendance analytics. All application implementation is in `app/main.py`.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (the assignment's target is MongoDB 7)

## Install and configure (Windows PowerShell)

```powershell
python --version                 # Python 3.11 or newer; your Python 3.13 is fine
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Start a local MongoDB 7 container if you do not already have a MongoDB instance:

```powershell
docker run -d --rm --name hrone-mongodb -p 27017:27017 mongo:7
```

Set the connection for the current PowerShell session. Real process environment variables take precedence over a local `.env` file.

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

The assignment's sample data files are intentionally not included in this repository. If you have the original candidate kit locally and choose to run `sample_seed.py`, point it at a disposable database first: it deletes existing documents in the configured `employees` and `attendance_logs` collections before inserting the sample records.

## Start and verify

From the repository root, run the required command:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

The app creates its indexes at startup. `GET http://localhost:8000/health` returns `{"status":"ok"}` when MongoDB responds and 503 otherwise. Interactive API documentation is at `http://localhost:8000/docs`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests
```

Tests use the configured MongoDB database and create/clean employee records with `EMP9901` and `EMP9902`; choose a disposable test database if those codes are already in use.

## Design notes and verification limits

All API instants are strict epoch milliseconds; MongoDB stores UTC datetimes. Dates and shift times are interpreted in IST. Unique natural-key indexes and conditional updates protect the attendance state transitions and correction history. The monthly summaries, department summary, leaderboard, and daily trend are aggregation pipelines; the trend uses `$densify`, `$fill`, and a database window for its moving average. Explain routes call MongoDB with `executionStats` verbosity.

The included tests exercise real MongoDB operations and temporary edge cases; all 8 tests pass locally. A separate synthetic 100,000-record dataset was used to check the five explain routes and attendance list filters on MongoDB 8.3.7; each tested plan used `IXSCAN` and contained no `COLLSCAN`. The department trend returned the expected daily rows in that check. MongoDB 6.0 compatibility has not been verified locally. See `REVIEW.md` and `DECISIONS.md` for the code review and design answers.
