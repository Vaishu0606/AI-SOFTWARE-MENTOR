"""Cart routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models import Cart
from app.services import cart_service

router = APIRouter()


class AddItemRequest(BaseModel):
    product_id: int
    quantity: int = 1


@router.post("/", response_model=Cart)
async def create_cart() -> Cart:
    return cart_service.create_cart()


@router.get("/{cart_id}", response_model=Cart)
async def get_cart(cart_id: str) -> Cart:
    cart = cart_service.get_cart(cart_id)
    if not cart:
        raise HTTPException(status_code=404, detail="Cart not found")
    return cart


@router.post("/{cart_id}/items", response_model=Cart)
async def add_item(cart_id: str, body: AddItemRequest) -> Cart:
    cart = cart_service.add_item(cart_id, body.product_id, body.quantity)
    if not cart:
        raise HTTPException(status_code=404, detail="Cart or product not found")
    return cart
