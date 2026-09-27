"""Discount routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models import Cart
from app.services import cart_service, discount_service

router = APIRouter()


class ApplyDiscountRequest(BaseModel):
    cart_id: str
    code: str


@router.post("/apply", response_model=Cart)
async def apply_discount(body: ApplyDiscountRequest) -> Cart:
    cart = cart_service.get_cart(body.cart_id)
    if not cart:
        raise HTTPException(status_code=404, detail="Cart not found")

    discount_code = discount_service.get_discount_code(body.code)
    if not discount_code:
        raise HTTPException(status_code=400, detail="Invalid discount code")

    discount_cents = discount_service.calculate_discount_cents(
        discount_code, cart.subtotal_cents
    )

    updated_cart = cart_service.apply_discount(body.cart_id, discount_cents)
    if not updated_cart:
        raise HTTPException(status_code=500, detail="Failed to apply discount")

    return updated_cart
