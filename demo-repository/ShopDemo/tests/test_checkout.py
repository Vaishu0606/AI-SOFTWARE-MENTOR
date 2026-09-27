"""Tests for checkout service — without discount codes."""

import pytest

from app.models import Cart, CartItem
from app.services.checkout_service import checkout


def _make_cart(subtotal: int, discount: int = 0) -> Cart:
    total = max(0, subtotal - discount)
    return Cart(
        id="test-cart",
        items=[
            CartItem(
                product_id=1,
                name="Test Product",
                quantity=1,
                unit_price_cents=subtotal,
            )
        ],
        subtotal_cents=subtotal,
        discount_cents=discount,
        total_cents=total,
    )


def test_checkout_creates_order():
    cart = _make_cart(subtotal=9999)
    order = checkout(cart)
    assert order is not None
    assert order.status == "confirmed"
    assert order.total_cents == 9999


def test_checkout_empty_cart_returns_none():
    cart = Cart(
        id="empty",
        items=[],
        subtotal_cents=0,
        discount_cents=0,
        total_cents=0,
    )
    assert checkout(cart) is None


def test_checkout_preserves_total():
    cart = _make_cart(subtotal=5000, discount=0)
    order = checkout(cart)
    assert order is not None
    assert order.total_cents == 5000


def test_checkout_order_id_starts_with_ord():
    cart = _make_cart(subtotal=1000)
    order = checkout(cart)
    assert order is not None
    assert order.order_id.startswith("ORD-")
