"""
Shared pytest fixtures — Phase 6.

Each test gets its own SQLite file for the workflow database, so tests are
order-independent and cannot observe each other's runs or events.  The
in-memory stage registry is not shared: each test builds its own orchestrator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.db import configure_database, reset_database
from app.services.orchestrator import reset_default_orchestrator


@pytest.fixture(autouse=True)
def isolated_workflow_database(tmp_path: Path):
    """Point the workflow database at a per-test SQLite file."""
    reset_default_orchestrator()
    configure_database(tmp_path / "workflow-test.db")
    yield
    reset_default_orchestrator()
    reset_database()


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """A tiny FastAPI-style repository on disk for stage execution.

    Contains an entry point and a route module so that the Phase 4/5 layers
    have something real to analyse, plus a test file so the Phase 3 explorer
    has a test file to find.
    """
    repo = tmp_path / "SampleApp"
    (repo / "app").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)

    (repo / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from app.routes import router\n"
        "\n"
        "app = FastAPI()\n"
        "app.include_router(router)\n",
        encoding="utf-8",
    )
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        '@router.get("/items")\n'
        "async def list_items():\n"
        "    return []\n",
        encoding="utf-8",
    )
    (repo / "app" / "service.py").write_text(
        "def calculate_total(items):\n"
        "    return sum(item.price for item in items)\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_service.py").write_text(
        "def test_calculate_total():\n    assert True\n",
        encoding="utf-8",
    )
    return repo


#: The exact expression Phase 8 recognises as ``TRUNCATION_BEFORE_SCALING`` and
#: Phase 9 can rewrite.  Spelled as a constant so the tests that assert the
#: diagnosis agree with the tests that assert the fix, instead of each carrying
#: their own copy of a string that must match byte for byte.
BUGGY_DISCOUNT = "    return int(subtotal_cents / 100) * code.value"
FIXED_DISCOUNT = "    return int(subtotal_cents * code.value / 100)"


@pytest.fixture
def defective_repo(tmp_path: Path) -> Path:
    """A repository with the defect the whole pipeline is built to diagnose.

    ``sample_repo`` is deliberately defect-free, so a run against it honestly
    produces no fix — which is the right shape for most tests and useless for
    Phase 11, where the point is to approve, write and *verify* a real change.

    The layout is the minimum Phase 7 needs to connect the parts: a module
    holding the defect, a caller that imports it, and a test suite to run.  The
    test asserts the correct arithmetic, so it fails before the fix and passes
    after it.  That is the difference between "no regression" and "the diagnosed
    defect is fixed", and this fixture is the only place the two are told apart.

    It has no third-party dependencies on purpose: the controlled executor runs
    ``sys.executable -m pytest`` with plugin autoload disabled, so a fixture that
    needed FastAPI would be testing the environment rather than Phase 11.
    """
    repo = tmp_path / "DefectiveApp"
    (repo / "app" / "services").mkdir(parents=True)
    (repo / "app" / "routes").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)

    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "main.py").write_text(
        "from app.routes.checkout import checkout\n\n\ndef run():\n    return checkout\n",
        encoding="utf-8",
    )
    (repo / "app" / "services" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "services" / "discount_service.py").write_text(
        "def calculate_discount_cents(subtotal_cents, code):\n"
        "    \"\"\"Return the discount in cents for ``code`` percent off.\"\"\"\n"
        f"{BUGGY_DISCOUNT}\n",
        encoding="utf-8",
    )
    (repo / "app" / "routes" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "routes" / "checkout.py").write_text(
        "from app.services.discount_service import calculate_discount_cents\n"
        "\n"
        "\n"
        "def checkout(subtotal_cents, code):\n"
        "    return subtotal_cents - calculate_discount_cents(subtotal_cents, code)\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_discount.py").write_text(
        "from app.services.discount_service import calculate_discount_cents\n"
        "\n"
        "\n"
        "class Code:\n"
        "    def __init__(self, value):\n"
        "        self.value = value\n"
        "\n"
        "\n"
        "def test_discount_is_taken_on_the_whole_subtotal():\n"
        "    # 10% of 1050 cents is 105.  Truncating the subtotal first gives 100.\n"
        "    assert calculate_discount_cents(1050, Code(10)) == 105\n",
        encoding="utf-8",
    )
    return repo


@pytest.fixture
def blind_repo(tmp_path: Path) -> Path:
    """``defective_repo`` with a suite that never looks at the defect.

    Identical in every way except the assertion: this one divides a subtotal that
    is already a multiple of ten, so the truncated and the correct arithmetic
    agree.  The suite is green before the fix and green after it.

    It exists because "the suite passed" and "the defect is demonstrated fixed"
    are different claims, and a test suite that cannot tell them apart is the
    normal case rather than a contrived one — ShopDemo's own seven tests are
    exactly this.  A fixture that only ever produces the flattering result would
    let the dishonest answer pass unnoticed, so the honest answer needs a
    fixture of its own.
    """
    repo = tmp_path / "BlindApp"
    (repo / "app" / "services").mkdir(parents=True)
    (repo / "app" / "routes").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)

    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "main.py").write_text(
        "from app.routes.checkout import checkout\n\n\ndef run():\n    return checkout\n",
        encoding="utf-8",
    )
    (repo / "app" / "services" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "services" / "discount_service.py").write_text(
        "def calculate_discount_cents(subtotal_cents, code):\n"
        "    \"\"\"Return the discount in cents for ``code`` percent off.\"\"\"\n"
        f"{BUGGY_DISCOUNT}\n",
        encoding="utf-8",
    )
    (repo / "app" / "routes" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "routes" / "checkout.py").write_text(
        "from app.services.discount_service import calculate_discount_cents\n"
        "\n"
        "\n"
        "def checkout(subtotal_cents, code):\n"
        "    return subtotal_cents - calculate_discount_cents(subtotal_cents, code)\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_discount.py").write_text(
        "from app.services.discount_service import calculate_discount_cents\n"
        "\n"
        "\n"
        "class Code:\n"
        "    def __init__(self, value):\n"
        "        self.value = value\n"
        "\n"
        "\n"
        "def test_discount_is_taken_on_the_whole_subtotal():\n"
        "    # 1000 divides evenly, so truncating first happens to agree.  This\n"
        "    # test is green on the broken code, which is the whole point.\n"
        "    assert calculate_discount_cents(1000, Code(10)) == 100\n",
        encoding="utf-8",
    )
    return repo
