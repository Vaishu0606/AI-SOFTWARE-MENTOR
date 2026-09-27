"""Product routes."""

from fastapi import APIRouter, HTTPException

from app.database import PRODUCTS
from app.models import Product

router = APIRouter()


@router.get("/", response_model=list[Product])
async def list_products() -> list[Product]:
    return list(PRODUCTS.values())


@router.get("/{product_id}", response_model=Product)
async def get_product(product_id: int) -> Product:
    product = PRODUCTS.get(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product
