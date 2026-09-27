"""Tests for cart service."""

import pytest

from app.services.cart_service import add_item, clear_carts, create_cart, get_cart


@pytest.fixture(autouse=True)
def reset_carts():
    yield
    clear_carts()


def test_create_cart_returns_empty_cart():
    cart = create_cart()
    assert cart.items == []
    assert cart.subtotal_cents == 0
    assert cart.total_cents == 0


def test_add_item_updates_subtotal():
    cart = create_cart()
    updated = add_item(cart.id, 1, 1)  # Wireless Headphones: 9999 cents
    assert updated is not None
    assert updated.subtotal_cents == 9999


def test_add_multiple_items():
    cart = create_cart()
    add_item(cart.id, 1, 1)  # 9999
    updated = add_item(cart.id, 2, 2)  # 1499 * 2 = 2998
    assert updated is not None
    assert updated.subtotal_cents == 9999 + 2998


def test_add_same_item_twice_merges_quantity():
    cart = create_cart()
    add_item(cart.id, 2, 1)
    updated = add_item(cart.id, 2, 2)
    assert updated is not None
    assert len(updated.items) == 1
    assert updated.items[0].quantity == 3


def test_get_cart_returns_cart():
    cart = create_cart()
    retrieved = get_cart(cart.id)
    assert retrieved is not None
    assert retrieved.id == cart.id


def test_get_nonexistent_cart_returns_none():
    assert get_cart("nonexistent") is None


def test_add_item_to_nonexistent_cart_returns_none():
    result = add_item("bad-id", 1, 1)
    assert result is None


def test_add_nonexistent_product_returns_none():
    cart = create_cart()
    result = add_item(cart.id, 9999, 1)
    assert result is None
