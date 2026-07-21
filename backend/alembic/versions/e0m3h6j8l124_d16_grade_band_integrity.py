"""D16: job-grade lifecycle concurrency and salary-band integrity.

Revision ID: e0m3h6j8l124
Revises: d9l2g5i7k013
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e0m3h6j8l124"
down_revision: str | None = "d9l2g5i7k013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fail_on_dirty_legacy_data() -> None:
    bind = op.get_bind()

    blank_grade = bind.execute(sa.text("""
        SELECT id
        FROM job_grade
        WHERE btrim(code) = '' OR btrim(name) = ''
        LIMIT 1
        """)).first()
    if blank_grade is not None:
        raise RuntimeError("job_grade contains blank code or name values; repair them before D16")

    duplicate_band = bind.execute(sa.text("""
        SELECT job_grade_id, effective_from, count(*)
        FROM salary_band
        WHERE is_deleted = false
        GROUP BY job_grade_id, effective_from
        HAVING count(*) > 1
        LIMIT 1
        """)).first()
    if duplicate_band is not None:
        raise RuntimeError(
            "salary_band contains duplicate active grade/effective-date rows; "
            "resolve them before D16"
        )

    invalid_band = bind.execute(sa.text("""
        SELECT id
        FROM salary_band
        WHERE band_min = 'NaN'::numeric
           OR band_mid = 'NaN'::numeric
           OR band_max = 'NaN'::numeric
           OR band_min < 0
           OR band_mid < 0
           OR band_max < 0
           OR band_min > band_mid
           OR band_mid > band_max
        LIMIT 1
        """)).first()
    if invalid_band is not None:
        raise RuntimeError(
            "salary_band contains invalid negative or unordered amounts; repair them before D16"
        )


def upgrade() -> None:
    # Do not silently choose a duplicate winner or normalize historical money.
    # Operations must repair source data with an auditable decision first.
    _fail_on_dirty_legacy_data()

    op.add_column(
        "job_grade",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_check_constraint(
        "ck_job_grade_code_nonblank",
        "job_grade",
        "btrim(code) <> ''",
    )
    op.create_check_constraint(
        "ck_job_grade_name_nonblank",
        "job_grade",
        "btrim(name) <> ''",
    )
    op.create_check_constraint(
        "ck_job_grade_version_positive",
        "job_grade",
        "version > 0",
    )
    op.create_check_constraint(
        "ck_salary_band_not_nan",
        "salary_band",
        "band_min <> 'NaN'::numeric AND "
        "band_mid <> 'NaN'::numeric AND "
        "band_max <> 'NaN'::numeric",
    )
    op.create_check_constraint(
        "ck_salary_band_nonnegative",
        "salary_band",
        "band_min >= 0 AND band_mid >= 0 AND band_max >= 0",
    )
    op.create_check_constraint(
        "ck_salary_band_order",
        "salary_band",
        "band_min <= band_mid AND band_mid <= band_max",
    )
    op.create_index(
        "uq_salary_band_grade_effective_from_active",
        "salary_band",
        ["job_grade_id", "effective_from"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_salary_band_grade_effective_from_active",
        table_name="salary_band",
    )
    op.drop_constraint("ck_salary_band_order", "salary_band", type_="check")
    op.drop_constraint("ck_salary_band_nonnegative", "salary_band", type_="check")
    op.drop_constraint("ck_salary_band_not_nan", "salary_band", type_="check")
    op.drop_constraint("ck_job_grade_version_positive", "job_grade", type_="check")
    op.drop_constraint("ck_job_grade_name_nonblank", "job_grade", type_="check")
    op.drop_constraint("ck_job_grade_code_nonblank", "job_grade", type_="check")
    op.drop_column("job_grade", "version")
