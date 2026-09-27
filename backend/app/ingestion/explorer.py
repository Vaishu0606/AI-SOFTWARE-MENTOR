"""
Repository explorer — Phase 3 file-system analysis.

Responsibilities:
- Walk a repository's file tree safely (no path traversal, no symlink escape)
- Detect file languages by extension
- Identify test files, configuration files, and source files
- Collect structured file metadata
- Read source file contents with size and binary guards
- Produce a RepositoryExploration result

NOT in scope for Phase 3:
- AST analysis
- Dependency graph extraction
- AI provider calls
- Orchestrator integration

Security invariants enforced here:
- All resolved paths must remain under the repository root
- .env / secret files: existence noted, contents never read
- Binary files: detected and skipped
- Files over MAX_FILE_BYTES: skipped with reason recorded
- Symlinks outside the repository root: skipped
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Default maximum file size for content reading (1 MiB)
DEFAULT_MAX_FILE_BYTES: int = 1 * 1024 * 1024

# Directories that are always ignored during traversal
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        ".env",          # rare directory name — ignore as well
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        ".next",
        "out",
        "coverage",
        ".pytest_cache",
        ".mypy_cache",
        ".tox",
        ".eggs",
        "*.egg-info",
        ".idea",
        ".vscode",
        "tmp",
        "temp",
    }
)

# File patterns whose CONTENTS must never be read (secrets / env files)
SECRET_FILENAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    ".env.example",   # example files are safe to note but not required to read
)

SECRET_EXTENSION_PATTERNS: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx")

SECRET_NAME_SUBSTRINGS: tuple[str, ...] = ("secret", "credential", "password", "token")

# Extension → language name
EXTENSION_LANGUAGE_MAP: dict[str, str] = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript React",
    ".js": "JavaScript",
    ".jsx": "JavaScript React",
    ".java": "Java",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".c": "C",
    ".cs": "C#",
    ".go": "Go",
    ".rs": "Rust",
    ".php": "PHP",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "CSS",
    ".sass": "CSS",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".md": "Markdown",
    ".rst": "reStructuredText",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".xml": "XML",
    ".rb": "Ruby",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".dart": "Dart",
    ".r": "R",
    ".R": "R",
    ".lua": "Lua",
    ".tf": "Terraform",
    ".hcl": "HCL",
    ".proto": "Protobuf",
    ".graphql": "GraphQL",
    ".gql": "GraphQL",
    ".dockerfile": "Dockerfile",
    ".ini": "INI",
    ".cfg": "INI",
    ".conf": "Config",
}

# Filenames without extensions that indicate a specific language / type
FILENAME_LANGUAGE_MAP: dict[str, str] = {
    "Dockerfile": "Dockerfile",
    "Makefile": "Makefile",
    "Procfile": "Config",
    "Brewfile": "Ruby",
    "Gemfile": "Ruby",
    "Rakefile": "Ruby",
}

# Configuration / project files to highlight
PROJECT_FILE_NAMES: frozenset[str] = frozenset(
    {
        "README.md",
        "README.rst",
        "README.txt",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "requirements.txt",
        "requirements-dev.txt",
        "Pipfile",
        "Pipfile.lock",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "poetry.lock",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        ".gitignore",
        ".gitattributes",
        "tsconfig.json",
        "jsconfig.json",
        "vite.config.ts",
        "vite.config.js",
        "next.config.ts",
        "next.config.js",
        "next.config.mjs",
        "tailwind.config.ts",
        "tailwind.config.js",
        "pytest.ini",
        "tox.ini",
        "mypy.ini",
        ".eslintrc",
        ".eslintrc.json",
        ".eslintrc.js",
        "eslint.config.mjs",
        ".prettierrc",
        ".flake8",
        "Makefile",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "CHANGELOG.md",
    }
)

# Signatures of known binary formats (first bytes)
BINARY_SIGNATURES: tuple[bytes, ...] = (
    b"\x89PNG",        # PNG
    b"\xff\xd8\xff",   # JPEG
    b"GIF8",           # GIF
    b"BM",             # BMP
    b"\x1f\x8b",       # gzip
    b"PK\x03\x04",     # zip/jar/docx
    b"\x7fELF",        # ELF binary
    b"MZ",             # PE/EXE
    b"%PDF",           # PDF
    b"\xcf\xfa\xed\xfe",  # Mach-O binary (little-endian)
    b"\xfe\xed\xfa\xce",  # Mach-O binary (big-endian)
    b"RIFF",           # WAV/AVI
    b"\x00\x00\x01\xba",  # MPEG
    b"\x49\x44\x33",   # MP3 ID3
    b"OggS",           # OGG
    b"fLaC",           # FLAC
    b"\x1a\x45\xdf\xa3",  # Matroska/WebM
)


# ── Data models ───────────────────────────────────────────────────────────────


@dataclass
class FileMetadata:
    """Metadata about a single file discovered during exploration."""

    relative_path: str          # POSIX-style, relative to repository root
    extension: str              # e.g. ".py"
    language: str               # e.g. "Python" or "Unknown"
    size_bytes: int
    is_test: bool
    is_config: bool
    is_secret: bool             # existence noted; contents never read


@dataclass
class SourceFileEvidence:
    """Source file contents collected as an evidence item."""

    relative_path: str
    language: str
    content: str
    line_count: int
    size_bytes: int


@dataclass
class SkippedFile:
    """Records a file that was skipped during content reading."""

    relative_path: str
    reason: str  # e.g. "binary", "exceeds_size_limit", "secret"


@dataclass
class RepositoryExploration:
    """The full structured result of exploring a repository.

    This is the primary output of the Explorer and the input to future agents.
    """

    root: str                                       # absolute path
    name: str                                       # derived from root dirname
    total_files: int
    directories: list[str]                          # relative POSIX paths
    files: list[FileMetadata]
    source_files: list[FileMetadata]                # non-test source files
    test_files: list[FileMetadata]
    config_files: list[FileMetadata]
    languages: dict[str, int]                       # language → file count
    primary_language: str | None
    skipped_files: list[SkippedFile]
    source_evidence: list[SourceFileEvidence] = field(default_factory=list)


# ── Explorer ──────────────────────────────────────────────────────────────────


class RepositoryExplorer:
    """Safely explores a local repository and produces structured metadata.

    Usage::

        explorer = RepositoryExplorer()
        result = explorer.explore(repo_root)
        # result: RepositoryExploration

        # Optionally read source file contents:
        result = explorer.explore(repo_root, read_source=True)
    """

    def __init__(self, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> None:
        self.max_file_bytes = max_file_bytes

    # ── Public API ────────────────────────────────────────────────────────────

    def explore(
        self,
        root: Path,
        read_source: bool = False,
    ) -> RepositoryExploration:
        """Explore the repository at ``root``.

        Args:
            root: Absolute, resolved path to the repository root.
                  Must have been validated by ``RepositoryCloner`` first.
            read_source: If True, also read and attach source file contents
                         to the returned exploration (respecting all guards).

        Returns:
            A ``RepositoryExploration`` containing all discovered metadata.
        """
        root = root.resolve()

        all_files: list[FileMetadata] = []
        directories: list[str] = []
        skipped: list[SkippedFile] = []

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current_dir = Path(dirpath).resolve()

            # ── Security: must remain inside root ─────────────────────────
            if not self._is_inside_root(current_dir, root):
                dirnames.clear()
                continue

            # ── Prune ignored directories (in-place to prevent descent) ───
            dirnames[:] = [
                d for d in dirnames
                if not self._should_ignore_dir(d)
                and self._is_inside_root((current_dir / d).resolve(), root)
            ]

            # Record directory (skip root itself)
            if current_dir != root:
                rel_dir = current_dir.relative_to(root).as_posix()
                directories.append(rel_dir)

            for filename in filenames:
                file_path = (current_dir / filename).resolve()

                # ── Security: symlink / traversal check ───────────────────
                if not self._is_inside_root(file_path, root):
                    skipped.append(
                        SkippedFile(
                            relative_path=str(file_path),
                            reason="path_outside_root",
                        )
                    )
                    continue

                rel_path = file_path.relative_to(root).as_posix()
                ext = file_path.suffix.lower()
                language = self._detect_language(file_path)
                is_test = self._is_test_file(file_path, root)
                is_config = filename in PROJECT_FILE_NAMES
                is_secret = self._is_secret_file(file_path)

                try:
                    size_bytes = file_path.stat().st_size
                except OSError:
                    skipped.append(SkippedFile(rel_path, "stat_error"))
                    continue

                all_files.append(
                    FileMetadata(
                        relative_path=rel_path,
                        extension=ext,
                        language=language,
                        size_bytes=size_bytes,
                        is_test=is_test,
                        is_config=is_config,
                        is_secret=is_secret,
                    )
                )

        # ── Classify and count ─────────────────────────────────────────────
        source_files = [f for f in all_files if not f.is_test and not f.is_config]
        test_files = [f for f in all_files if f.is_test]
        config_files = [f for f in all_files if f.is_config]

        language_counts: dict[str, int] = {}
        for f in all_files:
            if f.language != "Unknown":
                language_counts[f.language] = language_counts.get(f.language, 0) + 1

        primary_language = (
            max(language_counts, key=lambda k: language_counts[k])
            if language_counts
            else None
        )

        exploration = RepositoryExploration(
            root=str(root),
            name=root.name,
            total_files=len(all_files),
            directories=sorted(directories),
            files=all_files,
            source_files=source_files,
            test_files=test_files,
            config_files=config_files,
            languages=language_counts,
            primary_language=primary_language,
            skipped_files=skipped,
        )

        if read_source:
            exploration.source_evidence = self._read_source_files(
                all_files, root, skipped
            )

        return exploration

    # ── Private helpers ───────────────────────────────────────────────────────

    def _is_inside_root(self, path: Path, root: Path) -> bool:
        """Return True iff ``path`` is inside (or equal to) ``root``."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _should_ignore_dir(self, dirname: str) -> bool:
        return dirname in IGNORED_DIRS or dirname.startswith(".")

    def _detect_language(self, path: Path) -> str:
        # Check full filename first (e.g. "Dockerfile")
        if path.name in FILENAME_LANGUAGE_MAP:
            return FILENAME_LANGUAGE_MAP[path.name]
        # Check for .env.* patterns
        if path.name.startswith(".env"):
            return "Config"
        return EXTENSION_LANGUAGE_MAP.get(path.suffix.lower(), "Unknown")

    def _is_test_file(self, path: Path, root: Path) -> bool:
        name = path.name
        rel = path.relative_to(root).as_posix()

        # Pattern: test_*.py / *_test.py
        if name.startswith("test_") and name.endswith(".py"):
            return True
        if name.endswith("_test.py"):
            return True
        # Pattern: *.test.ts / *.spec.ts / *.test.js / *.spec.js / *.test.tsx
        if any(
            name.endswith(s)
            for s in (".test.ts", ".spec.ts", ".test.js", ".spec.js",
                      ".test.tsx", ".spec.tsx", ".test.jsx", ".spec.jsx")
        ):
            return True
        # Pattern: file lives in a tests/ or test/ directory
        parts = rel.split("/")
        if any(p in ("tests", "test", "__tests__") for p in parts[:-1]):
            return True
        return False

    def _is_secret_file(self, path: Path) -> bool:
        name = path.name.lower()
        # Exact filename matches (.env, .env.local, etc.)
        if name in {p.lower() for p in SECRET_FILENAME_PATTERNS}:
            return True
        # .env.* prefix
        if name.startswith(".env"):
            return True
        # Extensions (.pem, .key, etc.)
        if path.suffix.lower() in SECRET_EXTENSION_PATTERNS:
            return True
        # Substrings in filename (e.g. "db_password.txt")
        for substr in SECRET_NAME_SUBSTRINGS:
            if substr in name:
                return True
        return False

    def _is_binary(self, path: Path) -> bool:
        """Detect binary files by reading the first 512 bytes."""
        try:
            with path.open("rb") as f:
                header = f.read(512)
            # Check known binary signatures
            for sig in BINARY_SIGNATURES:
                if header.startswith(sig):
                    return True
            # Null byte heuristic
            if b"\x00" in header:
                return True
            return False
        except OSError:
            return True  # treat unreadable as binary

    def _read_source_files(
        self,
        files: list[FileMetadata],
        root: Path,
        skipped: list[SkippedFile],
    ) -> list[SourceFileEvidence]:
        """Read contents of non-secret, non-binary, within-limit source files."""
        evidence: list[SourceFileEvidence] = []

        for meta in files:
            rel = meta.relative_path
            path = (root / rel).resolve()

            # Security: do not read secrets
            if meta.is_secret:
                skipped.append(SkippedFile(rel, "secret"))
                continue

            # Size guard
            if meta.size_bytes > self.max_file_bytes:
                skipped.append(SkippedFile(rel, "exceeds_size_limit"))
                continue

            # Binary guard
            if self._is_binary(path):
                skipped.append(SkippedFile(rel, "binary"))
                continue

            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                skipped.append(SkippedFile(rel, f"read_error: {exc}"))
                continue

            evidence.append(
                SourceFileEvidence(
                    relative_path=rel,
                    language=meta.language,
                    content=content,
                    line_count=content.count("\n") + 1,
                    size_bytes=meta.size_bytes,
                )
            )

        return evidence
