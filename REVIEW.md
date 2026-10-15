# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Always returned 200 without checking MongoDB. | Stop MongoDB and call `/health`; it still reported ready. | Added a MongoDB `ping` and return 503 on failure. |
| 2 | `create_employee` | Check-then-insert allowed duplicate employee codes under concurrency. | Send two creates at the same time. | Added a unique index and handled `DuplicateKeyError`. |
| 3 | `punch_in` | Unknown employees caused a lookup error, and duplicate attendance had a race. | Punch in with an unknown code or send concurrent requests. | Added 404 handling and a unique `(emp_code, date)` index. |
| 4 | Employee and punch models | Dates, times, status values, and epoch ranges were not validated. | Send malformed dates, invalid shifts, or an unsupported status. | Added Pydantic validation and shared parsing helpers. |
| 5 | Time helpers | Local naive datetimes broke IST dates, overnight shifts, and API timestamp format. | Test a punch around midnight or inspect a response timestamp. | Store UTC BSON datetimes and convert to/from epoch milliseconds at the boundary. |
| 6 | `list_employees` | Page one skipped the first page and `total` ignored the department filter. | Request page 1 or filter by department. | Corrected the skip formula, filtered count, and sort. |
| 7 | `list_attendance` | Loaded all matching records into Python and sorted there. | Use a large collection and observe memory/time. | Moved filtering, sorting, and pagination into MongoDB. |
| 8 | Missing routes | Punch-out, corrections, analytics, and explain were absent. | Compare the route list with `openapi.yaml`. | Implemented the missing route families. |

I kept the original MongoDB document model and used stored derived values for analytics, because the contract says seeded values are authoritative.

Also note anything you looked at and decided was **not** a defect, and why.
