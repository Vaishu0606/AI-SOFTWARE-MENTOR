"""
Bounded text scans over already-loaded source — Phase 7.

Why this module exists
---------------------
Phase 4 parses code with :mod:`ast` and hands the investigators structured facts.
Two questions still need *within-function* detail the AST model does not carry:

* which attribute is **assigned** in one file and **read** in another, and
* does a relevant function contain an operation that silently loses precision.

The tempting shortcut is to re-parse the file here.  That is rejected on
purpose: parsing untrusted repository text a second time, in a second module,
with a second code path, buys very little and costs a real security surface.
Instead these helpers scan the *lines the Phase 4 model already bounded for us*
(line ranges of known functions) with narrow, side-effect-free regular
expressions.

What that means for honesty
---------------------------
The output is text-level, not semantic.  So every result is reported as a
``method="text_scan"`` observation, phrased as "this text appears here", and any
inference drawn from it is a ``HYPOTHESIS``.  Nothing here executes, imports, or
evaluates the text it reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── Attribute reads and writes ─────────────────────────────────────────────────

#: ``obj.attr =`` / ``obj.attr +=`` / ``obj.attr: T =`` — an attribute write.
_ASSIGN_RE = re.compile(
    r"^\s*(?P<obj>[A-Za-z_][A-Za-z0-9_]*)\.(?P<attr>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s*(?::[^=]+)?(?P<op>=|\+=|-=|\*=|/=)"
)

#: ``obj.attr`` used anywhere other than as the target of an assignment.
_READ_RE = re.compile(
    r"\b(?P<obj>[A-Za-z_][A-Za-z0-9_]*)\.(?P<attr>[A-Za-z_][A-Za-z0-9_]*)\b"
)

#: ``self.attr`` is the same idea; keep the shape uniform for callers.
_SELF_ASSIGN_RE = re.compile(
    r"^\s*self\.(?P<attr>[A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?(?P<op>=|\+=|-=|\*=|/=)"
)

#: Lines that only *look* like an assignment inside a string or comment.
_COMMENT_PREFIXES = ("#", '"""', "'''", "*", "//")


@dataclass(frozen=True)
class AttributeSite:
    """One place an attribute name appears, in one file."""

    file_path: str
    line: int
    attribute: str
    #: The object the attribute is reached through, e.g. ``cart`` or ``self``.
    owner: str
    is_write: bool
    text: str


def _is_noise(line: str) -> bool:
    """True for blank lines, comments and docstring-ish lines."""
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith(_COMMENT_PREFIXES)


def find_attribute_sites(
    file_path: str, lines: list[tuple[int, str]]
) -> list[AttributeSite]:
    """Find attribute reads and writes in ``(line_number, text)`` rows.

    Args:
        file_path: Repository-relative path, used only for labelling.
        lines: ``(line_number, text)`` rows — normally a function's line range as
            reported by Phase 4, so the scan stays inside a known function.

    Returns:
        Sites ordered by line number.
    """
    sites: list[AttributeSite] = []
    for number, text in lines:
        if _is_noise(text):
            continue

        self_assign = _SELF_ASSIGN_RE.match(text)
        if self_assign is not None:
            sites.append(
                AttributeSite(
                    file_path=file_path,
                    line=number,
                    attribute=self_assign.group("attr"),
                    owner="self",
                    is_write=True,
                    text=text.strip(),
                )
            )
            continue

        assign = _ASSIGN_RE.match(text)
        if assign is not None:
            sites.append(
                AttributeSite(
                    file_path=file_path,
                    line=number,
                    attribute=assign.group("attr"),
                    owner=assign.group("obj"),
                    is_write=True,
                    text=text.strip(),
                )
            )
            continue

        for match in _READ_RE.finditer(text):
            sites.append(
                AttributeSite(
                    file_path=file_path,
                    line=number,
                    attribute=match.group("attr"),
                    owner=match.group("obj"),
                    is_write=False,
                    text=text.strip(),
                )
            )
    return sites


