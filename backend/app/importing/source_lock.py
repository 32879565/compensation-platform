"""Transaction lock for the append-only legacy salary evidence dataset."""

from hashlib import blake2b

from sqlalchemy import func, select
from sqlalchemy.orm import Session

_LOCK_PAYLOAD = b"compensation-platform:legacy-salary-dataset"
LEGACY_SALARY_DATASET_LOCK_KEY = int.from_bytes(
    blake2b(_LOCK_PAYLOAD, digest_size=8).digest(),
    byteorder="big",
    signed=True,
)


def lock_legacy_salary_dataset(session: Session) -> None:
    """Serialize legacy/import writes with catalog preview and application."""

    session.execute(select(func.pg_advisory_xact_lock(LEGACY_SALARY_DATASET_LOCK_KEY))).one()
