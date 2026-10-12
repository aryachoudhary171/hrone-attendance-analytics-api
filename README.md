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

To load the supplied sample data:

```powershell
python sample_seed.py
```

**The sample seeder deletes existing documents in the configured `employees` and `attendance_logs` collections before inserting the examples. Use a disposable database.**

## Start and verify

From the repository root, run the required command:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

The app creates its indexes at startup. `GET http://localhost:8000/health` returns `{"status":"ok"}` when MongoDB responds and 503 otherwise. Interactive API documentation is at `http://localhost:8000/docs`.

## Tests

```powershell
python -m pytest -q tests
```

Tests use the configured MongoDB database and create/clean employee records with `EMP9901` and `EMP9902`; choose a disposable test database if those codes are already in use.

## Design notes and verification limits

All API instants are strict epoch milliseconds; MongoDB stores UTC datetimes. Dates and shift times are interpreted in IST. Unique natural-key indexes and conditional updates protect the attendance state transitions and correction history. The monthly summaries, department summary, leaderboard, and daily trend are aggregation pipelines; the trend uses `$densify`, `$fill`, and a database window for its moving average. Explain routes call MongoDB with `executionStats` verbosity.

The included tests exercise real MongoDB operations on the supplied small sample plus temporary edge-case records. The local verification used MongoDB 8.3.7 and did not benchmark the target 100,000-record dataset or MongoDB 6.0 specifically. See `REVIEW.md` and `DECISIONS.md` for the code review and design answers.
