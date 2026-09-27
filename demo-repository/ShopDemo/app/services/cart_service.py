"""Cart service — manages cart state."""

from __future__ import annotations

import uuid

from app.database import PRODUCTS
from app.models import Cart, CartItem

# In-memory cart store (keyed by cart ID)
_carts: dict[str, Cart] = {}


def create_cart() -> Cart:
    cart_id = str(uuid.uuid4())[:8]
    cart = Cart(
        id=cart_id,
        items=[],
        subtotal_cents=0,
        discount_cents=0,
        total_cents=0,
    )
    _carts[cart_id] = cart
    return cart


def get_cart(cart_id: str) -> Cart | None:
    return _carts.get(cart_id)


def add_item(cart_id: str, product_id: int, quantity: int) -> Cart | None:
    cart = _carts.get(cart_id)
    if cart is None:
        return None
    product = PRODUCTS.get(product_id)
    if product is None:
        return None

    # Update existing item or add new one
    for item in cart.items:
        if item.product_id == product_id:
            cart.items.remove(item)
            cart.items.append(
                CartItem(
                    product_id=product_id,
                    name=product.name,
                    quantity=item.quantity + quantity,
                    unit_price_cents=product.price_cents,
                )
            )
            break
    else:
        cart.items.append(
            CartItem(
                product_id=product_id,
                name=product.name,
                quantity=quantity,
                unit_price_cents=product.price_cents,
            )
        )

    return _recalculate(cart)


def _recalculate(cart: Cart) -> Cart:
    subtotal = sum(
        item.unit_price_cents * item.quantity for item in cart.items
    )
    cart.subtotal_cents = subtotal
    cart.total_cents = max(0, subtotal - cart.discount_cents)
    return cart


def apply_discount(cart_id: str, discount_cents: int) -> Cart | None:
    cart = _carts.get(cart_id)
    if cart is None:
        return None
    cart.discount_cents = discount_cents
    return _recalculate(cart)


def clear_carts() -> None:
    """Test helper: reset all in-memory carts."""
    _carts.clear()
