"""
Phase 4 tests — AST Analyzer (unit tests for analyze_file).

Covers all 20 required test scenarios:
 1.  Simple function extraction
 2.  Async function extraction
 3.  Class extraction
 4.  Method extraction
 5.  Inheritance / base classes
 6.  Imports (regular)
 7.  Imports (from … import … as alias)
 8.  Function calls
 9.  Decorators
10.  FastAPI GET route detection
11.  FastAPI POST route detection
12.  Router route detection
13.  Source line numbers
14.  Syntax error handling
15.  Empty source
16.  Unsupported language
17.  Multiple files via analyze_repository
18.  Repository-level aggregation
19.  Malicious/untrusted source does NOT execute
20.  ShopDemo integration (separate test file)
"""

from __future__ import annotations

import pytest

from app.analysis.ast_analyzer import ASTAnalyzer, FileAnalysis

analyzer = ASTAnalyzer()


# ── 1. Simple function extraction ─────────────────────────────────────────────


def test_simple_function_name():
    result = analyzer.analyze_file("mod.py", "def add(a, b):\n    return a + b\n")
    names = [f.name for f in result.functions]
    assert "add" in names


def test_simple_function_arguments():
    result = analyzer.analyze_file("mod.py", "def add(a, b):\n    return a + b\n")
    fn = next(f for f in result.functions if f.name == "add")
    assert "a" in fn.arguments
    assert "b" in fn.arguments


def test_function_line_start():
    src = "x = 1\n\ndef my_func():\n    pass\n"
    result = analyzer.analyze_file("mod.py", src)
    fn = next(f for f in result.functions if f.name == "my_func")
    assert fn.line_start == 3


def test_function_line_end():
    src = "def my_func():\n    x = 1\n    y = 2\n    return x + y\n"
    result = analyzer.analyze_file("mod.py", src)
    fn = next(f for f in result.functions if f.name == "my_func")
    assert fn.line_end >= 4


# ── 2. Async function extraction ──────────────────────────────────────────────


def test_async_function_detected():
    src = "async def fetch(url: str) -> str:\n    return url\n"
    result = analyzer.analyze_file("mod.py", src)
    fn = next(f for f in result.functions if f.name == "fetch")
    assert fn.is_async is True


def test_sync_function_not_async():
    src = "def run():\n    pass\n"
    result = analyzer.analyze_file("mod.py", src)
    fn = next(f for f in result.functions if f.name == "run")
    assert fn.is_async is False


# ── 3. Class extraction ───────────────────────────────────────────────────────


def test_class_name_extracted():
    src = "class MyService:\n    pass\n"
    result = analyzer.analyze_file("mod.py", src)
    names = [c.name for c in result.classes]
    assert "MyService" in names


def test_class_line_start():
    src = "x = 1\n\nclass Foo:\n    pass\n"
    result = analyzer.analyze_file("mod.py", src)
    cls = next(c for c in result.classes if c.name == "Foo")
    assert cls.line_start == 3


# ── 4. Method extraction ──────────────────────────────────────────────────────


def test_method_inside_class():
    src = (
        "class Cart:\n"
        "    def add_item(self, item):\n"
        "        pass\n"
    )
    result = analyzer.analyze_file("mod.py", src)
    cls = next(c for c in result.classes if c.name == "Cart")
    method_names = [m.name for m in cls.methods]
    assert "add_item" in method_names


def test_method_is_method_flag():
    src = (
        "class Svc:\n"
        "    def run(self):\n"
        "        pass\n"
    )
    result = analyzer.analyze_file("mod.py", src)
    cls = next(c for c in result.classes if c.name == "Svc")
    method = next(m for m in cls.methods if m.name == "run")
    assert method.is_method is True
    assert method.class_name == "Svc"


def test_method_not_in_top_level_functions():
    src = (
        "class Svc:\n"
        "    def run(self):\n"
        "        pass\n"
    )
    result = analyzer.analyze_file("mod.py", src)
    top_names = [f.name for f in result.functions]
    assert "run" not in top_names


