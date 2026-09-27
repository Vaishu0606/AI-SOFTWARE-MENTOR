"""ShopDemo domain models."""

from __future__ import annotations

from pydantic import BaseModel


class Product(BaseModel):
    id: int
    name: str
    price_cents: int  # price stored in integer cents to avoid float rounding


class CartItem(BaseModel):
    product_id: int
    name: str
    quantity: int
    unit_price_cents: int


class Cart(BaseModel):
    id: str
    items: list[CartItem]
    subtotal_cents: int
    discount_cents: int
    total_cents: int


class DiscountCode(BaseModel):
    code: str
    discount_type: str   # "percent" or "fixed"
    value: int           # percent (0-100) or fixed cents
    min_order_cents: int


class Order(BaseModel):
    order_id: str
    cart_id: str
    total_cents: int
    status: str
