"""ShopDemo — FastAPI entry point."""

from fastapi import FastAPI

from app.routes.cart import router as cart_router
from app.routes.checkout import router as checkout_router
from app.routes.discount import router as discount_router
from app.routes.products import router as products_router

app = FastAPI(title="ShopDemo", version="1.0.0")

app.include_router(products_router, prefix="/products", tags=["products"])
app.include_router(cart_router, prefix="/cart", tags=["cart"])
app.include_router(discount_router, prefix="/discount", tags=["discount"])
app.include_router(checkout_router, prefix="/checkout", tags=["checkout"])