def test_async_method_detected():
    src = (
        "class Handler:\n"
        "    async def handle(self):\n"
        "        pass\n"
    )
    result = analyzer.analyze_file("mod.py", src)
    cls = next(c for c in result.classes if c.name == "Handler")
    method = next(m for m in cls.methods if m.name == "handle")
    assert method.is_async is True


# ── 5. Inheritance / base classes ─────────────────────────────────────────────


def test_single_base_class():
    src = "class Child(Parent):\n    pass\n"
    result = analyzer.analyze_file("mod.py", src)
    cls = next(c for c in result.classes if c.name == "Child")
    assert "Parent" in cls.bases


def test_multiple_base_classes():
    src = "class C(A, B):\n    pass\n"
    result = analyzer.analyze_file("mod.py", src)
    cls = next(c for c in result.classes if c.name == "C")
    assert "A" in cls.bases
    assert "B" in cls.bases


def test_no_bases_returns_empty_list():
    src = "class Plain:\n    pass\n"
    result = analyzer.analyze_file("mod.py", src)
    cls = next(c for c in result.classes if c.name == "Plain")
    assert cls.bases == []


# ── 6. Regular imports ────────────────────────────────────────────────────────


def test_import_module():
    src = "import os\n"
    result = analyzer.analyze_file("mod.py", src)
    modules = [i.module for i in result.imports]
    assert "os" in modules


def test_import_is_not_from_import():
    src = "import sys\n"
    result = analyzer.analyze_file("mod.py", src)
    imp = next(i for i in result.imports if i.module == "sys")
    assert imp.is_from_import is False


def test_from_import():
    src = "from pathlib import Path\n"
    result = analyzer.analyze_file("mod.py", src)
    imp = next(i for i in result.imports if i.module == "pathlib")
    assert "Path" in imp.names
    assert imp.is_from_import is True


def test_from_import_multiple_names():
    src = "from typing import Any, Optional, List\n"
    result = analyzer.analyze_file("mod.py", src)
    imp = next(i for i in result.imports if i.module == "typing")
    assert "Any" in imp.names
    assert "Optional" in imp.names
    assert "List" in imp.names


def test_import_line_number():
    src = "x = 1\nimport os\n"
    result = analyzer.analyze_file("mod.py", src)
    imp = next(i for i in result.imports if i.module == "os")
    assert imp.line == 2


# ── 7. Import aliases ─────────────────────────────────────────────────────────


def test_import_alias():
    src = "import numpy as np\n"
    result = analyzer.analyze_file("mod.py", src)
    imp = next(i for i in result.imports if i.module == "numpy")
    assert imp.aliases.get("numpy") == "np"


def test_from_import_alias():
    src = "from datetime import datetime as dt\n"
    result = analyzer.analyze_file("mod.py", src)
    imp = next(i for i in result.imports if i.module == "datetime")
    assert imp.aliases.get("datetime") == "dt"


# ── 8. Function calls ─────────────────────────────────────────────────────────


def test_call_inside_function_detected():
    src = (
        "def process():\n"
        "    helper()\n"
        "    print('done')\n"
    )
    result = analyzer.analyze_file("mod.py", src)
    fn = next(f for f in result.functions if f.name == "process")
    call_names = [c.callable_expr for c in fn.calls]
    assert any("helper" in n for n in call_names)
    assert any("print" in n for n in call_names)


def test_method_call_detected():
    src = (
        "def run():\n"
        "    obj.method()\n"
    )
    result = analyzer.analyze_file("mod.py", src)
    fn = next(f for f in result.functions if f.name == "run")
    call_exprs = [c.callable_expr for c in fn.calls]
    assert any("obj.method" in e for e in call_exprs)


