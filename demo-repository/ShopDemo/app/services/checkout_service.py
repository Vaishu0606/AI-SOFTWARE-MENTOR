"""Checkout service — validates cart and creates orders."""

from __future__ import annotations

import uuid

from app.models import Cart, Order


def checkout(cart: Cart) -> Order | None:
    """Process a checkout for the given cart.

    Returns an Order if successful, or None if the cart is empty or invalid.
    """
    if not cart.items:
        return None

    if cart.total_cents < 0:
        return None

    order_id = f"ORD-{str(uuid.uuid4())[:8].upper()}"
    return Order(
        order_id=order_id,
        cart_id=cart.id,
        total_cents=cart.total_cents,
        status="confirmed",
    )
