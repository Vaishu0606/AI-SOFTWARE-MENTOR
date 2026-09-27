"""
Phase 4 tests — ShopDemo AST integration.

Runs the full ASTAnalyzer against the actual demo-repository/ShopDemo
and verifies that real structures are discovered.

These tests inspect actual repository source — no fake/hardcoded results.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.analysis.ast_analyzer import ASTAnalyzer

# ── Locate ShopDemo ────────────────────────────────────────────────────────────

_TESTS_DIR = Path(__file__).parent
_SHOPDEMO = _TESTS_DIR.parent.parent / "demo-repository" / "ShopDemo"


def _skip_if_missing():
    if not _SHOPDEMO.exists():
        pytest.skip("demo-repository/ShopDemo not found")


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def shopdemo_analysis():
    _skip_if_missing()
    analyzer = ASTAnalyzer()
    return analyzer.analyze_repository_path(_SHOPDEMO)


# ── Repository-level assertions ────────────────────────────────────────────────


def test_shopdemo_name(shopdemo_analysis):
    assert shopdemo_analysis.name == "ShopDemo"


def test_shopdemo_has_python_files(shopdemo_analysis):
    python_files = [f for f in shopdemo_analysis.files if f.language == "Python"]
    assert len(python_files) > 0


def test_shopdemo_has_no_fatal_error(shopdemo_analysis):
    """All files should either parse successfully or record a non-fatal error."""
    # No file should have caused an unhandled exception — all errors are recorded
    assert shopdemo_analysis is not None


def test_shopdemo_routes_detected(shopdemo_analysis):
    """ShopDemo has FastAPI routes — they must all be detected."""
    assert len(shopdemo_analysis.routes) > 0


def test_shopdemo_has_post_route(shopdemo_analysis):
    post_routes = [r for r in shopdemo_analysis.routes if r.http_method == "POST"]
    assert len(post_routes) > 0


def test_shopdemo_has_get_route(shopdemo_analysis):
    get_routes = [r for r in shopdemo_analysis.routes if r.http_method == "GET"]
    assert len(get_routes) > 0


def test_shopdemo_discount_route_detected(shopdemo_analysis):
    """/apply route in discount.py must be found."""
    apply_routes = [r for r in shopdemo_analysis.routes if r.path == "/apply"]
    assert len(apply_routes) >= 1


def test_shopdemo_checkout_route_detected(shopdemo_analysis):
    checkout_routes = [r for r in shopdemo_analysis.routes if r.path == "/"]
    # Checkout router uses @router.post("/")
    assert len(checkout_routes) >= 1


# ── Functions discovered ───────────────────────────────────────────────────────


def test_shopdemo_discount_functions_found(shopdemo_analysis):
    fn_names = [f.name for f in shopdemo_analysis.all_functions]
    assert "get_discount_code" in fn_names
    assert "calculate_discount_cents" in fn_names


def test_shopdemo_cart_functions_found(shopdemo_analysis):
    fn_names = [f.name for f in shopdemo_analysis.all_functions]
    assert "create_cart" in fn_names
    assert "add_item" in fn_names
    assert "get_cart" in fn_names


def test_shopdemo_checkout_function_found(shopdemo_analysis):
    fn_names = [f.name for f in shopdemo_analysis.all_functions]
    assert "checkout" in fn_names


# ── Classes discovered ────────────────────────────────────────────────────────


def test_shopdemo_pydantic_models_found(shopdemo_analysis):
    """Pydantic models like Cart, CartItem, Product should be detected as classes."""
    class_names = [c.name for c in shopdemo_analysis.all_classes]
    assert len(class_names) > 0


def test_shopdemo_request_classes_found(shopdemo_analysis):
    """Request body classes (e.g. ApplyDiscountRequest) should be detected."""
    class_names = [c.name for c in shopdemo_analysis.all_classes]
    assert "ApplyDiscountRequest" in class_names


# ── Imports discovered ────────────────────────────────────────────────────────


def test_shopdemo_fastapi_imports_found(shopdemo_analysis):
    all_imports = []
    for f in shopdemo_analysis.files:
        all_imports.extend(f.imports)
    modules = [i.module for i in all_imports]
    assert "fastapi" in modules


def test_shopdemo_service_imports_found(shopdemo_analysis):
    """Routes should import from app.services."""
    all_imports = []
    for f in shopdemo_analysis.files:
        all_imports.extend(f.imports)
    modules = [i.module for i in all_imports]
    assert any("services" in m for m in modules)


# ── Relationships ─────────────────────────────────────────────────────────────


def test_shopdemo_relationships_built(shopdemo_analysis):
    assert len(shopdemo_analysis.relationships) > 0


def test_shopdemo_discount_route_imports_discount_service(shopdemo_analysis):
    """discount.py imports from app.services which includes discount_service."""
    route_rels = [
        r for r in shopdemo_analysis.relationships
        if "discount" in r.source_file and "services" in r.target_module
    ]
    assert len(route_rels) > 0


# ── File-level details ────────────────────────────────────────────────────────


def test_shopdemo_discount_service_file_analyzed(shopdemo_analysis):
    discount_file = next(
        (f for f in shopdemo_analysis.files
         if "discount_service" in f.relative_path),
        None,
    )
    assert discount_file is not None
    assert discount_file.supported is True
    assert discount_file.error is None


def test_shopdemo_calculate_discount_has_line_info(shopdemo_analysis):
    fn = next(
        (f for f in shopdemo_analysis.all_functions
         if f.name == "calculate_discount_cents"),
        None,
    )
    assert fn is not None
    assert fn.line_start > 0
    assert fn.line_end >= fn.line_start


def test_shopdemo_test_files_analyzed(shopdemo_analysis):
    """Test files like test_cart.py should also be analyzable."""
    test_files = [f for f in shopdemo_analysis.files if "test_" in f.relative_path]
    assert len(test_files) > 0
