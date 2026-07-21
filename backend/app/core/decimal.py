"""Canonical Decimal serialization for JSON audit and external API payloads."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import overload

_TWO_PLACES = Decimal("0.01")


@overload
def decimal_text(value: Decimal) -> str: ...


@overload
def decimal_text(value: None) -> None: ...


def decimal_text(value: Decimal | None) -> str | None:
    """Serialize a monetary/attendance Decimal with its required two places.

    PostgreSQL ``NUMERIC(..., 2)`` normally restores the scale when values are
    fetched from the database, but request-sourced Decimals can be ``20`` or
    ``5100``.  Audit JSON and payslip JSON must preserve the same fixed-scale
    representation regardless of where the value originated.
    """

    if value is None:
        return None
    return format(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP), "f")