def test_call_line_number():
    src = (
        "def run():\n"
        "    pass\n"
        "\n"
        "helper()\n"
    )
    result = analyzer.analyze_file("mod.py", src)
    top_calls = result.top_level_calls
    call = next((c for c in top_calls if "helper" in c.callable_expr), None)
    assert call is not None
    assert call.line == 4


# ── 9. Decorators ─────────────────────────────────────────────────────────────


def test_function_decorator_extracted():
    src = (
        "@property\n"
        "def value(self):\n"
        "    return self._v\n"
    )
    result = analyzer.analyze_file("mod.py", src)
    fn = next(f for f in result.functions if f.name == "value")
    assert "property" in fn.decorators


def test_class_decorator_extracted():
    src = (
        "@dataclass\n"
        "class Point:\n"
        "    x: int\n"
        "    y: int\n"
    )
    result = analyzer.analyze_file("mod.py", src)
    cls = next(c for c in result.classes if c.name == "Point")
    assert "dataclass" in cls.decorators


def test_multiple_decorators():
    src = (
        "@decorator_one\n"
        "@decorator_two\n"
        "def func():\n"
        "    pass\n"
    )
    result = analyzer.analyze_file("mod.py", src)
    fn = next(f for f in result.functions if f.name == "func")
    assert len(fn.decorators) == 2


# ── 10. FastAPI GET route ─────────────────────────────────────────────────────


def test_app_get_route_detected():
    src = (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "\n"
        "@app.get('/health')\n"
        "async def health():\n"
        "    return {'status': 'ok'}\n"
    )
    result = analyzer.analyze_file("routes.py", src)
    assert len(result.routes) == 1
    route = result.routes[0]
    assert route.http_method == "GET"
    assert route.path == "/health"
    assert route.function_name == "health"
    assert route.router_var == "app"


def test_get_route_line_number():
    src = (
        "app = None\n"
        "\n"
        "@app.get('/items')\n"
        "def list_items():\n"
        "    pass\n"
    )
    result = analyzer.analyze_file("routes.py", src)
    route = next(r for r in result.routes if r.function_name == "list_items")
    assert route.line == 3


# ── 11. FastAPI POST route ────────────────────────────────────────────────────


def test_app_post_route_detected():
    src = (
        "@app.post('/items')\n"
        "async def create_item(body):\n"
        "    pass\n"
    )
    result = analyzer.analyze_file("routes.py", src)
    route = next(r for r in result.routes if r.function_name == "create_item")
    assert route.http_method == "POST"
    assert route.path == "/items"


def test_put_route_detected():
    src = (
        "@app.put('/items/{id}')\n"
        "async def update_item(id: int):\n"
        "    pass\n"
    )
    result = analyzer.analyze_file("routes.py", src)
    route = next(r for r in result.routes if r.function_name == "update_item")
    assert route.http_method == "PUT"


def test_delete_route_detected():
    src = (
        "@app.delete('/items/{id}')\n"
        "async def delete_item(id: int):\n"
        "    pass\n"
    )
    result = analyzer.analyze_file("routes.py", src)
    route = next(r for r in result.routes if r.function_name == "delete_item")
    assert route.http_method == "DELETE"


# ── 12. Router-style routes ───────────────────────────────────────────────────


def test_router_post_route_detected():
    src = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@router.post('/apply')\n"
        "async def apply_discount(body):\n"
        "    pass\n"
    )
    result = analyzer.analyze_file("discount.py", src)
    route = next(r for r in result.routes if r.function_name == "apply_discount")
    assert route.http_method == "POST"
    assert route.path == "/apply"
    assert route.router_var == "router"


def test_router_get_route_detected():
    src = (
        "@router.get('/')\n"
        "async def list_all():\n"
        "    pass\n"
    )
    result = analyzer.analyze_file("mod.py", src)
    route = next(r for r in result.routes if r.function_name == "list_all")
    assert route.http_method == "GET"


def test_route_file_path_recorded():
    src = (
        "@router.get('/items')\n"
        "async def items():\n"
        "    pass\n"
    )
    result = analyzer.analyze_file("app/routes/items.py", src)
    route = result.routes[0]
    assert route.file_path == "app/routes/items.py"


