"""
Evidence construction for Phase 7 — the honesty enforcement point.

Every Phase 7 evidence item is created through :func:`build_evidence`, which
refuses to build anything the phase is not allowed to claim.  The rules are
enforced in code rather than by convention so a mistake fails loudly:

``VERIFIED_RESULT`` is rejected
    Phase 7 executes nothing, so it verifies nothing.  A verified result may
    only be produced by a real test run with an exit code (Phase 11).

``OBSERVATION`` must be traceable
    An observation is a fact read from the repository, so it must carry a
    repository-relative ``file_path``.  Claims about the repository as a whole
    (counts, languages) legitimately have no single file; those must be marked
    ``repository_level=True`` instead of pointing at a made-up line.

``HYPOTHESIS`` must carry a confidence
    A hypothesis is a guess.  Leaving the confidence off would let a guess
    masquerade as a finding.

``PROPOSED_ACTION`` is investigative only
    Phase 7 may propose the *next investigation step* ("reproduce this
    combination").  It may not propose a code fix — that is Phase 9, behind the
    human approval gate.

Also enforced here: a per-investigator cap on the number of evidence items, so
a large repository cannot flood the store.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from app.models.domain import Evidence, EvidenceType
from app.models.investigation import (
    InvestigatorName,
    SourceLocation,
    SourceReference,
)
from app.models.workflow import utc_now

#: Evidence types Phase 7 may produce.  ``VERIFIED_RESULT`` is deliberately absent.
ALLOWED_EVIDENCE_TYPES: frozenset[EvidenceType] = frozenset(
    {EvidenceType.OBSERVATION, EvidenceType.HYPOTHESIS, EvidenceType.PROPOSED_ACTION}
)

#: Substrings that would turn an investigative next step into a code change.
#: Verbs and phrases that mean "edit the repository's code".  Matched on word
#: boundaries rather than as bare substrings, because ``fix`` also occurs inside
#: "fixture" and "prefix" — a legitimate investigative instruction like "add a
#: test fixture" must not be mistaken for a code-change proposal.
_FIX_LANGUAGE = (
    "fix",
    "fixes",
    "fixed",
    "patch",
    "refactor",
    "rewrite",
    "implement",
    "modify",
    "edit the",
    "update the",
    "replace the",
    "swap the",
    "hard-code",
    "hardcode",
)

#: Compiled once: a whole-word occurrence of any of the phrases above.
_FIX_LANGUAGE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _FIX_LANGUAGE) + r")\b"
)


class EvidencePolicyError(ValueError):
    """Raised when an investigator tries to emit evidence it may not emit."""


def _is_unsafe_evidence_path(path: str) -> bool:
    """True when ``path`` points outside the session's repository.

    Rule 10 requires every file access to stay inside the cloned session
    directory, and an evidence citation is a claim about a specific file, so the
    same rule applies to the paths it names.  Three cases are rejected:

    * absolute paths, including Windows drive-qualified ones such as
      ``C:\\Users\\...`` and UNC paths, which do not begin with a separator
    * ``..`` segments, which escape upwards
    * empty paths, handled by the caller

    ``PurePosixPath`` is used after normalising separators so the check behaves
    identically on Windows and POSIX hosts.
    """
    if not path:
        return True
    if path.startswith(("/", "\\")):
        return True
    # ``C:foo`` / ``C:/foo`` — a drive-qualified path is absolute on Windows
    # even without a leading separator, and is meaningless relative to any repo.
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return True
    return ".." in PurePosixPath(path.replace("\\", "/")).parts


def build_evidence(
    *,
    run_id: str,
    investigator: InvestigatorName,
    evidence_type: EvidenceType,
    title: str,
    description: str,
    location: SourceLocation | None = None,
    snippet: str | None = None,
    confidence: float | None = None,
    supports: list[str] | None = None,
    references: list[SourceReference] | None = None,
    repository_level: bool = False,
    method: str = "static_analysis",
    extra_metadata: dict[str, Any] | None = None,
) -> Evidence:
    """Build one validated, traceable :class:`Evidence` item.

    Args:
        run_id: The owning workflow run.
        investigator: Which investigator produced this item.
        evidence_type: Must be one of ``OBSERVATION``/``HYPOTHESIS``/
            ``PROPOSED_ACTION``.
        title: Short statement, e.g. ``"Route POST /checkout calls checkout()"``.
        description: The supporting detail, phrased as what was observed.
        location: Where it was read.  Required unless ``repository_level``.
        snippet: Quoted source, already truncated by the caller.
        confidence: ``0.0``–``1.0``.  Required for ``HYPOTHESIS``, forbidden
            for ``OBSERVATION`` (an observation is either read or it is not).
        supports: IDs of evidence items this one leans on.  A hypothesis should
            point at the observations that motivated it.
        references: Secondary locations that also support the statement.
        repository_level: True for claims about the whole repository, which have
            no single file.
        method: How it was derived, e.g. ``"ast_call_graph"``, ``"text_scan"``.
        extra_metadata: Investigator-specific detail.

    Raises:
        EvidencePolicyError: If any rule above is violated.
    """
    # ── 1. Type must be permitted at all ───────────────────────────────────
    if evidence_type not in ALLOWED_EVIDENCE_TYPES:
        raise EvidencePolicyError(
            f"Phase 7 may not emit {evidence_type.value}. "
            f"Only {sorted(t.value for t in ALLOWED_EVIDENCE_TYPES)} are allowed; "
            f"VERIFIED_RESULT requires actual test execution in a later phase."
        )

    # ── 2. Traceability ────────────────────────────────────────────────────
    if evidence_type is EvidenceType.OBSERVATION and location is None and not repository_level:
        raise EvidencePolicyError(
            f"OBSERVATION must cite a repository-relative file path "
            f"(or set repository_level=True): {title!r}"
        )

    if location is not None and not location.file_path:
        raise EvidencePolicyError(
            f"Evidence location must carry a file path: {title!r}"
        )
    if location is not None and _is_unsafe_evidence_path(location.file_path):
        raise EvidencePolicyError(
            f"Evidence must cite a repository-relative path inside the session "
            f"repository, not {location.file_path!r}"
        )

    # ── 3. Confidence semantics ────────────────────────────────────────────
    if evidence_type is EvidenceType.HYPOTHESIS and confidence is None:
        raise EvidencePolicyError(
            f"HYPOTHESIS must state a confidence so it cannot be read as a "
            f"fact: {title!r}"
        )
    if confidence is not None and not (0.0 <= confidence <= 1.0):
        raise EvidencePolicyError(
            f"Confidence must be between 0.0 and 1.0, got {confidence}"
        )
    if evidence_type is EvidenceType.OBSERVATION and confidence is not None:
        raise EvidencePolicyError(
            f"OBSERVATION must not carry a confidence value: {title!r}"
        )

    # ── 4. Phase 7 proposes investigation steps, never code fixes ───────────
    if evidence_type is EvidenceType.PROPOSED_ACTION:
        offenders = _FIX_LANGUAGE_RE.findall(f"{title} {description}".lower())
        if offenders:
            raise EvidencePolicyError(
                f"Phase 7 must not propose code changes (found {sorted(set(offenders))}); "
                f"fix proposals belong to Phase 9 behind the approval gate. "
                f"Got: {title!r}"
            )

    metadata: dict[str, Any] = {
        "investigator": investigator.value,
        "method": method,
        "phase": 7,
    }
    if location is not None:
        if location.line_start is not None:
            metadata["line_start"] = location.line_start
        if location.line_end is not None:
            metadata["line_end"] = location.line_end
    if repository_level:
        metadata["repository_level"] = True
    if supports:
        metadata["supports"] = list(supports)
    if references:
        metadata["references"] = [r.model_dump(mode="json") for r in references]
    if evidence_type is EvidenceType.HYPOTHESIS and confidence is not None:
        metadata["confidence"] = confidence
    if extra_metadata:
        metadata.update(extra_metadata)

    return Evidence(
        session_id=run_id,
        evidence_type=evidence_type,
        agent=investigator.value,
        title=title.strip(),
        description=description.strip(),
        source_file=location.file_path if location else None,
        source_line=location.line_start if location else None,
        raw_content=snippet,
        confidence=confidence,
        timestamp=utc_now(),
        metadata=metadata,
    )


class EvidenceBuilder:
    """Per-investor evidence accumulator with a hard item cap.

    Each investigator owns one builder, which is what keeps the three
    investigators independent: they share the read-only context but write only
    into their own list.
    """

    def __init__(
        self,
        run_id: str,
        investigator: InvestigatorName,
        max_items: int,
    ) -> None:
        if max_items < 1:
            raise ValueError("max_items must be at least 1")
        self.run_id = run_id
        self.investigator = investigator
        self.max_items = max_items
        self._items: list[Evidence] = []
        self._truncated = False

    def __len__(self) -> int:
        return len(self._items)

    @property
    def items(self) -> list[Evidence]:
        """The accumulated evidence, in production order."""
        return list(self._items)

    @property
    def truncated(self) -> bool:
        """True when the cap was hit and some findings were dropped."""
        return self._truncated

    def add(self, evidence: Evidence) -> Evidence | None:
        """Append ``evidence`` unless the cap has been reached.

        Returns the stored item, or None when the cap rejected it.
        """
        if len(self._items) >= self.max_items:
            self._truncated = True
            return None
        self._items.append(evidence)
        return evidence

    # ── Typed shorthands ──────────────────────────────────────────────────

    def observation(self, **kwargs: Any) -> Evidence | None:
        return self.add(
            build_evidence(
                run_id=self.run_id,
                investigator=self.investigator,
                evidence_type=EvidenceType.OBSERVATION,
                **kwargs,
            )
        )

    def hypothesis(self, **kwargs: Any) -> Evidence | None:
        return self.add(
            build_evidence(
                run_id=self.run_id,
                investigator=self.investigator,
                evidence_type=EvidenceType.HYPOTHESIS,
                **kwargs,
            )
        )

    def proposed_action(self, **kwargs: Any) -> Evidence | None:
        return self.add(
            build_evidence(
                run_id=self.run_id,
                investigator=self.investigator,
                evidence_type=EvidenceType.PROPOSED_ACTION,
                **kwargs,
            )
        )


__all__ = [
    "ALLOWED_EVIDENCE_TYPES",
    "EvidenceBuilder",
    "EvidencePolicyError",
    "build_evidence",
]
