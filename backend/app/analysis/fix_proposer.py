"""
Fix proposal engine — Phase 9.

Responsibilities
----------------
Given the root cause Phase 8 recorded, produce a **minimal, reviewable** change
and a standard unified diff for it, in memory.  Nothing is applied, nothing is
executed, and no file in the analysed repository is written — ever.

Why Phase 9 reads source at all
--------------------------------
Phases 7 and 8 never open a repository file: they reason over evidence that was
already extracted and sanitised.  Phase 9 is the first phase that *must* look at
the file again, because a diff that does not reflect what is actually on disk is
worse than no diff.  So this module contains the single new capability in the
system — a constrained, read-only source reader — and it is deliberately narrow:

* the path must be repository-relative, with no ``..`` and no drive/root prefix
* the resolved path must stay inside the run's own repository root
* secret files (``.env``, ``*.pem``, ``*token*``, …) are refused, reusing the
  Phase 3 ``explorer`` rules so a diff can never leak a credential
* oversized and non-UTF-8 files are refused rather than guessed at

Determinism
-----------
This is a pure function of ``(RootCauseAnalysis, repository contents)``.  There
is no clock, no randomness and no model call, so the same root cause against the
same source always yields a byte-identical proposal — which is what makes the
proposal reviewable, diffable and cacheable.

A note on the LLM
-----------------
The architecture allows an ``AIProvider``-backed proposer, and this module is
the seam where one would plug in behind :class:`FixProposer`.  Phase 9 ships the
deterministic engine only.  That is a deliberate choice, not an omission: the
product's central claim is that a proposal is derived from cited evidence, and a
free-form generation step cannot be held to that standard by construction.  The
interface is here so adding one later is a change, not a rewrite.

Honesty boundaries
------------------
* A proposal is never applied and never tested.  ``tests_executed`` is hard-wired
  ``False``; only Phase 11's sandboxed executor may produce a ``VERIFIED_RESULT``.
* Only ``TRUNCATION_BEFORE_SCALING`` has an automated minimal fix.  Every other
  root-cause kind returns ``INSUFFICIENT_EVIDENCE`` rather than a guess.
* The rewrite fires only on an expression the Phase 8 detector also recognises,
  and only when that expression occurs **exactly once** in the file.  Zero
  matches means the evidence is stale; more than one means the change is
  ambiguous.  Both refuse to produce a diff.
"""

from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path, PurePosixPath

from app.analysis.evidence_synthesizer import detect_truncation_before_scaling
from app.ingestion.explorer import (
    DEFAULT_MAX_FILE_BYTES,
    SECRET_EXTENSION_PATTERNS,
    SECRET_NAME_SUBSTRINGS,
)
from app.models.fix_proposal import (
    AnchorLocation,
    FixChange,
    FixProposal,
    FixProposalStatus,
    RiskAssessment,
    RiskLevel,
    ValidationPlan,
)
from app.models.root_cause import (
    ConfidenceLevel,
    RootCauseAnalysis,
    RootCauseKind,
    SourceAnchor,
)

logger = logging.getLogger(__name__)


# ── Errors ────────────────────────────────────────────────────────────────────


class FixProposalError(Exception):
    """Base class for proposal failures that are not honest verdicts."""


class UnsafeSourcePathError(FixProposalError):
    """The requested path is not one this module will read.

    Traversal attempts, absolute paths, symlink escapes and secret files all
    land here.  They are reported as ``INVALID`` proposals rather than raised,
    because "I refuse to look at that file" is an answer the developer needs to
    see, not a crash.
    """


class SourceUnavailableError(FixProposalError):
    """The file exists and is safe to name, but its text is not usable."""


# ── The constrained reader ────────────────────────────────────────────────────


def is_secret_relative_path(relative_path: str) -> bool:
    """Return True when ``relative_path`` names a file Phase 3 refuses to read.

    Mirrors ``RepositoryExplorer._is_secret_file`` but works from the
    repository-relative string, so it can be applied to an *anchor* — which is
    untrusted data derived from evidence — before any path is joined.  The
    Phase 3 constants are imported rather than restated so the two definitions
    cannot drift apart.
    """
    name = PurePosixPath(relative_path).name.lower()
    if name.startswith(".env"):
        return True
    if PurePosixPath(relative_path).suffix.lower() in SECRET_EXTENSION_PATTERNS:
        return True
    return any(substr in name for substr in SECRET_NAME_SUBSTRINGS)


