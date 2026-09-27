#!/usr/bin/env python3
"""Reset the AI Software Mentor hackathon demo repository to its seeded defect.

SCOPE — READ THIS FIRST
-----------------------
This script rewrites exactly one file:

    demo-repository/ShopDemo/app/services/discount_service.py

and nothing else, anywhere. It refuses to run if the resolved target ever
lands outside this repository's own ``demo-repository`` directory, so it cannot
be pointed at a real codebase by accident. The path is derived from this
file's own location, so it behaves identically no matter which directory you
run it from.

It is a DEMO-RESET tool for a DISPOSABLE fixture. ``ShopDemo`` is not real
customer data and carries no secrets. Do not extend it into a general-purpose
"revert my changes" utility, and do not run it against anything but the
checked-in demo repository.

WHY IT EXISTS
-------------
The mentor's approval gate writes the reviewed fix to the repository it was
given. A demo run against ``ShopDemo`` therefore leaves that checkout with the
bug already fixed, and a second run honestly concludes there is nothing to
fix. Resetting restores the seed condition so the demo is repeatable.

WHAT IT GUARANTEES
------------------
* Deterministic. It recognises exactly two states of one line — the seeded bug
  and the fix the mentor proposes. Anything else is a hard error, never a
  guess.
* Idempotent. Running it on an already-buggy file is a successful no-op.
* Lossless. Whole lines only. The neighbouring ``# Correct:`` / ``# Buggy:``
  comments contain both expressions as *text*, so a naive substring replace
  would corrupt them; this script compares a full line and rewrites it in place.
* Verified. After any write it re-reads the file from disk and asserts the
  exact seeded state before reporting success.

USAGE
-----
    python scripts/reset_demo_repo.py            # reset, then verify
    python scripts/reset_demo_repo.py --check    # verify only, change nothing

Exit codes: 0 seeded state confirmed, 1 check failed or content unrecognised,
2 target missing or refused by a safety guard.

Standard library only; no dependencies, no network, no backend involvement.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Constants. The single source of truth for the seed condition.
# --------------------------------------------------------------------------

# Repository root, derived from this file's location (scripts/reset_demo_repo.py).
REPO_ROOT = Path(__file__).resolve().parent.parent

# The only subtree this script is ever allowed to touch.
DEMO_ROOT = (REPO_ROOT / "demo-repository").resolve()

# The one file this script is ever allowed to rewrite.
TARGET = (
    DEMO_ROOT
    / "ShopDemo"
    / "app"
    / "services"
    / "discount_service.py"
).resolve()

# The seeded defect: the division is truncated BEFORE the multiplication, so
# every percent discount is rounded down to whole units of 100 cents first.
BUGGY_RETURN = "return int(subtotal_cents / 100) * code.value"

# The fix the mentor's Phase 9 rewriter proposes: scale first, truncate last.
FIXED_RETURN = "return int(subtotal_cents * code.value / 100)"

# Anchor proving this is the file we think it is. If this is absent, the file
# has been restructured and this script must refuse rather than guess.
FUNCTION_ANCHOR = "def calculate_discount_cents("


class ResetError(RuntimeError):
    """Raised when the target cannot be safely reset or verified."""


def _guard_target() -> Path:
    """Refuse to touch anything outside the checked-in demo repository."""
    if not TARGET.is_relative_to(DEMO_ROOT):
        raise ResetError(
            f"refusing to touch {TARGET}: it is outside {DEMO_ROOT}"
        )
    if not TARGET.is_file():
        raise ResetError(f"demo target not found: {TARGET}")
    return TARGET


def _read_lines(path: Path) -> list[str]:
    """Read the file, refusing anything that is not UTF-8 text."""
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:  # pragma: no cover - defensive
        raise ResetError(f"{path.name} is not UTF-8 text: {exc}") from exc


def _classify(lines: list[str]) -> tuple[int, int]:
    """Return (buggy_index, fixed_index) for whole-line matches only.

    Matching on the stripped line is what keeps the ``# Correct:`` and
    ``# Buggy:`` comment lines intact: they are prefixed with ``#`` and so can
    never compare equal to either constant.
    """
    buggy = [i for i, line in enumerate(lines) if line.strip() == BUGGY_RETURN]
    fixed = [i for i, line in enumerate(lines) if line.strip() == FIXED_RETURN]
    return (buggy[0] if buggy else -1, fixed[0] if fixed else -1)


def verify(path: Path) -> str:
    """Assert the file is in the exact seeded state. Return a short report."""
    lines = _read_lines(path)
    source = "\n".join(lines)

    if FUNCTION_ANCHOR not in source:
        raise ResetError(
            f"{path.name} no longer defines {FUNCTION_ANCHOR!r}; the demo "
            "fixture has been restructured and this script will not guess"
        )

    buggy_hits = [i for i, line in enumerate(lines) if line.strip() == BUGGY_RETURN]
    fixed_hits = [i for i, line in enumerate(lines) if line.strip() == FIXED_RETURN]

    # A truncated or dedented write must never pass as a successful reset.
    try:
        compile(source, str(path), "exec")
    except SyntaxError as exc:
        raise ResetError(
            f"{path.name} is not valid Python after reset (line {exc.lineno}: "
            f"{exc.msg}); refusing to report success"
        ) from exc

    if len(buggy_hits) != 1:
        raise ResetError(
            f"expected exactly one seeded buggy return, found {len(buggy_hits)}"
            + (
                "; the mentor's fix is present, so the demo is not in its "
                "seeded state — run this script without --check"
                if fixed_hits
                else "; the file was edited by hand and this script will not guess"
            )
        )
    if fixed_hits:
        raise ResetError(
            f"file still contains the mentor's fix at line {fixed_hits[0] + 1}"
        )

    # The explanatory comments are part of the fixture; a reviewer reads them.
    for annotation in ("# Correct:", "# Buggy:"):
        if annotation not in source:
            raise ResetError(f"lost the {annotation!r} annotation comment")

    return (
        f"seeded defect present at line {buggy_hits[0] + 1}\n"
        f"    {lines[buggy_hits[0]].strip()}\n"
        "    (division truncates before scaling, so percent discounts are "
        "systematically under-discounted)"
    )


def reset(path: Path) -> str:
    """Restore the seeded defect. Idempotent."""
    lines = _read_lines(path)
    buggy_index, fixed_index = _classify(lines)

    if buggy_index != -1 and fixed_index == -1:
        # Already seeded. Rewriting would be a no-op with needless churn.
        return "already in the seeded buggy state; nothing to do"

    if fixed_index == -1:
        raise ResetError(
            "found neither the seeded bug nor the mentor's fix; the file has "
            "been edited by hand and this script will not guess"
        )

    if buggy_index != -1:
        raise ResetError(
            "file contains both the seeded bug and the fix; refusing to edit an "
            "ambiguous file"
        )

    # Replace only the statement, keeping the line's original indentation.
    # Writing BUGGY_RETURN bare would dedent the return out of its `if` block
    # and produce a file that no longer parses.
    indent = lines[fixed_index][: len(lines[fixed_index]) - len(lines[fixed_index].lstrip())]
    lines[fixed_index] = indent + BUGGY_RETURN
    # newline="" keeps the fixture's existing line endings untouched.
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")

    return f"restored the seeded defect over the mentor's fix (line {fixed_index + 1})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reset the disposable ShopDemo demo repository to its seeded "
            "discount bug. Touches exactly one file."
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the seeded state without writing anything",
    )
    args = parser.parse_args(argv)

    try:
        target = _guard_target()

        if not args.check:
            print(reset(target))
            print()

        print(f"verifying {target.relative_to(REPO_ROOT)}")
        print("  " + verify(target).replace("\n", "\n  "))
        print("\nOK: demo repository is in the intentional buggy state.")
        return 0

    except ResetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
