# DECISIONS.md

1. **Indexes.** `employees.emp_code` has a unique index because it is the business identifier. `attendance_logs` has a unique `(emp_code, date)` index: it enforces the natural-key rule and protects against concurrent duplicate punch-ins. I also added indexes for employee/date lookups and date/employee listing. I did not index every derived field because analytics group and sum those values; extra indexes would slow writes without helping the main filters.

2. **Punch-in race.** A preliminary `find_one` is useful for readability but is not the guarantee. Two requests can pass it together. The unique index makes MongoDB the final authority: one insert succeeds and the other raises `DuplicateKeyError`, which the API translates to 409.

3. **Ties.** The leaderboard sorts by total late minutes descending and `emp_code` ascending for stable output. Rank changes only when the total changes, so totals of 80 can both be rank 2 and the next total becomes rank 4. The limit is applied to rank, not array position, so a cutoff tie is not silently dropped.

4. **Headcount.** Department reporting starts with employees whose `joined_on` is on or before the month end. Attendance is merged afterward, so an eligible employee with no log still contributes to headcount.

5. **100x scale.** I would keep writes in the transactional collections but generate daily or monthly summary documents asynchronously. Reports could then read pre-aggregated data, with MongoDB indexes and a reporting read preference handling operational queries.
