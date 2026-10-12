# Implementation decisions

1. **Indexes.** Employee indexes are unique `emp_code`, `(department, joined_on, emp_code)`, and `(joined_on, emp_code)` for identity and eligible headcount. Attendance uses unique `(emp_code, date)` for daily uniqueness/lookups, `(date desc, emp_code asc)` for listing, and `(date, status, emp_code)` for filtered lists. I omitted a status-only index because no main query filters on status alone.

2. **Punch-in race.** Both requests can read the same employee and compute the same date. The unique attendance index accepts one insert; MongoDB rejects the other, and the API maps that duplicate-key error to 409.

3. **Ties.** `$rank` gives equal totals the same competition rank. The pipeline applies `rank <= limit` afterward, returning everyone tied at the cutoff, ordered by `emp_code`.

4. **Headcount.** Department summary starts with employees who joined by month end, then looks up each employee's logs. An empty lookup still contributes that employee to the department group.

5. **100x data.** I would benchmark production-shaped data, then consider sharding attendance by employee/date, archiving older months, and pre-aggregating finalized monthly analytics while keeping live attendance authoritative.
