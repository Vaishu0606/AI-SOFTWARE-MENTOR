"""Checkout routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models import Order
from app.services import cart_service, checkout_service

router = APIRouter()


class CheckoutRequest(BaseModel):
    cart_id: str


@router.post("/", response_model=Order)
async def checkout(body: CheckoutRequest) -> Order:
    cart = cart_service.get_cart(body.cart_id)
    if not cart:
        raise HTTPException(status_code=404, detail="Cart not found")

    order = checkout_service.checkout(cart)
    if not order:
        raise HTTPException(status_code=400, detail="Cart is empty or invalid")

    return order
