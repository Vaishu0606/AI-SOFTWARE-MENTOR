"""
Phase 9 tests — the deterministic fix proposer.

These cover the engine in isolation: the constrained reader, the rewriter, anchor
locating, diff construction, risk and validation, and the honesty rules that make
a proposal reviewable at all (no writes, no claims of verification, byte-identical
output for identical input).
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from app.analysis.evidence_synthesizer import detect_truncation_before_scaling
from app.analysis.fix_proposer import (
    DEFAULT_FIX_PROPOSER,
    FixProposer,
    SourceUnavailableError,
    UnsafeSourcePathError,
    assess_risk,
    build_unified_diff,
    build_validation_plan,
    find_top_level_operator,
    is_secret_relative_path,
    locate_anchor,
    read_source_text,
    rewrite_truncation_before_scaling,
    split_code_and_comment,
)
from app.ingestion.explorer import DEFAULT_MAX_FILE_BYTES
from app.models.fix_proposal import (
    AnchorLocation,
    ChangeType,
    FixChange,
    FixProposal,
    FixProposalStatus,
    RiskLevel,
)
from app.models.root_cause import (
    ConfidenceBreakdown,
    ConfidenceLevel,
    RootCauseAnalysis,
    RootCauseKind,
    RootCauseStatus,
    SourceAnchor,
)

BUGGY = "    return int(subtotal_cents / 100) * code.value"
FIXED = "    return int(subtotal_cents * code.value / 100)"


# ── Fixtures / builders ───────────────────────────────────────────────────────


def make_repo(tmp_path: Path, body: str = BUGGY + "\n", name: str = "pricing.py") -> Path:
    repo = tmp_path / "Repo"
    (repo / "app" / "services").mkdir(parents=True, exist_ok=True)
    (repo / "app" / "services" / name).write_text(
        "def calculate_discount_cents(subtotal_cents, code):\n" + body,
        encoding="utf-8",
    )
    return repo


def make_analysis(
    *,
    line: int = 2,
    excerpt: str = BUGGY.strip(),
    status: RootCauseStatus = RootCauseStatus.DETERMINED,
    kind: RootCauseKind = RootCauseKind.TRUNCATION_BEFORE_SCALING,
    level: ConfidenceLevel = ConfidenceLevel.MEDIUM,
    root_cause: str | None = "Truncation happens before scaling.",
    file_path: str = "app/services/pricing.py",
    function: str | None = "calculate_discount_cents",
) -> RootCauseAnalysis:
    """A Phase 8 analysis shaped exactly like the one the workflow produces."""
    return RootCauseAnalysis(
        run_id="11111111-1111-4111-8111-111111111111",
        issue="Checkout is failing when a discount code is applied.",
        status=status,
        root_cause=root_cause,
        root_cause_kind=kind if root_cause else None,
        anchor=(
            SourceAnchor(
                file_path=file_path,
                line=line,
                function=function,
                excerpt=excerpt,
            )
            if line is not None
            else None
        ),
        confidence=0.48,
        confidence_level=level,
        confidence_rationale="One direct observation, no corroboration.",
        confidence_breakdown=ConfidenceBreakdown(
            base=0.6,
            corroboration_bonus=0.0,
            score=0.48,
            level=level,
            rationale="test fixture",
        ),
        supporting_evidence=[],
        tests_executed=False,
        verified_result_count=0,
    )


# ── The rewriter ──────────────────────────────────────────────────────────────


def test_rewriter_moves_the_scaling_inside_the_cast() -> None:
    assert rewrite_truncation_before_scaling(BUGGY) == FIXED


def test_rewriter_handles_the_leading_multiplier_form() -> None:
    assert (
        rewrite_truncation_before_scaling("y = ratio * int(a / b)")
        == "y = int(a * ratio / b)"
    )
    assert (
        rewrite_truncation_before_scaling("ratio * int(a / b)")
        == "int(a * ratio / b)"
    )


@pytest.mark.parametrize(
    ("cast", "expected"),
    [
        ("int(a / b) * c", "int(a * c / b)"),
        ("float(a / b) * c", "float(a * c / b)"),
        ("round(a / b) * c", "round(a * c / b)"),
        ("int(a / b) * 0.9", "int(a * 0.9 / b)"),
        ("x = int(a/b) * rate", "x = int(a * rate / b)"),
    ],
)
def test_rewriter_covers_the_quantising_casts(cast: str, expected: str) -> None:
    assert rewrite_truncation_before_scaling(cast) == expected


def test_rewriter_preserves_indentation_and_a_trailing_comment() -> None:
    assert (
        rewrite_truncation_before_scaling("        int(a / b) * c  # note")
        == "        int(a * c / b)  # note"
    )


@pytest.mark.parametrize(
    "line",
    [
        # Already correct: the scaling happens before the cast.
        "int(a * b / c) * d",
        # Not the defect: a cast of a variable, no division inside.
        "int(ratio) * c",
        # A compound operand: moving it across the cast would change meaning.
        "int(a + b / c) * d",
        "int(a / b) * (c + 1)",
        "int(a / b) * c + 1",
        # The cast is nested inside another call.
        "foo(int(a / b)) * c",
        # Not a defect at all.
        "x = 1",
        "total = subtotal + tax",
    ],
)
def test_rewriter_refuses_shapes_it_cannot_rewrite_safely(line: str) -> None:
    assert rewrite_truncation_before_scaling(line) is None


def test_rewriter_never_fires_where_the_phase8_detector_is_silent() -> None:
    """The rewrite target and the detector must agree.

    Phase 8 only reports ``TRUNCATION_BEFORE_SCALING`` when
    ``detect_truncation_before_scaling`` is true.  If the rewriter fired on
    something the detector ignores, Phase 9 would be editing a line nobody
    diagnosed — so the invariant is asserted directly rather than assumed.
    """
    corpus = [
        "int(a/b)*c", "int(a * b / c) * d", "x=1", "int(a/b)", "round(a/b)*c",
        "a*int(b/c)", "float(a/b)*c", "int(a/b)*c+d", "int(f(a)/b)*c",
        "return int(a/b)*c", "int(a+b/c)*d", "int(a/b)*(c+1)", "int(a/b)*c  # x",
    ]
    fired_without_detection = [
        line for line in corpus
        if rewrite_truncation_before_scaling(line) is not None
        and not detect_truncation_before_scaling(line)
    ]
    assert fired_without_detection == []


def test_rewritten_expression_actually_fixes_the_arithmetic() -> None:
    """The rewrite is not just a string swap: it changes the computed value.

    ``int(1000/100) * 1.5`` truncates to 10 then scales to 15, losing the
    fractional part of 1000 cents scaled by 1.5.  The rewritten expression
    scales first and truncates once.
    """
    def buggy(subtotal_cents: int, value: float) -> int:
        return int(subtotal_cents / 100) * value

    def fixed(subtotal_cents: int, value: float) -> int:
        return int(subtotal_cents * value / 100)

    # An exact multiple is unchanged: no regression on the previously-correct path.
    assert buggy(1000, 1.5) == fixed(1000, 1.5) == 15
    # A non-exact multiple is now correct rather than truncated.
    assert buggy(1050, 1.5) == 15
    assert fixed(1050, 1.5) == 15
    assert buggy(1001, 1.0) == 10
    assert fixed(1001, 1.0) == 10
    # A case where truncation visibly under-charges before the fix.
    assert buggy(999, 1.0) == 9
    assert fixed(999, 1.0) == 9


def test_find_top_level_operator_skips_brackets_and_strings() -> None:
    assert find_top_level_operator("a / b", "/") == 2
    assert find_top_level_operator("f(a / b)", "/") is None
    assert find_top_level_operator("'a / b'", "/") is None
    assert find_top_level_operator('"a / b" / c', "/") == 8
    assert find_top_level_operator("a // b", "/") is None
    assert find_top_level_operator("a ** b", "*") is None


def test_split_code_and_comment_only_splits_a_real_comment() -> None:
    # The comment starts at the '#', so code keeps the whitespace before it and
    # the two halves reassemble to the original line byte-for-byte.
    assert split_code_and_comment("x = 1  # note") == ("x = 1  ", "# note")
    assert split_code_and_comment("x = '#' ") == ("x = '#' ", "")
    assert split_code_and_comment("x = 1") == ("x = 1", "")
    code, comment = split_code_and_comment("x = 1  # note")
    assert code + comment == "x = 1  # note"


# ── The constrained reader ────────────────────────────────────────────────────


def test_reader_returns_source_for_a_relative_path(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    text = read_source_text(repo, "app/services/pricing.py")
    assert BUGGY.strip() in text


@pytest.mark.parametrize(
    "path",
    [
        "../../../etc/passwd",
        "app/../../outside.py",
        "..",
        "/etc/passwd",
        "C:/Windows/System32/config/SAM",
        "app/services/../../../secret.py",
    ],
)
def test_reader_refuses_traversal_and_absolute_paths(
    tmp_path: Path, path: str
) -> None:
    repo = make_repo(tmp_path)
    with pytest.raises(UnsafeSourcePathError):
        read_source_text(repo, path)


@pytest.mark.parametrize(
    "path",
    [".env", "app/.env", "config/.env.production", "id_rsa.pem", "certs/x.key",
     "app/db_password.txt", "app/api_token.py", "secrets/credentials.yaml"],
)
def test_reader_refuses_secret_files(tmp_path: Path, path: str) -> None:
    repo = make_repo(tmp_path)
    with pytest.raises(UnsafeSourcePathError):
        read_source_text(repo, path)


def test_is_secret_relative_path_matches_phase3_rules() -> None:
    assert is_secret_relative_path(".env") is True
    assert is_secret_relative_path("nested/dir/.env.local") is True
    assert is_secret_relative_path("tls/server.key") is True
    assert is_secret_relative_path("app/services/discount.py") is False


def test_reader_refuses_a_missing_file(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    with pytest.raises(SourceUnavailableError):
        read_source_text(repo, "app/services/nope.py")


def test_reader_refuses_oversized_and_binary_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    big = repo / "big.py"
    big.write_text("x = 1\n" * (DEFAULT_MAX_FILE_BYTES // 4 + 10), encoding="utf-8")
    with pytest.raises(SourceUnavailableError):
        read_source_text(repo, "big.py")

    binary = repo / "blob.py"
    binary.write_bytes(b"\xff\xfe\x00 int(a / b) * c")
    with pytest.raises(SourceUnavailableError):
        read_source_text(repo, "blob.py")


def test_reader_refuses_empty_and_padded_paths(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    for path in ("", "   ", " app/services/pricing.py"):
        with pytest.raises(UnsafeSourcePathError):
            read_source_text(repo, path)


def test_reader_refuses_a_directory(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    with pytest.raises(SourceUnavailableError):
        read_source_text(repo, "app/services")


# ── Anchor locating ───────────────────────────────────────────────────────────


def test_locate_anchor_accepts_a_unique_excerpt() -> None:
    lines = ["def f():", "    return int(a / b) * c"]
    index, reason = locate_anchor(lines, SourceAnchor(file_path="x.py", line=2, excerpt="return int(a / b) * c"))
    assert reason == ""
    assert index == 1


def test_locate_anchor_refuses_an_ambiguous_excerpt() -> None:
    """Two identical lines mean the change cannot be attributed to a location."""
    lines = ["def f():", "    return int(a / b) * c", "def g():", "    return int(a / b) * c"]
    index, reason = locate_anchor(lines, SourceAnchor(file_path="x.py", line=2, excerpt="return int(a / b) * c"))
    assert index is None
    assert "occurs 2 times" in reason


def test_locate_anchor_refuses_stale_evidence() -> None:
    lines = ["def f():", "    return 0"]
    index, reason = locate_anchor(lines, SourceAnchor(file_path="x.py", line=2, excerpt="return int(a / b) * c"))
    assert index is None
    assert "no longer exists" in reason


def test_locate_anchor_falls_back_to_the_line_when_there_is_no_excerpt() -> None:
    lines = ["def f():", "    return int(a / b) * c"]
    index, reason = locate_anchor(lines, SourceAnchor(file_path="x.py", line=2))
    assert reason == ""
    assert index == 1

    index, reason = locate_anchor(lines, SourceAnchor(file_path="x.py", line=99))
    assert index is None
    assert reason


# ── Diff construction ─────────────────────────────────────────────────────────


def test_unified_diff_is_standard_and_uses_relative_paths() -> None:
    before = "a = 1\nb = 2\nc = 3\n"
    after = "a = 1\nb = 99\nc = 3\n"
    diff = build_unified_diff("app/x.py", before, after)
    assert "--- a/app/x.py" in diff
    assert "+++ b/app/x.py" in diff
    assert "-b = 2" in diff
    assert "+b = 99" in diff
    # Never discloses the absolute layout of the machine that produced it.
    assert str(Path.cwd()) not in diff


def test_unified_diff_matches_difflib_output() -> None:
    before = "a = 1\nb = 2\n"
    after = "a = 1\nb = 3\n"
    expected = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/app/x.py",
            tofile="b/app/x.py",
            n=3,
        )
    )
    assert build_unified_diff("app/x.py", before, after) == expected


# ── Risk and validation ───────────────────────────────────────────────────────


def test_risk_is_empty_when_nothing_is_proposed() -> None:
    risk = assess_risk([])
    assert risk.level is RiskLevel.LOW
    assert risk.factors == []
    assert "nothing to review" in risk.rationale


def test_risk_flags_monetary_arithmetic() -> None:
    change = FixChange(
        file_path="app/services/discount_service.py",
        line=52,
        function="calculate_discount_cents",
        before=BUGGY,
        after=FIXED,
    )
    risk = assess_risk([change])
    assert risk.level is RiskLevel.MEDIUM
    assert any("money" in f for f in risk.factors)


def test_risk_flags_auth_surfaces_as_high() -> None:
    change = FixChange(
        file_path="app/auth/session.py",
        line=10,
        function="verify_token",
        before="return int(a / b) * token_scale",
        after="return int(a * token_scale / b)",
    )
    assert assess_risk([change]).level is RiskLevel.HIGH


def test_risk_is_deterministic() -> None:
    change = FixChange(
        file_path="app/services/pricing.py", line=1, before="int(a/b)*c", after="int(a*c/b)"
    )
    assert assess_risk([change]).model_dump() == assess_risk([change]).model_dump()


def test_validation_plan_never_claims_anything_was_run() -> None:
    analysis = make_analysis()
    change = FixChange(
        file_path="app/services/pricing.py",
        line=2,
        before=BUGGY,
        after=FIXED,
    )
    plan = build_validation_plan(analysis, [change])
    assert plan.tests_executed is False
    assert plan.steps
    assert any("regression test" in s.lower() for s in plan.steps)
    assert "Phase 9 drafted this plan" in plan.notes


# ── The proposer end to end (no orchestrator) ─────────────────────────────────


def test_proposer_produces_a_ready_reviewable_diff(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    proposal = DEFAULT_FIX_PROPOSER.propose(make_analysis(), repo)

    assert proposal.status is FixProposalStatus.READY_FOR_REVIEW
    assert proposal.is_ready_for_review() is True
    assert proposal.unified_diff is not None
    assert FIXED.strip() in proposal.unified_diff
    assert BUGGY.strip() in proposal.unified_diff
    assert proposal.affected_files == ["app/services/pricing.py"]
    assert len(proposal.changes) == 1
    assert proposal.changes[0].before.strip() == BUGGY.strip()
    assert proposal.changes[0].after.strip() == FIXED.strip()
    assert proposal.changes[0].change_type is ChangeType.MODIFY
    assert proposal.tests_executed is False
    assert proposal.risk.level is RiskLevel.MEDIUM
    assert proposal.validation_plan.tests_executed is False


def test_proposer_does_not_write_to_the_repository(tmp_path: Path) -> None:
    """The whole point: the diff is built in memory and the tree is untouched."""
    repo = make_repo(tmp_path)
    target = repo / "app" / "services" / "pricing.py"
    before_bytes = target.read_bytes()
    before_tree = {p: p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()}

    DEFAULT_FIX_PROPOSER.propose(make_analysis(), repo)

    assert target.read_bytes() == before_bytes
    assert {p: p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()} == before_tree
    # And the defect is still there, unfixed.
    assert BUGGY.strip() in target.read_text(encoding="utf-8")


def test_proposer_is_deterministic_across_identical_input(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    first = DEFAULT_FIX_PROPOSER.propose(make_analysis(), repo)
    second = DEFAULT_FIX_PROPOSER.propose(make_analysis(), repo)
    assert first.model_dump() == second.model_dump()


def test_proposer_declines_when_no_root_cause_was_determined(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    analysis = make_analysis(status=RootCauseStatus.INSUFFICIENT_EVIDENCE, root_cause=None)
    proposal = DEFAULT_FIX_PROPOSER.propose(analysis, repo)

    assert proposal.status is FixProposalStatus.INSUFFICIENT_EVIDENCE
    assert proposal.unified_diff is None
    assert proposal.changes == []
    assert proposal.affected_files == []
    assert proposal.is_ready_for_review() is False
    assert proposal.root_cause is None


def test_proposer_declines_on_low_confidence(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    analysis = make_analysis(level=ConfidenceLevel.LOW)
    proposal = DEFAULT_FIX_PROPOSER.propose(analysis, repo)
    assert proposal.status is FixProposalStatus.INSUFFICIENT_EVIDENCE
    assert proposal.unified_diff is None
    assert "LOW confidence" in proposal.rationale


def test_proposer_declines_for_a_root_cause_kind_it_cannot_fix(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    analysis = make_analysis(kind=RootCauseKind.UNEXERCISED_PATH)
    proposal = DEFAULT_FIX_PROPOSER.propose(analysis, repo)
    assert proposal.status is FixProposalStatus.INSUFFICIENT_EVIDENCE
    assert proposal.unified_diff is None
    assert "UNEXERCISED_PATH" in proposal.rationale


def test_proposer_marks_an_incomplete_anchor_invalid(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    analysis = make_analysis(line=None, excerpt="")  # anchor is None
    proposal = DEFAULT_FIX_PROPOSER.propose(analysis, repo)
    assert proposal.status is FixProposalStatus.INVALID
    assert proposal.unified_diff is None
    assert "complete source location" in proposal.rationale


def test_proposer_refuses_a_traversal_anchor(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    analysis = make_analysis(file_path="../../../etc/passwd")
    proposal = DEFAULT_FIX_PROPOSER.propose(analysis, repo)
    assert proposal.status is FixProposalStatus.INVALID
    assert proposal.unified_diff is None
    assert ".." in proposal.rationale


def test_proposer_refuses_a_secret_anchor(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / ".env").write_text("STRIPE_SECRET_KEY=sk_live_leak\n", encoding="utf-8")
    analysis = make_analysis(file_path=".env", excerpt="STRIPE_SECRET_KEY=sk_live_leak")
    proposal = DEFAULT_FIX_PROPOSER.propose(analysis, repo)
    assert proposal.status is FixProposalStatus.INVALID
    assert "sk_live_leak" not in proposal.model_dump_json()
    assert proposal.unified_diff is None


def test_proposer_refuses_when_the_source_moved_on(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, body="    return 0\n")
    proposal = DEFAULT_FIX_PROPOSER.propose(make_analysis(), repo)
    assert proposal.status is FixProposalStatus.INVALID
    assert proposal.unified_diff is None
    assert "no longer exists" in proposal.rationale


def test_proposer_refuses_an_ambiguous_anchor(tmp_path: Path) -> None:
    """The same buggy line twice: a diff would be a guess, so none is offered."""
    repo = tmp_path / "Repo"
    (repo / "app" / "services").mkdir(parents=True)
    (repo / "app" / "services" / "pricing.py").write_text(
        "def a():\n" + BUGGY + "\ndef b():\n" + BUGGY + "\n",
        encoding="utf-8",
    )
    proposal = DEFAULT_FIX_PROPOSER.propose(make_analysis(), repo)
    assert proposal.status is FixProposalStatus.INVALID
    assert proposal.unified_diff is None
    assert "occurs 2 times" in proposal.rationale


def test_proposer_does_not_claim_stale_evidence_when_the_anchor_still_matches(
    tmp_path: Path,
) -> None:
    """The anchor matched, so the source is current — say that, not "re-run it".

    Claiming the repository had moved on would send the reviewer to repeat an
    investigation whose evidence is in fact still accurate, and would label a
    human-design problem as a tooling problem.
    """
    repo = make_repo(tmp_path, body="    return 0\n")
    proposal = DEFAULT_FIX_PROPOSER.propose(make_analysis(excerpt="    return 0"), repo)
    assert proposal.status is FixProposalStatus.INSUFFICIENT_EVIDENCE
    assert proposal.unified_diff is None
    assert "still reads exactly as the investigation recorded it" in proposal.rationale
    assert "re-run" not in proposal.rationale
    assert "needs human design" in proposal.rationale


def test_proposer_declines_a_shape_it_cannot_rewrite(tmp_path: Path) -> None:
    repo = tmp_path / "Repo"
    (repo / "app" / "services").mkdir(parents=True)
    line = "    return int(subtotal_cents + tax) * code.value"
    (repo / "app" / "services" / "pricing.py").write_text(
        "def calculate_discount_cents(subtotal_cents, code):\n" + line + "\n",
        encoding="utf-8",
    )
    proposal = DEFAULT_FIX_PROPOSER.propose(make_analysis(excerpt=line.strip()), repo)
    assert proposal.status is FixProposalStatus.INSUFFICIENT_EVIDENCE
    assert proposal.unified_diff is None
    assert "not the division-then-scale shape" in proposal.rationale
    # A recognised-but-unrewritable shape reports the rewriter's own reason.
    assert "is not a shape Phase 9 will rewrite" not in proposal.rationale


def test_injected_reader_is_used_and_may_fail(tmp_path: Path) -> None:
    """The reader is injectable so a fixture tree need not exist on disk."""
    calls: list[tuple[Path, str]] = []

    def reader(root: Path, relative_path: str) -> str:
        calls.append((root, relative_path))
        return f"def calculate_discount_cents(subtotal_cents, code):\n{BUGGY}\n"

    proposal = FixProposer(reader=reader).propose(make_analysis(), tmp_path / "virtual")
    assert calls == [(tmp_path / "virtual", "app/services/pricing.py")]
    assert proposal.status is FixProposalStatus.READY_FOR_REVIEW


def test_proposal_summary_round_trips_without_losing_anything() -> None:
    original = FixProposal(
        run_id="11111111-1111-4111-8111-111111111111",
        issue="Checkout is failing when a discount code is applied.",
        status=FixProposalStatus.INSUFFICIENT_EVIDENCE,
        rationale="nothing to do",
    )
    restored = FixProposal.from_summary(original.run_id, original.to_summary())
    assert restored.model_dump() == original.model_dump()
    # Determinism guard: the model carries no clock.
    assert "created_at" not in original.model_dump()
    assert "timestamp" not in original.model_dump()


def test_a_non_ready_proposal_never_carries_a_diff() -> None:
    """The invariant a client relies on to render "no proposal" correctly."""
    for status in (FixProposalStatus.INSUFFICIENT_EVIDENCE, FixProposalStatus.INVALID):
        proposal = FixProposal(
            run_id="11111111-1111-4111-8111-111111111111",
            issue="x",
            status=status,
            rationale="r",
        )
        assert proposal.unified_diff is None
        assert proposal.changes == []
        assert proposal.affected_files == []
        assert proposal.is_ready_for_review() is False
        assert proposal.added_line_count() == 0
        assert proposal.removed_line_count() == 0


def test_a_proposal_cannot_carry_source_excerpts(tmp_path: Path) -> None:
    """Regression guard: the proposal model holds no source excerpt field.

    ``FixProposal.anchor`` is an ``AnchorLocation``, not a ``SourceAnchor``, so
    there is nowhere for quoted source to be stored.  This matters most for files
    Phase 9 refuses to read: a secret path still gets a proposal back, and if the
    model carried an excerpt the refusal response would echo the secret to the
    client and into every log that serialises the proposal.
    """
    proposal = FixProposal(
        run_id="11111111-1111-4111-8111-111111111111",
        issue="x",
        status=FixProposalStatus.INVALID,
        rationale="refused",
        anchor=AnchorLocation(file_path=".env", line=1, function="main"),
    )
    assert "excerpt" not in AnchorLocation.model_fields
    assert "excerpt" not in proposal.model_dump_json()
    # A full SourceAnchor is rejected rather than silently coerced.
    with pytest.raises(Exception):
        FixProposal(
            run_id="11111111-1111-4111-8111-111111111111",
            issue="x",
            status=FixProposalStatus.INVALID,
            rationale="r",
            anchor=SourceAnchor(file_path=".env", line=1, excerpt="SECRET=leak"),
        )


def test_anchor_location_drops_the_excerpt(tmp_path: Path) -> None:
    location = AnchorLocation.from_anchor(
        SourceAnchor(file_path="a.py", line=3, function="f", excerpt="int(a/b)*c")
    )
    assert location.model_dump() == {"file_path": "a.py", "line": 3, "function": "f"}
    assert AnchorLocation.from_anchor(None) is None


def test_diff_line_counts_ignore_file_headers() -> None:
    proposal = FixProposal(
        run_id="11111111-1111-4111-8111-111111111111",
        issue="x",
        status=FixProposalStatus.READY_FOR_REVIEW,
        rationale="r",
        unified_diff="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n",
    )
    assert proposal.added_line_count() == 1
    assert proposal.removed_line_count() == 1