# ── 13. Source line numbers ───────────────────────────────────────────────────


def test_import_line_numbers_accurate():
    src = "import os\nimport sys\nimport json\n"
    result = analyzer.analyze_file("mod.py", src)
    lines = {i.module: i.line for i in result.imports}
    assert lines["os"] == 1
    assert lines["sys"] == 2
    assert lines["json"] == 3


def test_class_and_function_lines_correct():
    src = (
        "class A:\n"         # line 1
        "    def m(self):\n"  # line 2
        "        pass\n"     # line 3
        "\n"                 # line 4
        "def top():\n"       # line 5
        "    pass\n"         # line 6
    )
    result = analyzer.analyze_file("mod.py", src)
    cls = next(c for c in result.classes if c.name == "A")
    fn = next(f for f in result.functions if f.name == "top")
    assert cls.line_start == 1
    assert fn.line_start == 5


# ── 14. Syntax error handling ─────────────────────────────────────────────────


def test_syntax_error_returns_file_analysis():
    src = "def broken(:\n    pass\n"
    result = analyzer.analyze_file("bad.py", src)
    assert result.supported is True
    assert result.error is not None
    assert result.error.error_type == "SyntaxError"


def test_syntax_error_does_not_raise():
    src = "def broken(:\n    pass\n"
    # Must not raise — should return a FileAnalysis with error recorded
    result = analyzer.analyze_file("bad.py", src)
    assert isinstance(result, FileAnalysis)


def test_syntax_error_file_path_recorded():
    src = "def broken(:\n    pass\n"
    result = analyzer.analyze_file("broken/file.py", src)
    assert result.error is not None
    assert result.error.file_path == "broken/file.py"


# ── 15. Empty source ──────────────────────────────────────────────────────────


def test_empty_source_succeeds():
    result = analyzer.analyze_file("empty.py", "")
    assert result.supported is True
    assert result.error is None
    assert result.functions == []
    assert result.classes == []
    assert result.imports == []


def test_whitespace_only_source_succeeds():
    result = analyzer.analyze_file("ws.py", "   \n\n   \n")
    assert result.supported is True
    assert result.error is None


# ── 16. Unsupported language ──────────────────────────────────────────────────


def test_typescript_file_not_supported():
    src = "const x: number = 1;"
    result = analyzer.analyze_file("app.ts", src, language="TypeScript")
    assert result.supported is False


def test_unsupported_language_has_error():
    src = "public class Foo {}"
    result = analyzer.analyze_file("Foo.java", src, language="Java")
    assert result.error is not None
    assert result.error.error_type == "UnsupportedLanguage"


def test_unsupported_language_no_functions():
    src = "const x = 1;"
    result = analyzer.analyze_file("a.js", src, language="JavaScript")
    assert result.functions == []
    assert result.classes == []


# ── 17 + 18. Multiple files / repository-level analysis ──────────────────────


def test_analyze_repository_multiple_files():
    files = [
        ("a.py", "def foo(): pass\n", "Python"),
        ("b.py", "def bar(): pass\n", "Python"),
        ("c.ts", "const x = 1;", "TypeScript"),
    ]
    result = analyzer.analyze_repository(
        root="/tmp/repo", name="repo", file_sources=files
    )
    assert len(result.files) == 3


def test_analyze_repository_aggregates_functions():
    files = [
        ("a.py", "def foo(): pass\ndef baz(): pass\n", "Python"),
        ("b.py", "def bar(): pass\n", "Python"),
    ]
    result = analyzer.analyze_repository(
        root="/tmp/repo", name="repo", file_sources=files
    )
    names = [f.name for f in result.all_functions]
    assert "foo" in names
    assert "baz" in names
    assert "bar" in names


