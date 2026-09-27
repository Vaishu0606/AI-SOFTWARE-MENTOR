"""In-memory product catalog."""

from app.models import Product

PRODUCTS: dict[int, Product] = {
    1: Product(id=1, name="Wireless Headphones", price_cents=9999),
    2: Product(id=2, name="USB-C Cable", price_cents=1499),
    3: Product(id=3, name="Laptop Stand", price_cents=4999),
    4: Product(id=4, name="Mechanical Keyboard", price_cents=12999),
}
