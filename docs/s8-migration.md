# S8 approval migration preflight

## Required salary-structure interval preflight

S8 adds a database-enforced invariant: an employee/component pair may have
only one open-ended salary-structure interval.  The migration intentionally
fails closed if historical rows violate it, rather than choosing a salary
record automatically.  Run this read-only query before scheduling the
migration:

```sql
SELECT
  employee_id,
  component_id,
  COUNT(*) AS open_interval_count,
  jsonb_agg(
    jsonb_build_object(
      'id', id,
      'effective_from', effective_from,
      'amount', amount,
      'created_at', created_at,
      'updated_at', updated_at
    )
    ORDER BY effective_from, id
  ) AS open_rows
FROM employee_salary_structure
WHERE effective_to IS NULL
GROUP BY employee_id, component_id
HAVING COUNT(*) > 1
ORDER BY employee_id, component_id;
```

The result must be empty before applying S8.  If it is not, stop the rollout:
HR/payroll must determine the intended effective-dated timeline from the
approved adjustment and audit evidence.  In a separately reviewed, backed-up
data-repair change, close only superseded rows with the evidence-derived
`effective_to` date; retain every original row and record the selected source
document, operator, and before/after values.  Re-run the query until it is
empty, rehearse the migration on a restored copy, then apply S8.  Do not pick
the newest row or bulk-close duplicates automatically.

After a successful migration, verify the new partial unique index exists:

```sql
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = current_schema()
  AND tablename = 'employee_salary_structure'
  AND indexname = 'uq_ess_open_employee_component';
```

Record the preflight result, repair approval, rehearsal result, and index
verification in the deployment change record.

## Legacy allowance classification

The S8 migration preserves historical allowance records without guessing
whether an existing allowance is fixed or floating.  It installs the database
constraint as `NOT VALID` for legacy rows, but the constraint is enforced for
all new or changed rows immediately.  Payroll calculation continues to block
any employee whose allowance is still unclassified; it never silently treats
it as zero or chooses a category.

After deploying S8, an administrator must classify every legacy allowance
through `PATCH /api/salary-components/{component_id}` with either
`{"allowance_kind":"FIXED"}` or `{"allowance_kind":"FLOATING"}`.  The
component list endpoint identifies the remaining `ALLOWANCE` rows with a null
`allowance_kind`.

Before validating the database constraint, have a controlled DBA session run
this read-only preflight query:

```sql
SELECT id, code, name
FROM salary_component_def
WHERE component_type = 'ALLOWANCE'
  AND allowance_kind IS NULL
ORDER BY id;
```

When it returns no rows, validate the existing constraint in the same planned
maintenance window:

```sql
ALTER TABLE salary_component_def
VALIDATE CONSTRAINT ck_salary_component_allowance_kind;
```

Record the output of the preflight query, the users who classified the rows,
and the validation time in the deployment change record.  Do not use a bulk
default to classify historical allowances: the fixed/floating decision is a
business fact and must have HR evidence.