def test_analyze_repository_aggregates_routes():
    files = [
        (
            "routes.py",
            "@router.get('/a')\nasync def a(): pass\n"
            "@router.post('/b')\nasync def b(): pass\n",
            "Python",
        ),
    ]
    result = analyzer.analyze_repository(
        root="/tmp/repo", name="repo", file_sources=files
    )
    assert len(result.routes) == 2


def test_analyze_repository_aggregates_relationships():
    files = [
        ("a.py", "from app.services import cart_service\n", "Python"),
        ("b.py", "import os\n", "Python"),
    ]
    result = analyzer.analyze_repository(
        root="/tmp/repo", name="repo", file_sources=files
    )
    source_files = [r.source_file for r in result.relationships]
    assert "a.py" in source_files
    assert "b.py" in source_files


def test_analyze_repository_records_errors():
    files = [
        ("good.py", "def ok(): pass\n", "Python"),
        ("bad.py", "def broken(:\n    pass\n", "Python"),
    ]
    result = analyzer.analyze_repository(
        root="/tmp/repo", name="repo", file_sources=files
    )
    assert len(result.errors) == 1
    assert result.errors[0].error_type == "SyntaxError"


def test_one_bad_file_does_not_abort_analysis():
    files = [
        ("bad.py", "def broken(:\n    pass\n", "Python"),
        ("good.py", "def ok(): pass\n", "Python"),
    ]
    result = analyzer.analyze_repository(
        root="/tmp/repo", name="repo", file_sources=files
    )
    # good.py must still be analyzed
    good = next(f for f in result.files if f.relative_path == "good.py")
    assert good.error is None
    fn_names = [f.name for f in good.functions]
    assert "ok" in fn_names


# ── 19. Malicious / untrusted source does NOT execute ────────────────────────


def test_os_system_call_not_executed(tmp_path):
    """Source containing os.system() must be PARSED, never executed."""
    marker_file = tmp_path / "marker.txt"
    src = (
        "import os\n"
        f"os.system('echo pwned > {marker_file}')\n"
    )
    result = analyzer.analyze_file("evil.py", src)
    # The analysis must succeed (parse + AST walk)
    assert result.supported is True
    # The command must NOT have been executed — marker file must not exist
    assert not marker_file.exists(), (
        "SECURITY VIOLATION: os.system() was executed during AST analysis!"
    )


def test_exec_in_source_not_executed(tmp_path):
    """Source containing exec() must be PARSED, never executed."""
    marker_file = tmp_path / "exec_marker.txt"
    src = (
        "import os\n"
        f"exec(\"open('{marker_file}', 'w').close()\")\n"
    )
    result = analyzer.analyze_file("evil.py", src)
    assert result.supported is True
    assert not marker_file.exists(), (
        "SECURITY VIOLATION: exec() was executed during AST analysis!"
    )


def test_decorator_not_executed(tmp_path):
    """Decorators must be parsed as AST nodes, never called."""
    marker_file = tmp_path / "dec_marker.txt"
    # If the analyzer ever evaluated the decorator expression, it would
    # try to call a function that writes to marker_file. Since we're in
    # test context and the function doesn't exist, it would raise NameError
    # at minimum — but the real invariant is it must not run at all.
    src = (
        "import os\n"
        f"@os.makedirs('{marker_file}', exist_ok=True)\n"
        "def func():\n"
        "    pass\n"
    )
    result = analyzer.analyze_file("evil.py", src)
    assert result.supported is True
    assert not marker_file.exists(), (
        "SECURITY VIOLATION: decorator was executed during AST analysis!"
    )


def test_import_not_executed():
    """Import statements in analyzed source must not cause actual imports."""
    src = "import nonexistent_module_xyz_12345\n"
    # If we actually executed the import, this would raise ModuleNotFoundError.
    # If the analyzer is safe, it just records the import as an AST node.
    result = analyzer.analyze_file("mod.py", src)
    imp = next((i for i in result.imports if i.module == "nonexistent_module_xyz_12345"), None)
    assert imp is not None  # recorded as data
    assert result.error is None  # no error from the missing module
