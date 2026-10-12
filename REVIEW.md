# Starter code review

The symptoms below are reproducible from the original starter implementation. Unless noted as an executed test, the example is a proposed input/reproduction; I did not claim to have run the old starter against these cases.

| # | Where | What is wrong | How to notice | How it was fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It replaces the punch-in's local clock fields without first converting the instant to IST, and cannot select the previous-day start for an overnight shift. | Run on a host configured outside IST or use a 00:30 punch for a 22:00–06:00 shift. | Convert the instant to IST, derive the attendance date, and compare UTC instants for the correct shift start. |
| 2 | `compute_work_hours` | Python `round` uses ties-to-even and binary floats, not decimal half-up. | A duration of exactly 4.505 hours should return 4.51. | Use `Decimal` and `ROUND_HALF_UP` after truncating instants to seconds. |
| 3 | `compute_overtime` | It never advances the shift end date for an overnight shift and can subtract a naive punch-out from an aware IST datetime. | A 22:00–06:00 shift with a 06:40 next-day punch-out should yield 40 minutes, not an exception or a negative value. | Build the shift end on the next day when required and compare normalized UTC instants. |
| 4 | `GET /health` | It always reports ready without contacting MongoDB. | Stop MongoDB and call `/health`; the starter still returns 200. | Ping MongoDB and return 503 on database errors. |
| 5 | `POST /employees` | Duplicate checking before insertion is racy and is not a database uniqueness guarantee. | Send two identical employee creates concurrently. | Create a unique `emp_code` index and map duplicate-key errors to 409. |
| 6 | `POST /employees` | `created_at=datetime.now()` is naive and does not establish UTC storage/API milliseconds. | Inspect the BSON timestamp under a non-UTC host timezone. | Store an aware UTC datetime and return epoch milliseconds. |
| 7 | `POST /employees` | The starter model omits the contract's format, length, date, email, and differing-shift validation. | Submit malformed employee codes, invalid dates, or equal shift times. | Add Pydantic constraints and validators matching the OpenAPI schema. |
| 8 | `GET /employees` | The offset uses `page * page_size`, skipping the first page. | Request page 1 with page size 2; it starts at row 3. | Use `(page - 1) * page_size`. |
| 9 | `GET /employees` | `total` counts all employees rather than the filtered query. | Filter by a department and compare total with the matching rows. | Count with the same filter used to fetch the page. |
| 10 | `GET /employees` | Pagination bounds are unchecked and the department filter is skipped for an empty string. | Try page 0, page size 101, or `department=`. | Validate page/page_size and apply a supplied department exactly. |
| 11 | `POST /attendance/punch-in` | Unknown employees cause a `None` dereference; status is not constrained to the three allowed values. | Submit an unknown code or status `ABSENT`. | Return 404 for missing employees and validate the request enum. |
| 12 | `POST /attendance/punch-in` | Epoch values are converted using host-local time, seconds/floats/strings are not safely rejected, and fractional seconds are retained. | Submit seconds, a float, or an instant near an IST midnight on a non-IST host. | Require strict epoch milliseconds, truncate to seconds, and derive the date in IST. |
| 13 | `POST /attendance/punch-in` | Check-then-insert permits duplicate records under concurrency. | Race two punch-ins for the same employee and IST attendance day. | Enforce the natural key with a unique `(emp_code, date)` index and map duplicate-key errors to 409. |
| 14 | `POST /attendance/punch-in` | It returns MongoDB's generated identifier as `id`, which the contract forbids exposing. | Inspect the successful response. | Omit `_id` and `id` from all responses. |
| 15 | `GET /attendance` | It loads every matching document into Python, sorts there, and slices after materialization. This is not practical at 100,000 records. | Inspect memory/query behavior on a large collection. | Filter, sort, skip, and limit in MongoDB; count separately with the same query. |
| 16 | `GET /attendance` | It returns Mongo `_id` as `id`, and omits legacy defaults for `half_day` and `history`. | Inspect a record with the sample's legacy shape. | Strip `_id` and normalize missing `half_day`/`history` at the API boundary. |
| 17 | `GET /attendance` | Sort order is only by date and is not the required deterministic date-descending, employee-ascending order. | Create multiple employees with records on the same date and repeat a page request. | Apply the exact two-field sort in MongoDB. |

## Reviewed behavior that was already correct

The starter used `emp_code` as the business identifier, addressed attendance with `(emp_code, date)`, and applied date and status filters to the attendance query. Those choices match the data model. The attendance count was over the filtered query, though it was achieved inefficiently by loading every matching document.