def read_source_text(root: Path, relative_path: str) -> str:
    """Read one repository-relative source file, safely, or raise.

    Args:
        root: The run's repository root, already resolved and validated by the
            Phase 3 ``RepositoryCloner`` when the stage ran.
        relative_path: A repository-relative POSIX path, such as
            ``app/services/discount_service.py``.

    Raises:
        UnsafeSourcePathError: The path is absolute, traverses upward, escapes
            ``root``, or names a secret file.
        SourceUnavailableError: The path is not a readable UTF-8 text file, or
            the file exceeds ``DEFAULT_MAX_FILE_BYTES``.

    Raises :class:`UnicodeDecodeError` never: a non-UTF-8 file is refused as
    :class:`SourceUnavailableError` rather than propagating, because a
    non-propagating refusal is what lets a stage report a verdict instead of
    crashing.
    """
    target = resolve_safe_source_path(root, relative_path)
    data = target.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceUnavailableError(
            f"Path {relative_path!r} is not UTF-8 text, so no textual diff "
            f"can be built from it."
        ) from exc


def resolve_safe_source_path(root: Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` to a real file inside ``root``, or raise.

    This is the single place in Phase 9 and Phase 11 where a repository-relative
    string becomes a ``Path``, and it is deliberately shared rather than
    reimplemented:

    * :func:`read_source_text` is Phase 9's only read, and
    * :class:`app.services.fix_applier.FixApplier` is Phase 11's only write.

    Both need exactly the same guarantees, and a second copy of these checks is a
    second set of rules that can drift.  A future edit that widened one of them
    would therefore widen the *write* path too — which is why the checks below are
    written to be tight rather than convenient, and why the write side
    re-validates rather than trusting this function's result.

    Args:
        root: The run's repository root, already resolved and validated by the
            Phase 3 ``RepositoryCloner`` when the stage ran.
        relative_path: A repository-relative POSIX path, such as
            ``app/services/discount_service.py``.

    Raises:
        UnsafeSourcePathError: The path is empty, has surrounding whitespace, is
            absolute, traverses upward, carries a drive letter, escapes ``root``
            through a symlink, or names a secret file.
        SourceUnavailableError: The path does not name a regular file, or the
            file is too large to be read.
    """
    if not relative_path or not relative_path.strip():
        raise UnsafeSourcePathError("An empty path is not a source location.")
    if relative_path != relative_path.strip():
        raise UnsafeSourcePathError(
            f"Path {relative_path!r} has surrounding whitespace and was not "
            f"produced by the explorer; refusing to normalise it."
        )

    candidate = PurePosixPath(relative_path)
    if candidate.is_absolute() or relative_path.startswith(("/", "\\")):
        raise UnsafeSourcePathError(
            f"Path {relative_path!r} is absolute; only repository-relative "
            f"paths are read."
        )
    if ".." in candidate.parts:
        raise UnsafeSourcePathError(
            f"Path {relative_path!r} contains '..' and was refused."
        )
    if re.match(r"^[A-Za-z]:", relative_path):
        raise UnsafeSourcePathError(
            f"Path {relative_path!r} carries a drive letter; only "
            f"repository-relative paths are read."
        )
    if is_secret_relative_path(relative_path):
        raise UnsafeSourcePathError(
            f"Path {relative_path!r} names a file whose contents are never "
            f"read, so no diff can be built from it."
        )

    resolved_root = root.resolve()
    # ``strict=False`` so a path that does not exist resolves anyway and can be
    # reported as "not a file here" rather than raising inside ``Path.resolve``.
    target = (resolved_root / candidate).resolve()
    # A repository-relative path that resolves outside the root has escaped via a
    # symlink, a junction or a reparse point.  ``relative_to`` is the check that
    # catches all three, which is why it is done on the *resolved* path and not on
    # the joined one.
    try:
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafeSourcePathError(
            f"Path {relative_path!r} resolves outside the repository root."
        ) from exc

    if not target.is_file():
        raise SourceUnavailableError(
            f"Path {relative_path!r} does not name a file in this repository."
        )

    try:
        size = target.stat().st_size
    except OSError as exc:  # pragma: no cover - permissions vary by platform
        raise SourceUnavailableError(
            f"Path {relative_path!r} could not be inspected: "
            f"{exc.strerror or exc}."
        ) from exc
    if size > DEFAULT_MAX_FILE_BYTES:
        raise SourceUnavailableError(
            f"Path {relative_path!r} is {size} bytes, over the "
            f"{DEFAULT_MAX_FILE_BYTES}-byte read limit."
        )

    return target


# ── The deterministic rewriter ───────────────────────────────────────────────
#
# The rewrite mirrors the Phase 8 detector exactly: a cast (``int``, ``float``,
# ``round``, …) applied to a division, multiplied by a factor *outside* the
# cast.  The minimal repair is to move the multiplication inside the cast, so the
# remainder is scaled before it is discarded:
#
#     int(a / b) * c     ->  int(a * c / b)
#
# It is intentionally conservative.  It refuses, rather than guesses, when the
# expression is not of that shape, when the cast is nested inside another call,
# or when more than one operator is involved.  A proposal that declines to fire
# is recoverable; a proposal that rewrites the wrong line is not.

#: A quantising call.  Kept in step with the Phase 8 detector by
#: ``test_rewriter_only_fires_where_the_detector_fires``.
_CAST_NAME_RE = re.compile(r"\b(?:int|float|complex|round|floor|ceil|trunc)\s*\(")

#: A ``#`` that starts a comment rather than appearing inside a string: either at
#: the start of the line or preceded by whitespace.
_COMMENT_START_RE = re.compile(r"(?:^|(?<=\s))#")

_OPEN_BRACKETS = "([{"
_CLOSE_BRACKETS = ")]}"
_QUOTES = "'\""

#: Operator spellings that must not be mistaken for a bare ``*`` or ``/``.
_COMPOUND_OPERATORS = ("**", "*=", "//", "/=")

#: An operand the rewriter is prepared to move, as a grouped pattern fragment.
#: Deliberately narrow: a name, a dotted name, an optional single subscript, or a
#: numeric literal.  Anything with a top-level operator, a call, or a conditional
#: is refused, because moving a compound expression across a cast changes what the
#: code means.  For example ``int(a + b / c) * d`` must not become
#: ``int(a + b * d / c)`` — that is a different program, so the rewriter declines
#: instead.  The fragment is written pre-grouped and without ``re.VERBOSE`` so it
#: can be embedded in the operand-position patterns below without its top-level
#: alternation splitting an enclosing group.
_SIMPLE_OPERAND = (
    r"(?:"
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\[[^\[\]]+\])?"
    r"|[0-9][0-9_]*(?:\.[0-9_]+)?"
    r")"
)

#: A whole operand, anchored.
_SIMPLE_OPERAND_RE = re.compile(rf"\A{_SIMPLE_OPERAND}\Z")

#: A multiplier written *after* the cast: ``  * factor`` and nothing else.
_TRAILING_FACTOR_RE = re.compile(rf"\s*\*\s*(?P<factor>{_SIMPLE_OPERAND})\s*\Z")

#: A multiplier written *before* the cast, consuming the whole prefix: an
#: optional assignment target, then ``factor * ``.  The prefix must be *entirely*
#: an assignment target and the factor and the operator, so the rewriter can
#: never drop code it does not understand.
_LEADING_FACTOR_RE = re.compile(
    rf"\A(?P<prefix>[A-Za-z_][A-Za-z0-9_]*\s*=\s*)?"
    rf"(?P<factor>{_SIMPLE_OPERAND})\s*\*\s*\Z"
)


def split_code_and_comment(line: str) -> tuple[str, str]:
    """Split a source line into ``(code, comment)``.

    The comment starts at the ``#`` itself, so the code half keeps the
    whitespace in front of it and the two parts reassemble the line
    byte-for-byte.  Only a ``#`` at the start of the line or preceded by
    whitespace is treated as a comment opener, which is the same heuristic the
    rest of the project uses.  Note that a ``#`` inside a string literal can
    still be mistaken for a comment; the rewriter only applies this to lines it
    has already matched against a known expression shape, and it re-derives the
    rewrite from the untouched line, so a mistaken split can only produce a
    no-change result that is then refused.
    """
    match = _COMMENT_START_RE.search(line)
    if match is None:
        return line, ""
    return line[: match.start()], line[match.start() :]


def find_top_level_operator(text: str, operator: str) -> int | None:
    """Index of the first ``operator`` at bracket depth zero outside a string.

    Returns ``None`` when there is no such operator.  Brackets and string
    literals are skipped so ``int(f(x) / y)`` and ``"a/b"`` cannot be mistaken
    for arithmetic at the top level.
    """
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in _QUOTES:
            index = _skip_string_literal(text, index)
            continue
        if char in _OPEN_BRACKETS:
            depth += 1
        elif char in _CLOSE_BRACKETS:
            depth -= 1
        elif depth == 0 and text.startswith(operator, index):
            if not any(
                text.startswith(compound, index) for compound in _COMPOUND_OPERATORS
            ):
                return index
            index += 2
            continue
        index += 1
    return None


def _skip_string_literal(text: str, start: int) -> int:
    """Return the index just past the string literal beginning at ``start``."""
    quote = text[start]
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    return len(text)


def _cast_argument_span(text: str, open_paren: int) -> tuple[int, int] | None:
    """Return the ``(start, end)`` span of a call's argument, by bracket balance.

    ``None`` when the brackets never balance, which happens on a truncated
    snippet.  An unbalanced snippet is "cannot analyse", never a match.
    """
    depth = 0
    for index in range(open_paren, len(text)):
        char = text[index]
        if char in _OPEN_BRACKETS:
            depth += 1
        elif char in _CLOSE_BRACKETS:
            depth -= 1
            if depth == 0:
                return open_paren + 1, index
    return None


def rewrite_truncation_before_scaling(line: str) -> str | None:
    """Move the scaling inside the cast, or return ``None``.

    ``int(a / b) * c`` becomes ``int(a * c / b)``: the fractional remainder is
    scaled before it is discarded instead of after.  ``None`` means "this line is
    not a shape I will rewrite", and the caller turns that into an honest
    ``INSUFFICIENT_EVIDENCE`` proposal rather than a guess.

    Indentation, a leading ``return``, the cast's own name, surrounding
    whitespace and any trailing comment are all preserved, so the result differs
    from the input only in the arithmetic.
    """
    code, comment = split_code_and_comment(line)
    stripped = code.rstrip()
    trailing = code[len(stripped) :]

    for match in _CAST_NAME_RE.finditer(stripped):
        open_paren = stripped.find("(", match.start())
        if open_paren == -1:  # pragma: no cover - the regex guarantees a paren
            continue
        span = _cast_argument_span(stripped, open_paren)
        if span is None:
            continue

        argument = stripped[span[0] : span[1]]
        division = find_top_level_operator(argument, "/")
        if division is None:
            continue
        left = argument[:division].strip()
        right = argument[division + 1 :].strip()
        if not _is_simple_operand(left) or not _is_simple_operand(right):
            continue

        cast_name = stripped[match.start() : open_paren]
        before = stripped[: match.start()]
        after = stripped[span[1] + 1 :]

        factor, prefix, suffix = _external_factor(before, after)
        if factor is None:
            continue

        rewritten = f"{prefix}{cast_name}({left} * {factor} / {right}){suffix}"
        return f"{rewritten}{trailing}{comment}"

    return None


def _is_simple_operand(text: str) -> bool:
    """True when ``text`` is an operand the rewriter is prepared to move."""
    return bool(_SIMPLE_OPERAND_RE.match(text.strip()))


def _external_factor(before: str, after: str) -> tuple[str | None, str, str]:
    """Split the text around a cast into its external multiplier.

    Returns ``(factor, prefix, suffix)`` where ``prefix + CAST(...) + suffix``
    reproduces the original arrangement, or ``(None, "", "")`` when the cast is
    not simply multiplied by one operand.

    Both arrangements are handled — ``int(a / b) * c`` and ``a * int(a / b)`` —
    and in each case the operand must consume its whole side of the expression.
    ``foo(int(a / b)) * c`` therefore refuses, because a factor buried inside a
    call is not a shape this rewriter is prepared to reason about.
    """
    trailing_match = _TRAILING_FACTOR_RE.match(after)
    if trailing_match is not None:
        # ``<prefix>CAST(...) * <factor>`` — the whole suffix is the factor.
        return trailing_match.group("factor"), before, ""

    leading_match = _LEADING_FACTOR_RE.match(before)
    if leading_match is not None and not after.strip():
        # ``<target> = <factor> * CAST(...)`` — the whole prefix is an optional
        # assignment plus the factor, and there is nothing following the cast that
        # a rewritten expression could reorder.
        return leading_match.group("factor"), leading_match.group("prefix") or "", after

    return None, "", ""


# ── Locating the anchor in the current source ─────────────────────────────────


def locate_anchor(lines: list[str], anchor: SourceAnchor) -> tuple[int | None, str]:
    """Find the single line the anchor refers to.

    Returns ``(index, "")`` on success and ``(None, reason)`` on refusal.  The
    reason is developer-facing, because "your evidence is stale" and "your
    evidence is ambiguous" need different responses.

    The rule is deliberately strict: the anchor's excerpt must occur **exactly
    once** in the file.  Zero occurrences means the source moved on since Phase 8
    read it; two or more means the change cannot be attributed to a location.
    Either way a diff would be a guess.
    """
    excerpt = (anchor.excerpt or "").strip()
    anchor_line = anchor.line

    if excerpt:
        matches = [
            index
            for index, text in enumerate(lines)
            if text.strip() == excerpt
        ]
        if len(matches) == 1:
            return matches[0], ""
        if not matches:
            return None, (
                f"The source recorded at line {anchor_line} of {anchor.file_path} "
                f"no longer exists in the file: `{excerpt}`. The repository "
                f"changed after the investigation, so a diff built from the old "
                f"reading would not apply. Re-run the investigation."
            )
        return None, (
            f"`{excerpt}` occurs {len(matches)} times in {anchor.file_path}, so "
            f"the root cause cannot be attributed to a single location. This is "
            f"refused rather than guessed at."
        )

    # No excerpt: the line number is the only locator, so the defect has to be
    # re-confirmed at that exact line before anything is proposed.
    if anchor_line is None or not (1 <= anchor_line <= len(lines)):
        return None, (
            f"{anchor.file_path} has {len(lines)} line(s) and the anchor carries "
            f"no usable line number or excerpt, so the defect cannot be located."
        )
    return anchor_line - 1, ""


# ── Diff construction ─────────────────────────────────────────────────────────


def build_unified_diff(
    relative_path: str, before: str, after: str
) -> str:
    """Return a standard unified diff, generated entirely in memory.

    Paths are repository-relative ``a/``/``b/`` prefixes, so the diff is
    applicable with ``git apply -p1`` from the repository root and never discloses
    the absolute layout of the machine that produced it.
    """
    diff_lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{relative_path}",
        tofile=f"b/{relative_path}",
        n=3,
    )
    return "".join(diff_lines)


# ── Risk ──────────────────────────────────────────────────────────────────────

_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
}

#: Deterministic risk signals, evaluated in this order so the reported factors
#: are always in the same sequence.  Each entry is
#: ``(label, level-if-matched, substrings)``, matched case-insensitively against
#: the changed path, function and lines.
_RISK_SIGNALS: tuple[tuple[str, RiskLevel, tuple[str, ...]], ...] = (
    (
        "touches an authentication, permission or cryptographic surface",
        RiskLevel.HIGH,
        (
            "auth", "permission", "role", "token", "password", "credential",
            "crypto", "signature", "session", "acl", "admin",
        ),
    ),
    (
        "touches a persistence or data-model surface",
        RiskLevel.HIGH,
        (
            "migration", "schema", "database", "sqlmodel", "alembic",
            "sessionmaker", "create_all", "drop_table",
        ),
    ),
    (
        "touches a public API or request-handling surface",
        RiskLevel.MEDIUM,
        ("route", "router", "endpoint", "api/", "app/api", "handler", "request", "response"),
    ),
    (
        "touches an external dependency or network surface",
        RiskLevel.MEDIUM,
        ("http", "requests.", "urllib", "httpx", "client", "gateway", "webhook", "sdk"),
    ),
    (
        "changes money, billing or a quantity that reaches a reported total",
        RiskLevel.MEDIUM,
        (
            "price", "discount", "tax", "invoice", "billing", "payment", "amount",
            "cents", "currency", "subtotal", "total", "balance", "fee", "charge",
        ),
    ),
    (
        "deletes or replaces a whole module rather than a line",
        RiskLevel.MEDIUM,
        ("__all__", "class ", "def __init__"),
    ),
)


def assess_risk(changes: list[FixChange]) -> RiskAssessment:
    """Derive a named risk level and the factors behind it.

    Purely mechanical: the level is the strongest signal the changed text
    matches, escalated when the change is broader than one file.  There is no
    model call and no judgement call, so the same change always reports the same
    risk and a reviewer can argue with a specific factor rather than a score.
    """
    if not changes:
        return RiskAssessment(
            level=RiskLevel.LOW,
            factors=[],
            rationale=(
                "No change is proposed, so there is nothing to review, apply or "
                "revert."
            ),
        )

    haystack = " ".join(
        part
        for change in changes
        for part in (
            change.file_path,
            change.function or "",
            change.before,
            change.after,
        )
    ).lower()

    factors: list[str] = []
    level = RiskLevel.LOW
    for label, signal_level, terms in _RISK_SIGNALS:
        if any(term in haystack for term in terms):
            factors.append(label)
            if _RISK_ORDER[signal_level] > _RISK_ORDER[level]:
                level = signal_level

    affected = sorted({change.file_path for change in changes})
    if len(affected) > 1:
        factors.append(f"spans {len(affected)} files, so a partial application is possible")
        if _RISK_ORDER[level] < _RISK_ORDER[RiskLevel.MEDIUM]:
            level = RiskLevel.MEDIUM

    if not factors:
        factors.append("localised change with no recognised high-blast-radius surface")

    return RiskAssessment(
        level=level,
        factors=factors,
        rationale=(
            f"{len(changes)} line-level change(s) across {len(affected)} file(s). "
            f"Strongest matching signal sets the level at {level.value}. This is "
            f"a mechanical rating from the changed text, not a prediction of "
            f"what the change will do at runtime — nothing here has been run."
        ),
    )


# ── Validation plan ───────────────────────────────────────────────────────────


def build_validation_plan(
    analysis: RootCauseAnalysis, changes: list[FixChange]
) -> ValidationPlan:
    """Return the checks a developer should perform.  None are performed here.

    The wording is imperative on purpose: these are instructions for a person,
    not results.  ``tests_executed`` is pinned ``False`` and the notes say so, so
    a client cannot render the plan as a record of verification.
    """
    steps: list[str] = []

    if changes:
        affected = ", ".join(sorted({c.file_path for c in changes}))
        total = sum(
            (c.line_end or c.line) - c.line + 1 for c in changes
        )
        steps.append(
            f"Review the proposed {total}-line change to {affected} before "
            f"applying anything."
        )
        steps.append(
            "Apply the diff in a working branch and confirm the hunk applies "
            "cleanly with no surrounding edits."
        )

    steps.append(
        f"Reproduce the reported behaviour first: {analysis.issue.strip()}"
    )
    steps.append(
        "Re-run the project's existing test suite; changing a computed value "
        "can move an expectation that was never asserted precisely."
    )

    if analysis.root_cause_kind is RootCauseKind.TRUNCATION_BEFORE_SCALING:
        steps.append(
            "Check an input where the division is exact: the corrected "
            "expression must return the same value as the current one."
        )
        steps.append(
            "Check an input where it is not exact: the corrected expression "
            "must return the intended value, not a truncated one."
        )
        steps.append(
            "Confirm the cast should still truncate rather than round, and say "
            "so explicitly if rounding is intended."
        )

    steps.append(
        "Add a regression test that fails before this change and passes after it."
    )
    steps.append(
        "Have a second reviewer confirm the risk assessment before merging."
    )

    return ValidationPlan(
        steps=steps,
        tests_executed=False,
        notes=(
            "Phase 9 drafted this plan and executed none of it. tests_executed "
            "is False by construction: only the sandboxed executor of Phase 11 "
            "may produce a VERIFIED_RESULT, and no file in the repository was "
            "modified to produce this proposal."
        ),
    )


# ── The proposal ──────────────────────────────────────────────────────────────

#: Assumptions that hold for every proposal Phase 9 can make, regardless of
#: whether a change was produced.
_BASE_LIMITATIONS: tuple[str, ...] = (
    "No test was executed. This is a static reading of the source plus a "
    "hand-built diff, not a fix that was tried and observed to work.",
    "Only TRUNCATION_BEFORE_SCALING has an automated minimal fix in Phase 9. "
    "Other root-cause kinds need human design and are reported as "
    "insufficient evidence rather than guessed at.",
    "The proposal addresses the single defect site Phase 8 ranked highest. "
    "Contributing factors and unresolved questions are not fixed by it.",
    "The diff was generated in memory. No file in the repository was created, "
    "modified or deleted, and applying it remains a human decision.",
)

_BASE_ASSUMPTIONS: tuple[str, ...] = (
    "The fix intent is the one Phase 8 recorded, derived from cited evidence "
    "rather than from the wording of this proposal.",
)


def _not_proposed(
    analysis: RootCauseAnalysis,
    status: FixProposalStatus,
    rationale: str,
    *,
    assumptions: list[str] | None = None,
    extra_limitations: list[str] = (),
) -> FixProposal:
    """Build a proposal that deliberately contains no change and no diff.

    ``changes`` stays empty and ``unified_diff`` stays ``None``, so a client
    cannot render "no fix" as a zero-length change.
    """
    return FixProposal(
        run_id=analysis.run_id,
        issue=analysis.issue,
        status=status,
        rationale=rationale,
        root_cause=analysis.root_cause,
        root_cause_kind=analysis.root_cause_kind,
        root_cause_confidence=analysis.confidence,
        root_cause_confidence_level=analysis.confidence_level,
        root_cause_status=analysis.status.value,
        anchor=AnchorLocation.from_anchor(analysis.anchor),
        supporting_evidence=list(analysis.supporting_evidence),
        affected_files=[],
        changes=[],
        unified_diff=None,
        risk=assess_risk([]),
        validation_plan=ValidationPlan(
            steps=analysis.recommended_next_investigation,
            tests_executed=False,
            notes=(
                "No change was proposed, so there is nothing to validate. These "
                "are the investigation steps Phase 8 recorded; none were run."
            ),
        ),
        assumptions=list(assumptions or _BASE_ASSUMPTIONS),
        limitations=[*_BASE_LIMITATIONS, *extra_limitations],
        tests_executed=False,
    )


def _insufficient(analysis: RootCauseAnalysis, rationale: str) -> FixProposal:
    return _not_proposed(analysis, FixProposalStatus.INSUFFICIENT_EVIDENCE, rationale)


def _invalid(analysis: RootCauseAnalysis, rationale: str) -> FixProposal:
    return _not_proposed(
        analysis,
        FixProposalStatus.INVALID,
        rationale,
        extra_limitations=(
            "The root cause itself is unaffected; only the source it points at "
            "could not be trusted to build a diff against.",
        ),
    )


class FixProposer:
    """Deterministic proposal generator. Stateless and safe to reuse.

    The reader is injectable purely so tests can supply a fixture tree without a
    repository on disk; the default is the constrained
    :func:`read_source_text`.
    """

    def __init__(self, reader=read_source_text) -> None:
        self._reader = reader

    def propose(self, analysis: RootCauseAnalysis, repository_root: Path) -> FixProposal:
        """Return the proposal for ``analysis`` against ``repository_root``.

        Never raises for an expected outcome: "no root cause", "not confident
        enough", "no automated fix for this kind", "the source moved", "the
        anchor is ambiguous" and "that file will not be read" are all returned
        as proposals carrying the appropriate status.  An unexpected exception is
        left to the stage, which fails the run loudly rather than inventing a
        verdict.
        """
        if not analysis.is_determined() or analysis.root_cause is None:
            return _insufficient(
                analysis,
                (
                    f"No fix is proposed because Phase 8 did not determine a "
                    f"root cause (status {analysis.status.value}). Proposing a "
                    f"change to a repository whose defect is unknown is how a "
                    f"tool starts making things worse. "
                    f"{len(analysis.unresolved_questions)} unresolved "
                    f"question(s) are recorded on the analysis."
                ),
            )

        if analysis.confidence_level is ConfidenceLevel.LOW:
            return _insufficient(
                analysis,
                (
                    f"No fix is proposed because the root cause is only "
                    f"{analysis.confidence_level.value} confidence "
                    f"({analysis.confidence}). A lead is not a licence to edit "
                    f"code; gather more evidence first."
                ),
            )

        if analysis.root_cause_kind is not RootCauseKind.TRUNCATION_BEFORE_SCALING:
            return _insufficient(
                analysis,
                (
                    f"No fix is proposed because Phase 9 has an automated "
                    f"minimal fix only for "
                    f"{RootCauseKind.TRUNCATION_BEFORE_SCALING.value}, and this "
                    f"root cause is "
                    f"{analysis.root_cause_kind.value if analysis.root_cause_kind else 'unclassified'}"
                    f". A change here needs human design, not a template."
                ),
            )

        anchor = analysis.anchor
        if (
            anchor is None
            or not anchor.file_path
            or anchor.line is None
            or not anchor.excerpt
        ):
            return _invalid(
                analysis,
                (
                    "No fix is proposed because the root cause does not point "
                    "at a complete source location: a diff can only be built "
                    "from an exact file, line and source excerpt."
                ),
            )

        try:
            original = self._reader(repository_root, anchor.file_path)
        except UnsafeSourcePathError as exc:
            return _invalid(analysis, f"No fix is proposed. {exc}")
        except SourceUnavailableError as exc:
            return _invalid(analysis, f"No fix is proposed. {exc}")

        lines = original.splitlines()
        index, reason = locate_anchor(lines, anchor)
        if index is None:
            return _invalid(analysis, f"No fix is proposed. {reason}")

        current = lines[index]
        if not detect_truncation_before_scaling(current):
            # The anchor matched by exact excerpt, so the source has *not* moved
            # on since the investigation — it still reads what Phase 8 recorded.
            # That rules out the stale-evidence explanation, and claiming it here
            # would send the reviewer to re-run an investigation for no reason.
            # What remains is that this expression is not the shape the
            # diagnosis generalises to, so Phase 9 declines to touch it.
            return _insufficient(
                analysis,
                (
                    f"No fix is proposed because line {index + 1} of "
                    f"{anchor.file_path} still reads exactly as the "
                    f"investigation recorded it, but `{current.strip()}` is not "
                    f"the division-then-scale shape this fix applies to. Phase 9 "
                    f"will not rewrite an expression it has not recognised as "
                    f"the diagnosed defect; this site needs human design."
                ),
            )

        rewritten = rewrite_truncation_before_scaling(current)
        if rewritten is None:
            return _insufficient(
                analysis,
                (
                    f"No fix is proposed because `{current.strip()}` is not a "
                    f"shape Phase 9 will rewrite automatically. The rewriter "
                    f"handles a single cast of a single division scaled by one "
                    f"operand; anything else is left to a human rather than "
                    f"approximated."
                ),
            )

        if rewritten == current:
            return _invalid(
                analysis,
                (
                    "No fix is proposed because the rewrite produced no "
                    "change, which means the source and the proposed repair "
                    "disagree."
                ),
            )

        change = FixChange(
            file_path=anchor.file_path,
            line=index + 1,
            function=anchor.function,
            before=current,
            after=rewritten,
            description=(
                f"Scale the value before the cast truncates it, instead of "
                f"truncating first and scaling the discarded remainder. "
                f"`{current.strip()}` becomes `{rewritten.strip()}`."
            ),
        )

        updated = _replace_line(
            original.splitlines(keepends=True), index, rewritten
        )
        diff = build_unified_diff(anchor.file_path, original, updated)
        if not diff:
            return _invalid(
                analysis,
                "No fix is proposed because the change produced an empty diff, "
                "which means the rewrite did not alter the file.",
            )

        return FixProposal(
            run_id=analysis.run_id,
            issue=analysis.issue,
            status=FixProposalStatus.READY_FOR_REVIEW,
            rationale=_rationale(analysis, change),
            root_cause=analysis.root_cause,
            root_cause_kind=analysis.root_cause_kind,
            root_cause_confidence=analysis.confidence,
            root_cause_confidence_level=analysis.confidence_level,
            root_cause_status=analysis.status.value,
            anchor=AnchorLocation.from_anchor(anchor),
            supporting_evidence=list(analysis.supporting_evidence),
            affected_files=[anchor.file_path],
            changes=[change],
            unified_diff=diff,
            risk=assess_risk([change]),
            validation_plan=build_validation_plan(analysis, [change]),
            assumptions=[
                *_BASE_ASSUMPTIONS,
                f"The source at {anchor.file_path}:{anchor.line} still reads as "
                f"the investigation recorded it; it was re-read and confirmed "
                f"before this diff was built.",
                "Moving the multiplication inside the cast is the intended "
                "repair: the remainder should be scaled before it is "
                "discarded, not after.",
                "Only the named line is defective. Neighbouring lines were read "
                "to locate it and were not modified.",
            ],
            limitations=list(_BASE_LIMITATIONS),
            tests_executed=False,
        )


def _replace_line(lines: list[str], index: int, replacement: str) -> str:
    """Return the file text with one line replaced, preserving its terminator.

    ``lines`` must be ``splitlines(keepends=True)`` output, so the original
    line's own end-of-line sequence is reused: a CRLF file stays CRLF, and a
    file with no trailing newline keeps having none.  Re-joining with ``"\\n"``
    would silently rewrite the whole file's line endings and produce a diff
    spanning every line.
    """
    updated = list(lines)
    original = lines[index]
    terminator = ""
    for candidate in ("\r\n", "\n", "\r"):
        if original.endswith(candidate):
            terminator = candidate
            break
    updated[index] = f"{replacement}{terminator}"
    return "".join(updated)


def _rationale(analysis: RootCauseAnalysis, change: FixChange) -> str:
    return (
        f"Phase 8 determined that {analysis.root_cause} The value is truncated "
        f"inside the cast and only then scaled, so the remainder is discarded "
        f"before it is multiplied and every non-exact multiple is wrong. The "
        f"minimal repair is to move the scaling inside the cast "
        f"({change.file_path}:{change.line}), which changes "
        f"{change.before.strip()} to {change.after.strip()} and nothing else. "
        f"The change is offered on {analysis.confidence_level.value} "
        f"confidence ({analysis.confidence}) from "
        f"{len(analysis.supporting_evidence)} cited evidence item(s), and it "
        f"has not been tested: applying it is a human decision."
    )


#: A ready-to-use proposer.  The class holds no state, so one instance per
#: process is enough.
DEFAULT_FIX_PROPOSER = FixProposer()


__all__ = [
    "DEFAULT_FIX_PROPOSER",
    "FixProposer",
    "FixProposalError",
    "SourceUnavailableError",
    "UnsafeSourcePathError",
    "assess_risk",
    "build_unified_diff",
    "build_validation_plan",
    "find_top_level_operator",
    "is_secret_relative_path",
    "locate_anchor",
    "read_source_text",
    "resolve_safe_source_path",
    "rewrite_truncation_before_scaling",
    "split_code_and_comment",
]
