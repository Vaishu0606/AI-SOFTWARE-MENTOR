"""Phase 3 tests — Repository Ingestion: RepositoryExplorer."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from app.ingestion.explorer import RepositoryExplorer, DEFAULT_MAX_FILE_BYTES


# ── Helpers ────────────────────────────────────────────────────────────────────


def make_repo(tmp_path: Path) -> Path:
    """Create a minimal synthetic repository for testing."""
    # Source files
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "main.py").write_text('print("hello")\n')
    (tmp_path / "app" / "utils.py").write_text("def add(a, b): return a + b\n")

    # Nested package
    (tmp_path / "app" / "services").mkdir()
    (tmp_path / "app" / "services" / "__init__.py").write_text("")
    (tmp_path / "app" / "services" / "cart_service.py").write_text(
        "def get_cart(id): return None\n"
    )

    # Tests
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "tests" / "test_example.py").write_text("def test_add(): assert 1+1==2\n")

    # Config / project files
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "README.md").write_text("# Test Repo\n")

    # TypeScript files
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "index.ts").write_text("const x = 1;\n")
    (tmp_path / "frontend" / "App.tsx").write_text("export default function App() {}\n")
    (tmp_path / "frontend" / "utils.js").write_text("function noop() {}\n")

    # JSON / YAML
    (tmp_path / "frontend" / "package.json").write_text('{"name": "test"}\n')
    (tmp_path / "frontend" / "config.yaml").write_text("key: value\n")

    return tmp_path


# ── Basic exploration ──────────────────────────────────────────────────────────


def test_explore_returns_exploration(tmp_path: Path) -> None:
    make_repo(tmp_path)
    explorer = RepositoryExplorer()
    result = explorer.explore(tmp_path)
    assert result is not None
    assert result.root == str(tmp_path)
    assert result.name == tmp_path.name


def test_explore_counts_files(tmp_path: Path) -> None:
    make_repo(tmp_path)
    result = RepositoryExplorer().explore(tmp_path)
    assert result.total_files > 0


# ── File discovery ────────────────────────────────────────────────────────────


def test_python_files_discovered(tmp_path: Path) -> None:
    make_repo(tmp_path)
    result = RepositoryExplorer().explore(tmp_path)
    py_files = [f for f in result.files if f.extension == ".py"]
    assert len(py_files) > 0


def test_typescript_files_discovered(tmp_path: Path) -> None:
    make_repo(tmp_path)
    result = RepositoryExplorer().explore(tmp_path)
    ts_files = [f for f in result.files if f.extension == ".ts"]
    assert len(ts_files) > 0


def test_tsx_files_discovered(tmp_path: Path) -> None:
    make_repo(tmp_path)
    result = RepositoryExplorer().explore(tmp_path)
    tsx_files = [f for f in result.files if f.extension == ".tsx"]
    assert len(tsx_files) > 0


def test_js_files_discovered(tmp_path: Path) -> None:
    make_repo(tmp_path)
    result = RepositoryExplorer().explore(tmp_path)
    js_files = [f for f in result.files if f.extension == ".js"]
    assert len(js_files) > 0


def test_json_files_discovered(tmp_path: Path) -> None:
    make_repo(tmp_path)
    result = RepositoryExplorer().explore(tmp_path)
    json_files = [f for f in result.files if f.extension == ".json"]
    assert len(json_files) > 0


# ── Directory discovery ───────────────────────────────────────────────────────


def test_directories_discovered(tmp_path: Path) -> None:
    make_repo(tmp_path)
    result = RepositoryExplorer().explore(tmp_path)
    assert "app" in result.directories
    assert "tests" in result.directories


def test_nested_directory_discovered(tmp_path: Path) -> None:
    make_repo(tmp_path)
    result = RepositoryExplorer().explore(tmp_path)
    assert "app/services" in result.directories


def test_root_itself_not_in_directories(tmp_path: Path) -> None:
    make_repo(tmp_path)
    result = RepositoryExplorer().explore(tmp_path)
    # The root directory should not appear in the subdirectory list
    assert "." not in result.directories
    assert "" not in result.directories


# ── Language detection ────────────────────────────────────────────────────────


def test_language_detection_python(tmp_path: Path) -> None:
    f = tmp_path / "hello.py"
    f.write_text("x = 1")
    result = RepositoryExplorer().explore(tmp_path)
    meta = next(m for m in result.files if m.relative_path == "hello.py")
    assert meta.language == "Python"


def test_language_detection_typescript(tmp_path: Path) -> None:
    f = tmp_path / "hello.ts"
    f.write_text("const x = 1;")
    result = RepositoryExplorer().explore(tmp_path)
    meta = next(m for m in result.files if m.relative_path == "hello.ts")
    assert meta.language == "TypeScript"


def test_language_detection_tsx(tmp_path: Path) -> None:
    f = tmp_path / "App.tsx"
    f.write_text("export default function App() {}")
    result = RepositoryExplorer().explore(tmp_path)
    meta = next(m for m in result.files if m.relative_path == "App.tsx")
    assert meta.language == "TypeScript React"


def test_language_detection_javascript(tmp_path: Path) -> None:
    f = tmp_path / "app.js"
    f.write_text("function noop() {}")
    result = RepositoryExplorer().explore(tmp_path)
    meta = next(m for m in result.files if m.relative_path == "app.js")
    assert meta.language == "JavaScript"


def test_language_detection_yaml(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("key: value")
    result = RepositoryExplorer().explore(tmp_path)
    meta = next(m for m in result.files if m.relative_path == "config.yaml")
    assert meta.language == "YAML"


def test_language_detection_yml(tmp_path: Path) -> None:
    f = tmp_path / "config.yml"
    f.write_text("key: value")
    result = RepositoryExplorer().explore(tmp_path)
    meta = next(m for m in result.files if m.relative_path == "config.yml")
    assert meta.language == "YAML"


def test_language_detection_unknown(tmp_path: Path) -> None:
    f = tmp_path / "data.xyz123"
    f.write_text("unknown")
    result = RepositoryExplorer().explore(tmp_path)
    meta = next(m for m in result.files if m.relative_path == "data.xyz123")
    assert meta.language == "Unknown"


def test_language_detection_dockerfile(tmp_path: Path) -> None:
    f = tmp_path / "Dockerfile"
    f.write_text("FROM python:3.11")
    result = RepositoryExplorer().explore(tmp_path)
    meta = next(m for m in result.files if m.relative_path == "Dockerfile")
    assert meta.language == "Dockerfile"


# ── Test file detection ───────────────────────────────────────────────────────


def test_test_py_prefix_detected(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_example.py").write_text("def test_x(): pass")
    result = RepositoryExplorer().explore(tmp_path)
    test_paths = [f.relative_path for f in result.test_files]
    assert "tests/test_example.py" in test_paths


def test_test_py_suffix_detected(tmp_path: Path) -> None:
    (tmp_path / "cart_test.py").write_text("def test_x(): pass")
    result = RepositoryExplorer().explore(tmp_path)
    test_paths = [f.relative_path for f in result.test_files]
    assert "cart_test.py" in test_paths


def test_spec_ts_detected(tmp_path: Path) -> None:
    (tmp_path / "cart.spec.ts").write_text("it('works', () => {})")
    result = RepositoryExplorer().explore(tmp_path)
    test_paths = [f.relative_path for f in result.test_files]
    assert "cart.spec.ts" in test_paths


def test_test_ts_detected(tmp_path: Path) -> None:
    (tmp_path / "cart.test.ts").write_text("it('works', () => {})")
    result = RepositoryExplorer().explore(tmp_path)
    test_paths = [f.relative_path for f in result.test_files]
    assert "cart.test.ts" in test_paths


def test_non_test_file_not_marked_as_test(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1")
    result = RepositoryExplorer().explore(tmp_path)
    meta = next(m for m in result.files if m.relative_path == "main.py")
    assert meta.is_test is False


# ── Config file detection ─────────────────────────────────────────────────────


def test_requirements_txt_detected_as_config(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    result = RepositoryExplorer().explore(tmp_path)
    config_paths = [f.relative_path for f in result.config_files]
    assert "requirements.txt" in config_paths


def test_readme_detected_as_config(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Hello")
    result = RepositoryExplorer().explore(tmp_path)
    config_paths = [f.relative_path for f in result.config_files]
    assert "README.md" in config_paths


# ── Ignored directories ───────────────────────────────────────────────────────


def test_node_modules_ignored(tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lodash.js").write_text("module.exports = {}")
    (tmp_path / "app.js").write_text("const x = 1;")
    result = RepositoryExplorer().explore(tmp_path)
    paths = [f.relative_path for f in result.files]
    assert not any("node_modules" in p for p in paths)


def test_pycache_ignored(tmp_path: Path) -> None:
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"\x00\x00")
    (tmp_path / "main.py").write_text("x = 1")
    result = RepositoryExplorer().explore(tmp_path)
    paths = [f.relative_path for f in result.files]
    assert not any("__pycache__" in p for p in paths)


def test_git_dir_ignored(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\nbare = false")
    (tmp_path / "app.py").write_text("x = 1")
    result = RepositoryExplorer().explore(tmp_path)
    paths = [f.relative_path for f in result.files]
    assert not any(".git" in p for p in paths)


def test_venv_ignored(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin")
    (tmp_path / "main.py").write_text("x = 1")
    result = RepositoryExplorer().explore(tmp_path)
    paths = [f.relative_path for f in result.files]
    assert not any(".venv" in p for p in paths)


def test_next_dir_ignored(tmp_path: Path) -> None:
    (tmp_path / ".next").mkdir()
    (tmp_path / ".next" / "chunk.js").write_text("// bundled")
    (tmp_path / "page.tsx").write_text("export default function Page() {}")
    result = RepositoryExplorer().explore(tmp_path)
    paths = [f.relative_path for f in result.files]
    assert not any(".next" in p for p in paths)


# ── Secret / .env protection ──────────────────────────────────────────────────


def test_env_file_is_secret(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=super_secret_value_12345\nDB_PASSWORD=hunter2\n")
    result = RepositoryExplorer().explore(tmp_path, read_source=True)
    # The file must be marked as secret
    meta = next((f for f in result.files if f.relative_path == ".env"), None)
    assert meta is not None
    assert meta.is_secret is True


def test_env_file_contents_never_returned(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=super_secret_value_12345\nDB_PASSWORD=hunter2\n")
    result = RepositoryExplorer().explore(tmp_path, read_source=True)
    # Contents must not appear anywhere in source evidence
    all_content = " ".join(ev.content for ev in result.source_evidence)
    assert "super_secret_value_12345" not in all_content
    assert "hunter2" not in all_content


def test_env_local_file_is_secret(tmp_path: Path) -> None:
    (tmp_path / ".env.local").write_text("TOKEN=abc123")
    result = RepositoryExplorer().explore(tmp_path)
    meta = next((f for f in result.files if ".env.local" in f.relative_path), None)
    assert meta is not None
    assert meta.is_secret is True


# ── Binary protection ─────────────────────────────────────────────────────────


def test_png_binary_not_decoded(tmp_path: Path) -> None:
    # Write a valid PNG header
    png = tmp_path / "image.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    result = RepositoryExplorer().explore(tmp_path, read_source=True)
    paths = [ev.relative_path for ev in result.source_evidence]
    assert "image.png" not in paths


def test_null_byte_file_treated_as_binary(tmp_path: Path) -> None:
    # A file containing null bytes is binary
    f = tmp_path / "data.bin"
    f.write_bytes(b"some\x00binary\x00data")
    result = RepositoryExplorer().explore(tmp_path, read_source=True)
    paths = [ev.relative_path for ev in result.source_evidence]
    assert "data.bin" not in paths


def test_binary_skipped_file_recorded(tmp_path: Path) -> None:
    f = tmp_path / "data.bin"
    f.write_bytes(b"some\x00binary\x00data")
    (tmp_path / "main.py").write_text("x = 1")
    result = RepositoryExplorer().explore(tmp_path, read_source=True)
    skipped_reasons = {s.relative_path: s.reason for s in result.skipped_files}
    assert skipped_reasons.get("data.bin") == "binary"


# ── File size limit ───────────────────────────────────────────────────────────


def test_file_exceeding_size_limit_skipped(tmp_path: Path) -> None:
    large_file = tmp_path / "large.py"
    # Write a file slightly larger than 1 KiB limit
    large_file.write_text("x = 1\n" * 300)

    explorer = RepositoryExplorer(max_file_bytes=100)  # 100 byte limit
    (tmp_path / "small.py").write_text("x = 1\n")
    result = explorer.explore(tmp_path, read_source=True)

    # large.py must not be in source evidence
    evidence_paths = [ev.relative_path for ev in result.source_evidence]
    assert "large.py" not in evidence_paths

    # large.py must be recorded as skipped
    skipped_reasons = {s.relative_path: s.reason for s in result.skipped_files}
    assert skipped_reasons.get("large.py") == "exceeds_size_limit"


def test_small_file_within_limit_is_read(tmp_path: Path) -> None:
    small_file = tmp_path / "small.py"
    small_file.write_text("x = 1\n")
    explorer = RepositoryExplorer(max_file_bytes=DEFAULT_MAX_FILE_BYTES)
    result = explorer.explore(tmp_path, read_source=True)
    evidence_paths = [ev.relative_path for ev in result.source_evidence]
    assert "small.py" in evidence_paths


# ── Source evidence ───────────────────────────────────────────────────────────


def test_source_evidence_content_matches_file(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("print('hello')\n")
    result = RepositoryExplorer().explore(tmp_path, read_source=True)
    ev = next(e for e in result.source_evidence if e.relative_path == "hello.py")
    assert "print('hello')" in ev.content


def test_source_evidence_line_count(tmp_path: Path) -> None:
    (tmp_path / "lines.py").write_text("a = 1\nb = 2\nc = 3\n")
    result = RepositoryExplorer().explore(tmp_path, read_source=True)
    ev = next(e for e in result.source_evidence if e.relative_path == "lines.py")
    assert ev.line_count == 4  # 3 newlines → 4 "lines" (including trailing)


def test_source_evidence_language(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1")
    result = RepositoryExplorer().explore(tmp_path, read_source=True)
    ev = next(e for e in result.source_evidence if e.relative_path == "app.py")
    assert ev.language == "Python"


def test_no_source_evidence_when_read_source_false(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1")
    result = RepositoryExplorer().explore(tmp_path, read_source=False)
    assert result.source_evidence == []


# ── Path safety ───────────────────────────────────────────────────────────────


def test_path_traversal_cannot_escape_root(tmp_path: Path) -> None:
    """Symlink pointing outside the repository root must be ignored."""
    import os, sys
    if sys.platform == "win32":
        # Symlink creation requires elevation on Windows; skip this specific test
        pytest.skip("Symlink creation requires elevated privileges on Windows")

    outside_file = tmp_path.parent / "outside_secret.txt"
    outside_file.write_text("TOP_SECRET=password123")

    symlink = tmp_path / "escaped_symlink.txt"
    os.symlink(outside_file, symlink)

    result = RepositoryExplorer().explore(tmp_path, read_source=True)
    all_content = " ".join(ev.content for ev in result.source_evidence)
    assert "TOP_SECRET" not in all_content
    assert "password123" not in all_content


# ── Language counts / primary language ───────────────────────────────────────


def test_languages_counted(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x=1")
    (tmp_path / "b.py").write_text("y=2")
    (tmp_path / "c.ts").write_text("const z = 3;")
    result = RepositoryExplorer().explore(tmp_path)
    assert result.languages.get("Python") == 2
    assert result.languages.get("TypeScript") == 1


def test_primary_language_most_frequent(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x=1")
    (tmp_path / "b.py").write_text("y=2")
    (tmp_path / "c.py").write_text("z=3")
    (tmp_path / "d.ts").write_text("const w = 4;")
    result = RepositoryExplorer().explore(tmp_path)
    assert result.primary_language == "Python"


# ── ShopDemo integration test ─────────────────────────────────────────────────


def test_shopdemo_exploration(tmp_path: Path) -> None:
    """Run the explorer against the actual ShopDemo demo repository."""
    import os

    # Locate demo-repository/ShopDemo relative to this test file
    tests_dir = Path(__file__).parent
    backend_dir = tests_dir.parent
    workspace_dir = backend_dir.parent
    shopdemo_path = workspace_dir / "demo-repository" / "ShopDemo"

    if not shopdemo_path.exists():
        pytest.skip("demo-repository/ShopDemo not found — Phase 2 not yet built")

    explorer = RepositoryExplorer()
    result = explorer.explore(shopdemo_path)

    # Basic structure
    assert result.name == "ShopDemo"
    assert result.total_files > 0

    # Must find Python files
    py_files = [f for f in result.files if f.language == "Python"]
    assert len(py_files) > 0

    # Must find app/ directory
    assert any("app" in d for d in result.directories)

    # Must find tests/ directory
    assert any("tests" in d for d in result.directories)

    # test_cart.py must be detected as a test file
    test_paths = [f.relative_path for f in result.test_files]
    assert any("test_cart.py" in p for p in test_paths)

    # README.md must be detected as a config file
    config_paths = [f.relative_path for f in result.config_files]
    assert any("README.md" in p for p in config_paths)

    # requirements.txt must be detected as a config file
    assert any("requirements.txt" in p for p in config_paths)

    # Primary language is Python
    assert result.primary_language == "Python"