def cross_file_attributes(
    sites_by_file: dict[str, list[AttributeSite]],
    min_support: int = 2,
) -> list[tuple[str, str, int, list[AttributeSite]]]:
    """Find attributes written in one file and read in another.

    This is the cheapest honest signal that one module hands a value to another:
    the attribute is defined in one file and consumed in a different one.  It
    says nothing about correctness — only that the two files are coupled.

    Args:
        sites_by_file: Attribute sites grouped by file path.
        min_support: Minimum number of files that must mention the attribute.

    Returns:
        ``(attribute, writing_file, line, all_sites)`` tuples sorted by
        attribute name then writing file, so output is deterministic.
    """
    writes: dict[str, list[AttributeSite]] = {}
    reads: dict[str, list[AttributeSite]] = {}
    for file_path, sites in sites_by_file.items():
        for site in sites:
            bucket = writes if site.is_write else reads
            bucket.setdefault(site.attribute, []).append(site)

    found: list[tuple[str, str, int, list[AttributeSite]]] = []
    for attribute, write_sites in writes.items():
        read_sites = reads.get(attribute, [])
        write_files = {s.file_path for s in write_sites}
        read_files = {s.file_path for s in read_sites}
        if not read_files:
            continue
        if len(write_files | read_files) < min_support:
            continue
        # Only interesting when the write and the read live in *different* files.
        if write_files == read_files and len(write_files) == 1:
            continue
        primary = sorted(write_sites, key=lambda s: (s.file_path, s.line))[0]
        all_sites = sorted(
            write_sites + read_sites, key=lambda s: (s.file_path, s.line)
        )
        found.append((attribute, primary.file_path, primary.line, all_sites))
    found.sort(key=lambda item: (item[0], item[1], item[2]))
    return found


# ── Precision-relevant operations ──────────────────────────────────────────────

#: ``a / b`` or ``a // b`` — a division, the classic silent-truncation site when
#: the operands are integers.
_DIVISION_RE = re.compile(r"(?<![/*])\b[A-Za-z0-9_)\]]\s*(?P<op>//|/)\s*[A-Za-z0-9_(\[]")

#: ``int(x)`` / ``round(x)`` — an explicit numeric conversion.
_CAST_RE = re.compile(r"\b(?P<fn>int|float|round|floor|ceil)\s*\(")

#: Comparison against a sentinel, e.g. ``if x < 0`` — a boundary check.
_COMPARISON_RE = re.compile(r"(?P<lhs>[\w.)]+)\s*(?P<op><|>|<=|>=|==)\s*(?P<rhs>-?\d+)")


@dataclass(frozen=True)
class PrecisionSite:
    """A place inside a function that can alter or check a numeric value."""

    file_path: str
    line: int
    kind: str
    detail: str
    text: str


def find_precision_sites(
    file_path: str, lines: list[tuple[int, str]], limit: int = 20
) -> list[PrecisionSite]:
    """Find divisions, numeric casts and numeric boundary checks in ``lines``.

    Generic static-analysis heuristics, not bug knowledge: they fire on any
    repository.  The caller is responsible for reporting them as things that
    *may* warrant investigation.
    """
    sites: list[PrecisionSite] = []
    for number, text in lines:
        if len(sites) >= limit:
            break
        if _is_noise(text):
            continue
        division = _DIVISION_RE.search(text)
        if division is not None:
            kind = "floor_division" if division.group("op") == "//" else "division"
            sites.append(
                PrecisionSite(
                    file_path=file_path,
                    line=number,
                    kind=kind,
                    detail=f"contains '{division.group(0).strip()}'",
                    text=text.strip(),
                )
            )
        cast = _CAST_RE.search(text)
        if cast is not None:
            sites.append(
                PrecisionSite(
                    file_path=file_path,
                    line=number,
                    kind="numeric_cast",
                    detail=f"calls {cast.group('fn')}()",
                    text=text.strip(),
                )
            )
        comparison = _COMPARISON_RE.search(text)
        if comparison is not None:
            sites.append(
                PrecisionSite(
                    file_path=file_path,
                    line=number,
                    kind="numeric_comparison",
                    detail=(
                        f"compares against {comparison.group('rhs')}"
                    ),
                    text=text.strip(),
                )
            )
    return sites


__all__ = [
    "AttributeSite",
    "PrecisionSite",
    "cross_file_attributes",
    "find_attribute_sites",
    "find_precision_sites",
]
