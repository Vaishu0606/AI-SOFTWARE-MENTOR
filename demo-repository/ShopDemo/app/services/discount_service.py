"""Discount service — validates and calculates discount amounts.

NOTE: This module contains the intentional bug used for the AI Software Mentor
demonstration. The bug is in calculate_discount_cents().
"""

from __future__ import annotations

from app.models import DiscountCode

# Available discount codes
DISCOUNT_CODES: dict[str, DiscountCode] = {
    "SAVE10": DiscountCode(
        code="SAVE10",
        discount_type="percent",
        value=10,
        min_order_cents=1000,
    ),
    "FLAT500": DiscountCode(
        code="FLAT500",
        discount_type="fixed",
        value=500,
        min_order_cents=2000,
    ),
    "HALFOFF": DiscountCode(
        code="HALFOFF",
        discount_type="percent",
        value=50,
        min_order_cents=5000,
    ),
}


def get_discount_code(code: str) -> DiscountCode | None:
    return DISCOUNT_CODES.get(code.upper())


def calculate_discount_cents(code: DiscountCode, subtotal_cents: int) -> int:
    """Return the discount amount in cents for the given subtotal.

    For percent discounts: discount = subtotal * (value / 100)
    For fixed discounts:   discount = value (capped at subtotal)
    """
    if subtotal_cents < code.min_order_cents:
        return 0

    if code.discount_type == "percent":
        # BUG: integer division truncates before multiplication,
        # causing systematic under-discounting.
        # Correct:   int(subtotal_cents * code.value / 100)
        # Buggy:     int(subtotal_cents / 100) * code.value
        return int(subtotal_cents * code.value / 100)
    elif code.discount_type == "fixed":
        return min(code.value, subtotal_cents)
    return 0
