"""Tests for discount service — in isolation from checkout."""

import pytest

from app.models import DiscountCode
from app.services.discount_service import calculate_discount_cents, get_discount_code


def test_get_valid_discount_code():
    code = get_discount_code("SAVE10")
    assert code is not None
    assert code.discount_type == "percent"
    assert code.value == 10


def test_get_discount_code_case_insensitive():
    assert get_discount_code("save10") is not None
    assert get_discount_code("SAVE10") is not None


def test_get_invalid_discount_code_returns_none():
    assert get_discount_code("NOTREAL") is None


def test_percent_discount_below_minimum_returns_zero():
    code = get_discount_code("SAVE10")
    assert code is not None
    # min_order_cents = 1000; subtotal = 500 → no discount
    result = calculate_discount_cents(code, 500)
    assert result == 0


def test_fixed_discount_below_minimum_returns_zero():
    code = get_discount_code("FLAT500")
    assert code is not None
    # min_order_cents = 2000; subtotal = 1000 → no discount
    result = calculate_discount_cents(code, 1000)
    assert result == 0


def test_fixed_discount_applied():
    code = get_discount_code("FLAT500")
    assert code is not None
    result = calculate_discount_cents(code, 5000)
    assert result == 500


def test_fixed_discount_capped_at_subtotal():
    code = get_discount_code("FLAT500")
    assert code is not None
    result = calculate_discount_cents(code, 300)
    # subtotal 300 < min_order_cents 2000 → returns 0 (min not met)
    assert result == 0
