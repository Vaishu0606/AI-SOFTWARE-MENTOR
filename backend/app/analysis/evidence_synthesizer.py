"""
Evidence synthesis and root-cause analysis — Phase 8.

Turns the Phase 7 :class:`~app.models.investigation.InvestigationResult` into a
:class:`~app.models.root_cause.RootCauseAnalysis`: a conclusion, the confidence
behind it, and — most importantly — a chain of stable evidence IDs that lets a
reviewer walk the claim back to the source lines that support it.

What this module is
-------------------
A **deterministic static reasoner**.  It reads evidence that Phases 3-7 already
recorded and applies a small set of precisely-defined rules.  There is no LLM
call, no randomness, no clock read and no I/O.  Given the same
``InvestigationResult`` it returns a byte-identical analysis, which is what makes
the result reviewable and replayable.

What this module is not
-----------------------
* It does not read the repository.  Every fact comes from evidence.
* It does not execute anything, so it never produces ``VERIFIED_RESULT``.  A
  root cause here is a *lead confirmed by reading code*, not a reproduced bug.
* It does not guess.  When no rule matches, the honest output is
  ``INSUFFICIENT_EVIDENCE`` with the reason recorded.

The rules
---------
``TRUNCATION_BEFORE_SCALING`` (:func:`detect_truncation_before_scaling`)
    A numeric value is cast to an integer/float and *then* scaled, instead of
    being scaled and *then* cast.  ``int(subtotal / 100) * 10`` discards the
    remainder of ``subtotal / 100`` and is therefore wrong for every value that
    is not an exact multiple of 100, in one direction, systematically.  The
    discriminator is precise: the cast must be applied to a division
    (``int(a / b)``) and the multiplication must sit *outside* the cast
    (``int(a / b) * c``).  Scaling inside the cast (``int(a * b / c)``) is
    arithmetically fine and is deliberately not reported.

``UNEXERCISED_PATH``
    Phase 7 observed that route-handling files are never imported by a test
    file.  This is a contributing factor, and on its own it is not a root cause.

``UNCOVERED_INTERACTION``
    Phase 7 observed that no single test exercises two issue-relevant modules
    together.  A defect that only appears when two individually-tested modules
    interact will survive a fully green suite.

Scoring
-------
``support_score`` (candidate ranking) and ``confidence`` (reported band) are
computed by :func:`_score_support` and :func:`_score_confidence`.  Both are
documented at their definitions and both are pure functions of the evidence
counts, so a reviewer can recompute them by hand.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.investigation.relevance import build_issue_terms, split_identifier
from app.models.domain import Evidence, EvidenceType
from app.models.investigation import (
    InvestigationResult,
    InvestigationStatus,
    InvestigatorStatus,
)
from app.models.root_cause import (
    HIGH_CONFIDENCE_THRESHOLD,
    PHASE8_CONSUMED_EVIDENCE_TYPES,
    CallEdgeModel,
    ChainNodeKind,
    ConfidenceBreakdown,
    ConfidenceLevel,
    ContributingFactor,
    ConflictingEvidence,
    CoverageGap,
    DataFlowModel,
    EvidenceChainLink,
    EvidenceRef,
    EvidenceSynthesis,
    FactorCategory,
    NumericSiteModel,
    RootCauseAnalysis,
    RootCauseCandidate,
    RootCauseKind,
    RootCauseStatus,
    RouteFactModel,
    SourceAnchor,
    SynthesisFinding,
    UnresolvedQuestion,
)

logger = logging.getLogger(__name__)


class EvidenceSynthesisError(Exception):
    """Raised when synthesis cannot be attempted at all."""


# ── Configuration ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SynthesisConfig:
    """Deterministic bounds on synthesis output size.

    Defaults bound a single analysis to a size a human will actually read.
    """

    #: Cap on chain hops.  A chain longer than this is truncated, not extended.
    max_chain_links: int = 12
    #: Cap on distinct findings kept as supporting evidence.
    max_supporting_evidence: int = 20
    #: Cap on candidates evaluated (top-ranked kept, rest dropped entirely).
    max_candidates: int = 6
    #: Cap on unresolved questions.
    max_questions: int = 6
    #: Cap on contributing factors.
    max_factors: int = 6


DEFAULT_SYNTHESIS_CONFIG = SynthesisConfig()


# ── Normalisation and deduplication ───────────────────────────────────────────

#: Punctuation and case are not part of a finding's identity, so they are
#: removed before comparing titles.  Whitespace collapses to a single space.
_TITLE_NOISE_RE = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str) -> str:
    """Return a comparison key for an evidence title."""
    return _TITLE_NOISE_RE.sub(" ", title.lower()).strip()


#: Metadata keys that carry no finding identity and are therefore excluded from
#: the deduplication fingerprint.  ``phase``/``investigator`` are provenance
#: labels stamped on every item by the stage, and the evidence ``id``/``timestamp``
#: live on the model rather than in metadata.  Nothing else is dropped: an
#: over-eager fingerprint would silently merge two different findings, which is
#: the one failure mode deduplication must never have.
_NON_SEMANTIC_METADATA_KEYS = frozenset({"phase", "investigator"})


def metadata_fingerprint(item: Evidence) -> str:
    """Return a canonical, content-derived identity string for an evidence item.

    Two items with the same fingerprint are the same finding reported twice.  The
    fingerprint covers the full metadata payload, not just the title: Phase 7 emits
    distinct hypotheses that share a title and description and differ only in the
    evidence they are anchored on, and keying on the title alone would merge them
    and lose a citation.
    """
    relevant = {
        key: value
        for key, value in item.metadata.items()
        if key not in _NON_SEMANTIC_METADATA_KEYS
    }
    return json.dumps(relevant, sort_keys=True, default=str, separators=(",", ":"))


def _evidence_key(item: Evidence) -> tuple[str, str, str, int, str]:
    """Return the content identity of a single finding.

    ``agent`` is deliberately *not* part of the key.  Two investigators reporting
    the same finding is corroboration, and collapsing it would destroy the only
    honest measure of agreement the project has: the group survives as one
    :class:`Finding` carrying both citations and both agent names.

    ``evidence_type`` *is* part of the key.  Merging a ``HYPOTHESIS`` into an
    ``OBSERVATION`` would launder speculation into fact, so two reports of the
    same site stay separate findings unless they agree on how they are labelled.
    """
    return (
        item.evidence_type.value,
        normalize_title(item.title),
        item.source_line or 0,
        item.source_file or "",
        metadata_fingerprint(item),
    )


@dataclass(frozen=True)
class Finding:
    """A deduplicated finding, plus the agents that independently reported it."""

    #: The first item of the group, in canonical order.  Its fields are the ones
    #: reported; the group exists to prove how many times it was observed.
    representative: Evidence
    #: Every evidence item in the group, in canonical order.  Deduplication must
    #: never lose a citation, so all of them stay referenceable — each one keeps
    #: its own agent and epistemic type.
    members: tuple[Evidence, ...] = ()
    #: Every evidence ID in the group, in canonical order.
    evidence_ids: tuple[str, ...] = ()
    #: Distinct agents reporting the same finding, sorted.
    agents: tuple[str, ...] = ()

    @property
    def corroborated(self) -> bool:
        """True when more than one investigator reported the same finding."""
        return len(self.agents) > 1

    @property
    def evidence_id(self) -> str:
        return self.representative.id

    def refs(self) -> list[EvidenceRef]:
        """Return one traceable reference per retained evidence ID."""
        if len(self.evidence_ids) <= 1:
            return [_to_ref(self.representative)]
        by_id = {item.id: item for item in self.members}
        return [
            _to_ref(by_id.get(eid, self.representative)) for eid in self.evidence_ids
        ]


def _to_ref(item: Evidence) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=item.id,
        agent=item.agent,
        evidence_type=item.evidence_type,
        title=item.title,
        source_file=item.source_file,
        source_line=item.source_line,
        method=item.metadata.get("method"),
    )


def _make_finding(group: list[Evidence]) -> Finding:
    """Collapse one content-identical group into a single :class:`Finding`.

    ``group`` is already in canonical order.  Its first item is the
    representative; every item in the group keeps its own referenceable ID and
    agent so the citations stay individually accurate.
    """
    return Finding(
        representative=group[0],
        members=tuple(group),
        evidence_ids=tuple(item.id for item in group),
        agents=tuple(sorted({item.agent for item in group})),
    )


def deduplicate(items: Sequence[Evidence]) -> list[Finding]:
    """Collapse repeated findings into one :class:`Finding` each.

    Output is sorted by the same key used for grouping, so the result does not
    depend on the order in which the concurrent investigators happened to append
    their items.
    """
    groups: dict[tuple[str, str, str, int, str], list[Evidence]] = {}
    for item in items:
        groups.setdefault(_evidence_key(item), []).append(item)
    return [_make_finding(groups[key]) for key in sorted(groups)]


# ── Static pattern detection ──────────────────────────────────────────────────

#: Calls that quantise a value and therefore lose information.
_CAST_CALL_RE = re.compile(r"\b(int|float|complex|round|floor|ceil|trunc)\s*\(")
#: A bare division.  ``**`` and ``//``-style doubled slashes are not division
#: for the purposes of an arithmetic-presence test.
_DIV_RE = re.compile(r"(?<![*/])/(?![*/])")
#: A multiplication, excluding ``**`` and the ``*=`` augmented assignment.
_MUL_RE = re.compile(r"(?<![*/])\*(?![*/=])")
#: ``#`` line comments.  Only ``#`` is stripped: in a Python-shaped repository
#: ``//`` is floor division, and treating it as a comment would corrupt the very
#: expression this rule has to read correctly.  ``re.MULTILINE`` matters because
#: the text may be a multi-line excerpt: a buggy expression mentioned in a
#: comment must never be reported as a defect in the code.
_HASH_COMMENT_RE = re.compile(r"#.*$", re.MULTILINE)


def strip_comment(text: str) -> str:
    """Remove ``#`` comments from a source line or a multi-line excerpt."""
    return _HASH_COMMENT_RE.sub("", text)


def _cast_argument_span(text: str, open_paren: int) -> tuple[int, int] | None:
    """Return the ``(start, end)`` span of a call's argument, by paren balance.

    Returns ``None`` when the parentheses do not balance, which happens on
    truncated snippets.  An unbalanced snippet is treated as "cannot analyse"
    rather than as a non-match, so a malformed input never produces a confident
    negative.
    """
    depth = 0
    for index in range(open_paren, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return open_paren + 1, index
    return None


def detect_truncation_before_scaling(text: str) -> bool:
    """Return True when ``text`` quantises a division and then scales it.

    ``int(a / b) * c`` and ``a * int(a / b)`` both report True — the remainder
    of ``a / b`` is discarded before ``c`` multiplies it.  ``int(a * b / c)``
    reports False: nothing is discarded, because the scaling happens before the
    cast.

    ``round`` is included because ``round(x) * c`` uses banker's rounding and is
    the same class of defect, but a cast applied to a variable rather than to a
    literal division (``int(ratio) * c``) reports False: nothing here shows that
    ``ratio`` was computed by a division.
    """
    line = strip_comment(text)
    for match in _CAST_CALL_RE.finditer(line):
        open_paren = line.index("(", match.end() - 1)
        span = _cast_argument_span(line, open_paren)
        if span is None:
            continue
        inside = line[span[0] : span[1]]
        if not _DIV_RE.search(inside):
            continue
        outside = line[: match.start()] + line[span[1] + 1 :]
        if _MUL_RE.search(outside):
            return True
    return False


# ── Evidence indexes ──────────────────────────────────────────────────────────


@dataclass
class EvidenceIndex:
    """Everything the rules need, extracted once from the findings.

    This is the in-memory form of :class:`~app.models.root_cause.EvidenceSynthesis`.
    ``to_synthesis``/``from_synthesis`` convert between the two so the
    ``EVIDENCE_PENDING`` stage can persist its work for ``ROOT_CAUSE_ANALYSIS``.
    """

    findings: list[Finding] = field(default_factory=list)
    routes: list[RouteFactModel] = field(default_factory=list)
    calls: list[CallEdgeModel] = field(default_factory=list)
    numeric_sites: list[NumericSiteModel] = field(default_factory=list)
    data_flows: list[DataFlowModel] = field(default_factory=list)
    #: Route-handling files that no test file imports.
    untested_route_files: tuple[str, ...] = ()
    #: One entry per reported concept, holding the pair most relevant to the
    #: issue: a coverage analysis produces a cross product of modules, and
    #: enumerating all of its permutations would bury the one pair that matters
    #: in noise.
    coverage_gaps: tuple[CoverageGap, ...] = ()
    #: Files Phase 7 reported as imported by at least one test file.
    covered_files: frozenset[str] = frozenset()
    rejected_types: tuple[str, ...] = ()
    rejected_count: int = 0
    raw_evidence_count: int = 0

    def by_type(self, evidence_type: EvidenceType) -> list[Finding]:
        return [
            f
            for f in self.findings
            if f.representative.evidence_type is evidence_type
        ]

    def counts(self) -> dict[str, int]:
        """Return per-type counts of distinct findings, sorted by type name."""
        counts: dict[str, int] = {}
        for finding in self.findings:
            name = finding.representative.evidence_type.value
            counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items()))

    # -- serialisation ────────────────────────────────────────────────────

    def to_synthesis(self) -> EvidenceSynthesis:
        """Return the persistable form of this index."""
        return EvidenceSynthesis(
            raw_evidence_count=self.raw_evidence_count,
            finding_count=len(self.findings),
            evidence_counts=self.counts(),
            findings=[
                SynthesisFinding(
                    evidence_id=finding.evidence_id,
                    agent=finding.representative.agent,
                    evidence_type=finding.representative.evidence_type,
                    title=finding.representative.title,
                    source_file=finding.representative.source_file,
                    source_line=finding.representative.source_line,
                    method=finding.representative.metadata.get("method"),
                    corroborating_agents=list(finding.agents),
                )
                for finding in self.findings
            ],
            routes=[r.model_copy(deep=True) for r in self.routes],
            call_edges=[c.model_copy(deep=True) for c in self.calls],
            numeric_sites=[s.model_copy(deep=True) for s in self.numeric_sites],
            data_flows=[d.model_copy(deep=True) for d in self.data_flows],
            coverage_gaps=[g.model_copy(deep=True) for g in self.coverage_gaps],
            untested_route_files=list(self.untested_route_files),
            covered_files=sorted(self.covered_files),
            conflicts=[],
            rejected_types=list(self.rejected_types),
            rejected_count=self.rejected_count,
        )

    @classmethod
    def from_synthesis(
        cls, synthesis: EvidenceSynthesis, evidence_by_id: dict[str, Evidence]
    ) -> "EvidenceIndex":
        """Rebuild an index from its persisted form.

        ``evidence_by_id`` supplies the full items for the retained findings.
        Findings whose evidence has since disappeared are dropped rather than
        reconstructed from the summary, so a stale synthesis degrades to fewer
        inputs instead of to fabricated ones.
        """
        findings: list[Finding] = []
        for item in synthesis.findings:
            full = evidence_by_id.get(item.evidence_id)
            if full is None:
                continue
            findings.append(
                Finding(
                    representative=full,
                    evidence_ids=(item.evidence_id,),
                    agents=tuple(item.corroborating_agents),
                )
            )
        return cls(
            findings=findings,
            routes=list(synthesis.routes),
            calls=list(synthesis.call_edges),
            numeric_sites=list(synthesis.numeric_sites),
            data_flows=list(synthesis.data_flows),
            untested_route_files=tuple(synthesis.untested_route_files),
            coverage_gaps=tuple(synthesis.coverage_gaps),
            covered_files=frozenset(synthesis.covered_files),
            rejected_types=tuple(synthesis.rejected_types),
            rejected_count=synthesis.rejected_count,
            raw_evidence_count=synthesis.raw_evidence_count,
        )


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return (str(value),)


def _line_of(item: Evidence, key: str = "line_start") -> int | None:
    value = item.metadata.get(key)
    return value if isinstance(value, int) else None


def build_index(
    items: Sequence[Evidence],
    terms: set[str] | None = None,
    config: SynthesisConfig = DEFAULT_SYNTHESIS_CONFIG,
) -> EvidenceIndex:
    """Extract the structural facts the rules reason over.

    Only ``OBSERVATION`` items are mined for structure.  Hypotheses are inputs to
    confidence and to coverage factors, but a hypothesis must never be used to
    assert a *location* — that would let an inference masquerade as a fact.

    ``terms`` are the reported issue's content terms, used only to pick the most
    relevant of several coverage pairs.  Omitting them yields an arbitrary-but
    deterministic choice rather than an error.
    """
    index = EvidenceIndex(findings=deduplicate(items))
    terms = terms or set()

    for finding in index.findings:
        item = finding.representative
        meta = item.metadata
        method = meta.get("method")

        if item.evidence_type is EvidenceType.OBSERVATION:
            if method == "ast_route_detection" and meta.get("route_path"):
                index.routes.append(
                    RouteFactModel(
                        http_method=str(meta.get("http_method", "")),
                        path=str(meta["route_path"]),
                        handler=str(meta.get("handler", "")),
                        file_path=item.source_file or "",
                        line=item.source_line,
                        evidence_ids=[item.id],
                    )
                )
            elif method == "ast_call_graph" and meta.get("callee"):
                index.calls.append(
                    CallEdgeModel(
                        caller_file=item.source_file or "",
                        caller_line=item.source_line,
                        callee=str(meta["callee"]),
                        symbol=str(
                            meta.get("target_symbol")
                            or str(meta["callee"]).rsplit(".", 1)[-1]
                        ),
                        target_file=str(meta.get("target_file", "")),
                        target_line=meta.get("target_definition_line"),
                        evidence_id=item.id,
                    )
                )
            elif method == "ast_import_graph" and meta.get("files_under_test"):
                index.covered_files |= frozenset(_as_tuple(meta["files_under_test"]))
            elif method == "import_graph" and meta.get("route_files"):
                index.untested_route_files = _as_tuple(meta["route_files"])

        if method == "text_scan":
            for site in _as_dicts(meta.get("sites")):
                if "line" not in site:
                    continue
                index.numeric_sites.append(
                    NumericSiteModel(
                        file_path=item.source_file or "",
                        function=str(meta.get("function", "")),
                        line=int(site["line"]),
                        kind=str(site.get("kind", "")),
                        detail=str(site.get("detail", "")),
                        text=str(site.get("text", "")),
                        evidence_id=item.id,
                    )
                )
            attribute = meta.get("attribute")
            if isinstance(attribute, str):
                index.data_flows.append(
                    DataFlowModel(
                        attribute=attribute,
                        written_in=list(_as_tuple(meta.get("written_in"))),
                        read_in=list(_as_tuple(meta.get("read_in"))),
                        evidence_id=item.id,
                    )
                )

        if method == "coverage_analysis":
            firsts = _as_tuple(meta.get("first_modules"))
            seconds = _as_tuple(meta.get("second_modules"))
            pairs: list[CoverageGap] = []
            for concept in _as_tuple(meta.get("concepts")) or ("",):
                for first in firsts:
                    for second in seconds:
                        if first != second:
                            pairs.append(
                                CoverageGap(
                                    concept=concept,
                                    first_module=str(first),
                                    second_module=str(second),
                                )
                            )
            index.coverage_gaps = tuple(
                index.coverage_gaps
            ) + tuple(_reduce_coverage_gaps(pairs, terms))

    index.routes.sort(key=lambda r: (r.path, r.http_method, r.file_path))
    index.calls.sort(
        key=lambda c: (c.caller_file, c.caller_line or 0, c.symbol, c.target_file)
    )
    index.numeric_sites.sort(key=lambda s: (s.file_path, s.line, s.kind, s.evidence_id))
    index.data_flows.sort(key=lambda d: (d.attribute, d.evidence_id))
    index.coverage_gaps = tuple(
        _reduce_coverage_gaps(list(index.coverage_gaps), terms)
    )
    return index


def _reduce_coverage_gaps(
    gaps: Sequence[CoverageGap], terms: set[str]
) -> list[CoverageGap]:
    """Keep one pair per reported concept, and one entry per distinct pair.

    A coverage analysis reports every ordered pair of issue-relevant modules, so
    for a four-module repository it yields six pairs of which one is usually
    informative, and several concepts can select the *same* winning pair.
    Ranking by issue-term overlap keeps the winner per concept; collapsing
    identical pairs afterwards keeps the factor list readable instead of
    repeating the same gap under three different names.
    """
    best: dict[str, tuple[int, str, str]] = {}
    for gap in gaps:
        overlap = len(_path_terms(gap.first_module) & terms) + len(
            _path_terms(gap.second_module) & terms
        )
        # Ties break on the pair itself, never on discovery order.
        key = (-overlap, gap.first_module, gap.second_module)
        current = best.get(gap.concept)
        if current is None or key < current:
            best[gap.concept] = key

    by_pair: dict[tuple[str, str], str] = {}
    for concept, (_, first, second) in sorted(best.items()):
        by_pair.setdefault((first, second), concept)
    return [
        CoverageGap(concept=concept, first_module=first, second_module=second)
        for (first, second), concept in sorted(
            by_pair.items(), key=lambda item: (item[0], item[1])
        )
    ]


def _as_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, dict)]
    return []


# ── Entry-point selection ─────────────────────────────────────────────────────


def issue_terms(issue: str) -> set[str]:
    """Return the comparable content terms of the reported issue.

    Reuses Phase 7's extraction so that Phase 8 relevance and Phase 7 relevance
    cannot drift apart.  Every term of at least three characters is kept.
    """
    return {term for term in build_issue_terms(issue).terms if len(term) >= 3}


def _path_terms(path: str) -> set[str]:
    return split_identifier(path)


def select_entry_route(
    index: EvidenceIndex, terms: set[str]
) -> RouteFactModel | None:
    """Return the route most likely to be the reported entry point.

    Scored by how many issue terms appear in the route path, its handler name and
    its file path, minus an equal penalty for every unrelated term it also
    matches (a route called ``/health`` in ``healthcheck.py`` matching
    ``checkout`` only by accident is not the entry point).  Ties break on
    ``(path, http_method, file_path)`` so the choice never depends on discovery
    order.
    """
    if not index.routes:
        return None

    def score(route: RouteFactModel) -> tuple[int, str, str, str]:
        hits = 0
        haystack = (
            f"{route.path} {route.handler} {route.file_path} {route.path}"
        ).lower()
        for term in terms:
            if term in haystack:
                hits += 1
        return (-hits, route.path, route.http_method, route.file_path)

    return min(index.routes, key=score)


# ── Candidate detection ───────────────────────────────────────────────────────

#: Ranking weight per candidate kind.  A defect that can be pointed at in the
#: source outranks a coverage observation, which is evidence *about the test
#: suite* rather than about the defect.
_KIND_PRIORITY: dict[RootCauseKind, float] = {
    RootCauseKind.TRUNCATION_BEFORE_SCALING: 0.60,
    RootCauseKind.UNEXERCISED_PATH: 0.30,
    RootCauseKind.UNCOVERED_INTERACTION: 0.25,
    RootCauseKind.UNRESOLVED: 0.0,
}

#: Numeric-site kinds that can carry an arithmetic defect.
_ARITHMETIC_KINDS = frozenset({"numeric_cast", "numeric_op"})


def _supporting_call_edges(
    index: EvidenceIndex, site: NumericSiteModel
) -> list[CallEdgeModel]:
    """Return the call edges that reach ``site``'s function.

    Matching is on the bare symbol plus the defining file, so a same-named
    function in a different module is not mistaken for this one.
    """
    if not site.function:
        return []
    return [
        edge
        for edge in index.calls
        if edge.symbol == site.function
        and (not site.file_path or edge.target_file == site.file_path)
    ]


def detect_candidates(
    index: EvidenceIndex, terms: set[str]
) -> list[RootCauseCandidate]:
    """Apply every recognition rule and return the ranked candidates.

    Ordering is total: ``(-support_score, file_path, line, kind)``.  Two runs with
    the same evidence therefore always rank candidates identically.
    """
    candidates: list[RootCauseCandidate] = []

    for site in index.numeric_sites:
        if site.kind not in _ARITHMETIC_KINDS:
            continue
        if not detect_truncation_before_scaling(site.text):
            continue
        callers = _supporting_call_edges(index, site)
        evidence_ids = [site.evidence_id]
        evidence_ids.extend(sorted({edge.evidence_id for edge in callers}))
        candidate = RootCauseCandidate(
            kind=RootCauseKind.TRUNCATION_BEFORE_SCALING,
            summary=(
                f"{site.function}() in {site.file_path}:{site.line} truncates a "
                f"division before scaling it, so the computed value is "
                f"systematically wrong for inputs that are not exact multiples."
            ),
            rationale=(
                f"Line {site.line} reads `{site.text.strip()}`. The division "
                f"happens inside the {site.kind.replace('_', ' ')} and the "
                f"multiplication happens outside it, so the fractional remainder "
                f"is discarded before it is scaled ({site.detail})."
            ),
            anchor=SourceAnchor(
                file_path=site.file_path,
                line=site.line,
                function=site.function or None,
                excerpt=site.text.strip() or None,
            ),
            evidence_ids=_dedup_preserve_order(evidence_ids),
            support_score=_KIND_PRIORITY[RootCauseKind.TRUNCATION_BEFORE_SCALING],
        )
        candidates.append(candidate)

    if index.untested_route_files:
        anchor_file = sorted(index.untested_route_files)[0]
        observation = _first_finding_with(index, method="import_graph", key="route_files")
        evidence_ids = [observation.representative.id] if observation else []
        candidates.append(
            RootCauseCandidate(
                kind=RootCauseKind.UNEXERCISED_PATH,
                summary=(
                    f"{len(index.untested_route_files)} route-handling file(s) are "
                    f"never imported by a test file, so the reported path is not "
                    f"exercised at the request boundary."
                ),
                rationale=(
                    "Phase 7 resolved no import edge from any test file to any "
                    "file that declares an HTTP route. A defect on this path "
                    "cannot be caught by the existing suite."
                ),
                anchor=SourceAnchor(file_path=anchor_file),
                evidence_ids=evidence_ids,
                support_score=_KIND_PRIORITY[RootCauseKind.UNEXERCISED_PATH],
            )
        )

    for gap in index.coverage_gaps:
        finding = _first_concept_finding(
            index, gap.concept, gap.first_module, gap.second_module
        )
        if finding is None:
            continue
        candidates.append(
            RootCauseCandidate(
                kind=RootCauseKind.UNCOVERED_INTERACTION,
                summary=(
                    f"No test exercises '{gap.first_module}' and "
                    f"'{gap.second_module}' together, so an interaction between "
                    f"them is not covered."
                ),
                rationale=(
                    "Each module is referenced by tests, but never by the same "
                    "test. Phase 7 found no single test file importing both."
                ),
                anchor=SourceAnchor(file_path=gap.first_module),
                evidence_ids=[finding.representative.id],
                support_score=_KIND_PRIORITY[RootCauseKind.UNCOVERED_INTERACTION],
            )
        )

    return _rank_candidates(candidates)


def _rank_candidates(
    candidates: Sequence[RootCauseCandidate],
) -> list[RootCauseCandidate]:
    unique: dict[tuple[str, str, int, str], RootCauseCandidate] = {}
    for candidate in candidates:
        key = (
            candidate.anchor.file_path,
            candidate.anchor.function or "",
            candidate.anchor.line or 0,
            candidate.kind.value,
        )
        unique.setdefault(key, candidate)
    return sorted(
        unique.values(),
        key=lambda c: (
            -c.support_score,
            c.anchor.file_path,
            c.anchor.line or 0,
            c.kind.value,
        ),
    )


def _first_finding_with(
    index: EvidenceIndex, method: str, key: str
) -> Finding | None:
    for finding in index.findings:
        if finding.representative.metadata.get("method") == method and (
            finding.representative.metadata.get(key)
        ):
            return finding
    return None


def _first_concept_finding(
    index: EvidenceIndex, concept: str, first: str, second: str
) -> Finding | None:
    for finding in index.findings:
        meta = finding.representative.metadata
        if meta.get("method") != "coverage_analysis":
            continue
        concepts = _as_tuple(meta.get("concepts"))
        firsts = {str(m) for m in _as_tuple(meta.get("first_modules"))}
        seconds = {str(m) for m in _as_tuple(meta.get("second_modules"))}
        if concept in concepts and first in firsts and second in seconds:
            return finding
    return None


# ── Conflicts ─────────────────────────────────────────────────────────────────


def detect_conflicts(index: EvidenceIndex) -> list[ConflictingEvidence]:
    """Find evidence that cannot all be true, and surface it rather than guess.

    Two detectors run:

    1. *Split resolution* — the same callee resolved to two different target
       files.  At least one static resolution is wrong, so no location derived
       from that symbol can be trusted.
    2. *Coverage contradiction* — a file reported as never imported by any test
       while another observation reports it as exercised.  The "missing
       coverage" claim is then unreliable, which matters because Phase 8 lowers
       confidence on coverage grounds.
    """
    conflicts: list[ConflictingEvidence] = []

    by_symbol: dict[str, dict[str, set[str]]] = {}
    for edge in index.calls:
        by_symbol.setdefault(edge.symbol, {}).setdefault(edge.target_file, set()).add(
            edge.evidence_id
        )

    for symbol in sorted(by_symbol):
        targets = by_symbol[symbol]
        if len(targets) < 2:
            continue
        ids = sorted({eid for ids in targets.values() for eid in ids})
        files = ", ".join(sorted(targets))
        conflicts.append(
            ConflictingEvidence(
                description=(
                    f"'{symbol}' was statically resolved to more than one "
                    f"definition ({files}). Locations derived from this symbol "
                    f"are ambiguous."
                ),
                evidence_ids=ids,
            )
        )

    untested_claims: dict[str, list[str]] = {}
    for finding in index.findings:
        meta = finding.representative.metadata
        if meta.get("method") != "coverage_analysis":
            continue
        for path in _as_tuple(meta.get("untested_files")):
            untested_claims.setdefault(str(path), []).append(finding.representative.id)

    for path in sorted(set(untested_claims) & set(index.covered_files)):
        conflicts.append(
            ConflictingEvidence(
                description=(
                    f"'{path}' is reported as not imported by any test file, but "
                    f"is also reported as exercised by the test suite. The "
                    f"coverage claim for this file is unreliable."
                ),
                evidence_ids=sorted(set(untested_claims[path])),
            )
        )

    return conflicts


# ── Confidence ────────────────────────────────────────────────────────────────

#: Base score per candidate kind before corroboration and penalties.
_BASE_SCORE: dict[RootCauseKind, float] = {
    RootCauseKind.TRUNCATION_BEFORE_SCALING: 0.60,
    RootCauseKind.UNEXERCISED_PATH: 0.35,
    RootCauseKind.UNCOVERED_INTERACTION: 0.30,
    RootCauseKind.UNRESOLVED: 0.0,
}

#: Bonus per additional independent investigator, capped at two bonuses.
_CORROBORATION_BONUS = 0.08
_MAX_CORROBORATION_BONUS = 0.16
#: Penalty per conflicting-evidence group.
_CONFLICT_PENALTY = 0.15
#: Penalty when no test exercises the reported path at all.
_UNTESTED_PATH_PENALTY = 0.12
#: Penalty per failed investigator.
_PARTIAL_PENALTY = 0.20
#: Penalty when the conclusion rests on hypotheses rather than a direct read of
#: the defect site.  This is what keeps a guess out of the HIGH band.
_INFERENCE_PENALTY = 0.10


def _score_confidence(
    candidate: RootCauseCandidate | None,
    *,
    corroborating_investigators: int,
    direct_observations: int,
    inferred_hypotheses: int,
    conflict_count: int,
    untested_path: bool,
    failed_investigators: int,
) -> ConfidenceBreakdown:
    """Return an auditable confidence breakdown.

    score = base(kind)
          + min(corroborating_investigators - 1, 2) * 0.08
          - penalties
          clamped to ``[0.0, 1.0]``

    and then capped so that an incomplete investigation can never reach ``HIGH``.
    """
    base = _BASE_SCORE.get(candidate.kind, 0.0) if candidate else 0.0
    bonus = min(
        max(corroborating_investigators - 1, 0) * _CORROBORATION_BONUS,
        _MAX_CORROBORATION_BONUS,
    )

    penalties: dict[str, float] = {}
    if conflict_count:
        penalties["conflicting_evidence"] = round(
            _CONFLICT_PENALTY * conflict_count, 4
        )
    if untested_path:
        penalties["reported_path_untested"] = _UNTESTED_PATH_PENALTY
    if failed_investigators:
        penalties["partial_investigation"] = round(
            _PARTIAL_PENALTY * failed_investigators, 4
        )
    if candidate is not None and not direct_observations:
        penalties["no_direct_observation"] = _INFERENCE_PENALTY
    if candidate is not None and inferred_hypotheses and not direct_observations:
        penalties["inference_only"] = _INFERENCE_PENALTY

    score = base + bonus - sum(penalties.values())
    score = round(min(max(score, 0.0), 1.0), 4)

    if failed_investigators and score >= HIGH_CONFIDENCE_THRESHOLD:
        score = round(HIGH_CONFIDENCE_THRESHOLD - 0.01, 4)
    if conflict_count and score >= HIGH_CONFIDENCE_THRESHOLD:
        score = round(HIGH_CONFIDENCE_THRESHOLD - 0.01, 4)

    level = ConfidenceLevel.from_score(score)
    return ConfidenceBreakdown(
        base=round(base, 4),
        corroboration_bonus=round(bonus, 4),
        penalties=penalties,
        score=score,
        level=level,
        independent_investigators=corroborating_investigators,
        direct_observations=direct_observations,
        inferred_hypotheses=inferred_hypotheses,
        rationale=_confidence_rationale(
            candidate, level, corroborating_investigators, penalties
        ),
    )


def _confidence_rationale(
    candidate: RootCauseCandidate | None,
    level: ConfidenceLevel,
    corroborating_investigators: int,
    penalties: dict[str, float],
) -> str:
    if candidate is None:
        return (
            "No recognised defect pattern was present in the evidence, so no "
            "root cause is claimed and confidence reflects the absence of a "
            "finding rather than certainty about the code."
        )
    parts = [
        f"Base {round(_BASE_SCORE.get(candidate.kind, 0.0), 2)} for a "
        f"{candidate.kind.value.replace('_', ' ').lower()} finding, read directly "
        f"from the source at {candidate.anchor.file_path}"
        + (f":{candidate.anchor.line}" if candidate.anchor.line else "")
        + "."
    ]
    if corroborating_investigators > 1:
        parts.append(
            f"Corroborated by {corroborating_investigators} independent "
            f"investigators."
        )
    else:
        parts.append("Reported by a single investigator.")
    if penalties:
        parts.append(
            "Reduced by "
            + ", ".join(
                f"{name.replace('_', ' ')} (-{value})"
                for name, value in sorted(penalties.items())
            )
            + "."
        )
    parts.append(
        f"Overall {level.value}. Nothing here was executed, so this is a "
        f"root cause proposed from static evidence, not a reproduced defect."
    )
    return " ".join(parts)


# ── Evidence chain ────────────────────────────────────────────────────────────


def build_evidence_chain(
    index: EvidenceIndex,
    candidate: RootCauseCandidate | None,
    terms: set[str],
    config: SynthesisConfig = DEFAULT_SYNTHESIS_CONFIG,
) -> list[EvidenceChainLink]:
    """Build the path from the reported entry point to the defect site.

    Every link cites the evidence it was derived from, so the chain is auditable
    end to end.  A link is only ever added when the evidence supports it; gaps
    in the chain are left as gaps.
    """
    if candidate is None:
        return []

    links: list[EvidenceChainLink] = []
    anchor = candidate.anchor
    site = next(
        (
            s
            for s in index.numeric_sites
            if s.file_path == anchor.file_path and s.line == anchor.line
        ),
        None,
    )

    callers = _supporting_call_edges(index, site) if site else []
    caller_file = callers[0].caller_file if callers else anchor.file_path
    route = _route_for_file(index, caller_file, terms) or select_entry_route(
        index, terms
    )

    if route is not None:
        links.append(
            EvidenceChainLink(
                kind=ChainNodeKind.ROUTE,
                label=f"{route.http_method} {route.path}",
                anchor=SourceAnchor(
                    file_path=route.file_path,
                    line=route.line,
                    function=route.handler or None,
                ),
                evidence_ids=list(route.evidence_ids),
                detail=(
                    f"Served by {route.handler}() in {route.file_path}"
                    + (f":{route.line}" if route.line else "")
                    + "."
                ),
            )
        )

    for edge in callers:
        links.append(
            EvidenceChainLink(
                kind=ChainNodeKind.CALL,
                label=f"{edge.caller_file} calls {edge.callee}()",
                anchor=SourceAnchor(
                    file_path=edge.caller_file, line=edge.caller_line
                ),
                evidence_ids=[edge.evidence_id],
                detail=(
                    f"Resolved statically to {edge.target_file}"
                    + (f":{edge.target_line}" if edge.target_line else "")
                    + "."
                ),
            )
        )

    links.append(
        EvidenceChainLink(
            kind=ChainNodeKind.SITE,
            label=f"{anchor.file_path}:{anchor.line} {anchor.function}()",
            anchor=anchor,
            evidence_ids=[candidate.evidence_ids[0]] if candidate.evidence_ids else [],
            detail=candidate.rationale,
        )
    )

    links.extend(_downstream_links(index, anchor, terms, callers))

    if index.untested_route_files:
        observation = _first_finding_with(index, method="import_graph", key="route_files")
        links.append(
            EvidenceChainLink(
                kind=ChainNodeKind.COVERAGE,
                label="Reported path is not exercised by any test",
                anchor=SourceAnchor(file_path=sorted(index.untested_route_files)[0]),
                evidence_ids=[observation.representative.id] if observation else [],
                detail=(
                    f"{len(index.untested_route_files)} route-handling file(s) have "
                    f"no import edge from any test file, so no test can fail "
                    f"because of this defect."
                ),
            )
        )

    return _dedup_links(links)[: config.max_chain_links]


def _route_for_file(
    index: EvidenceIndex, file_path: str, terms: set[str]
) -> RouteFactModel | None:
    matches = [route for route in index.routes if route.file_path == file_path]
    if not matches:
        return None
    return min(matches, key=lambda r: (-_hit_count(r, terms), r.path, r.http_method))


def _hit_count(route: RouteFactModel, terms: set[str]) -> int:
    haystack = f"{route.path} {route.handler} {route.file_path}".lower()
    return sum(1 for term in terms if term in haystack)


def _downstream_links(
    index: EvidenceIndex,
    anchor: SourceAnchor,
    terms: set[str],
    callers: Sequence[CallEdgeModel],
) -> list[EvidenceChainLink]:
    """Show where the affected value is consumed.

    A value that is truncated at one site only matters if something reads it.  A
    data-flow observation is used when it connects the anchor's neighbourhood to
    a file the report is actually about, which keeps the chain relevant without
    inventing a flow Phase 7 did not observe.
    """
    neighbourhood = {anchor.file_path} | {edge.caller_file for edge in callers}
    relevant_files = {
        path
        for path in list(index.covered_files) + list(index.untested_route_files)
        if _path_terms(path) & terms
    }

    links: list[EvidenceChainLink] = []
    for flow in index.data_flows:
        touched = set(flow.written_in) | set(flow.read_in)
        if not (touched & (neighbourhood | relevant_files)):
            continue
        links.append(
            EvidenceChainLink(
                kind=ChainNodeKind.DATA_FLOW,
                label=f"'{flow.attribute}' crosses a module boundary",
                anchor=SourceAnchor(file_path=sorted(touched)[0] if touched else ""),
                evidence_ids=[flow.evidence_id],
                detail=(
                    f"Written in {', '.join(sorted(flow.written_in)) or 'unknown'}; "
                    f"read in {', '.join(sorted(flow.read_in)) or 'unknown'}."
                ),
            )
        )
    return links


def _dedup_links(links: Iterable[EvidenceChainLink]) -> list[EvidenceChainLink]:
    seen: set[tuple[str, str]] = set()
    result: list[EvidenceChainLink] = []
    for link in links:
        key = (link.kind.value, link.label)
        if key in seen:
            continue
        seen.add(key)
        result.append(link)
    return result


# ── Factors, questions, next steps ───────────────────────────────────────────


def _build_factors(
    index: EvidenceIndex,
    candidate: RootCauseCandidate | None,
    conflicts: Sequence[ConflictingEvidence],
    failed_investigators: Sequence[str],
    config: SynthesisConfig,
) -> list[ContributingFactor]:
    factors: list[ContributingFactor] = []

    if index.untested_route_files:
        observation = _first_finding_with(index, method="import_graph", key="route_files")
        factors.append(
            ContributingFactor(
                category=FactorCategory.UNEXERCISED_ENTRY_POINT,
                description=(
                    f"{len(index.untested_route_files)} file(s) that declare HTTP "
                    f"routes are not imported by any test file, so the reported "
                    f"behaviour is never asserted at the request boundary."
                ),
                evidence_ids=[observation.representative.id] if observation else [],
            )
        )

    for gap in index.coverage_gaps:
        finding = _first_concept_finding(
            index, gap.concept, gap.first_module, gap.second_module
        )
        factors.append(
            ContributingFactor(
                category=FactorCategory.MISSING_COVERAGE,
                description=(
                    f"No test exercises '{gap.first_module}' and "
                    f"'{gap.second_module}' together, so an interaction defect "
                    f"between them survives a green suite."
                ),
                evidence_ids=[finding.representative.id] if finding else [],
            )
        )

    for conflict in conflicts:
        factors.append(
            ContributingFactor(
                category=FactorCategory.UNRESOLVED_CONFLICT,
                description=conflict.description,
                evidence_ids=list(conflict.evidence_ids),
            )
        )

    for name in failed_investigators:
        factors.append(
            ContributingFactor(
                category=FactorCategory.PARTIAL_INVESTIGATION,
                description=(
                    f"Investigator '{name}' failed, so part of the system was "
                    f"never examined and the conclusion cannot be complete."
                ),
                evidence_ids=[],
            )
        )

    return factors[: config.max_factors]


def _build_questions(
    index: EvidenceIndex,
    candidate: RootCauseCandidate | None,
    conflicts: Sequence[ConflictingEvidence],
    config: SynthesisConfig,
) -> list[UnresolvedQuestion]:
    questions: list[UnresolvedQuestion] = []

    if candidate is not None:
        questions.append(
            UnresolvedQuestion(
                question=(
                    f"Does {candidate.anchor.file_path}:"
                    f"{candidate.anchor.line or '?'} actually produce a wrong "
                    f"value for a real input?"
                ),
                why_it_matters=(
                    "The conclusion is read from the source text. No test was "
                    "executed, so the failure is predicted, not observed."
                ),
                recommended_step=(
                    "Execute the affected path with concrete inputs and record the "
                    "computed value at each call site. Only a recorded exit code "
                    "can turn this into a verified result."
                ),
                evidence_ids=list(candidate.evidence_ids),
            )
        )

    if index.untested_route_files:
        questions.append(
            UnresolvedQuestion(
                question=(
                    "Why does no test file import any file that declares a route?"
                ),
                why_it_matters=(
                    "A defect on the reported path is invisible to the existing "
                    "suite, so no regression could have caught it."
                ),
                recommended_step=(
                    "Add a test that exercises the reported request path through "
                    "its route handler."
                ),
                evidence_ids=(
                    [
                        _first_finding_with(
                            index, method="import_graph", key="route_files"
                        ).representative.id
                    ]
                    if _first_finding_with(index, method="import_graph", key="route_files")
                    else []
                ),
            )
        )

    for conflict in conflicts:
        questions.append(
            UnresolvedQuestion(
                question=(
                    "Which of the conflicting readings of the evidence is correct?"
                ),
                why_it_matters=conflict.description,
                recommended_step=(
                    "Inspect the definitions directly and record which file the "
                    "symbol actually resolves to at runtime."
                ),
                evidence_ids=list(conflict.evidence_ids),
            )
        )

    if not index.numeric_sites and index.findings:
        questions.append(
            UnresolvedQuestion(
                question=(
                    "No arithmetic site was recorded on the reported path — is the "
                    "defect non-arithmetic?"
                ),
                why_it_matters=(
                    "The only patterns recognised in this phase are arithmetic, so "
                    "a different class of defect would be missed entirely."
                ),
                recommended_step=(
                    "Re-run the investigation and review the recorded call chain "
                    "for the reported path."
                ),
                evidence_ids=[],
            )
        )

    return questions[: config.max_questions]


def _next_steps(
    questions: Sequence[UnresolvedQuestion], candidate: RootCauseCandidate | None
) -> list[str]:
    steps: list[str] = []
    for question in questions:
        if question.recommended_step not in steps:
            steps.append(question.recommended_step)
    if candidate is not None and not steps:
        steps.append(
            "Execute the affected path with concrete inputs to confirm the "
            "predicted value."
        )
    return steps


# ── The synthesizer ───────────────────────────────────────────────────────────


class EvidenceSynthesizer:
    """Turns a Phase 7 investigation into a Phase 8 root-cause analysis.

    The class holds no mutable state between calls, so a single instance is safe
    to share and cannot leak results from one run into another.
    """

    def __init__(self, config: SynthesisConfig | None = None) -> None:
        self._config = config or DEFAULT_SYNTHESIS_CONFIG

    # -- public API --------------------------------------------------------

    def synthesize_evidence(
        self,
        investigation: InvestigationResult,
        *,
        issue: str | None = None,
    ) -> tuple[EvidenceSynthesis, "EvidenceIndex"]:
        """Run the ``EVIDENCE_PENDING`` step and return the persistable index.

        This is the first half of Phase 8: reject unusable evidence types,
        deduplicate, index the structural facts, and detect conflicts.  It makes
        no judgement about what is wrong with the repository.
        """
        if investigation is None:
            raise EvidenceSynthesisError("investigation is required")

        raw = list(investigation.evidence)
        rejected = [
            e
            for e in raw
            if e.evidence_type not in PHASE8_CONSUMED_EVIDENCE_TYPES
        ]
        if rejected:
            logger.warning(
                "Run %s: ignoring %d evidence item(s) of type(s) %s; Phase 8 "
                "reasons only from %s",
                investigation.run_id,
                len(rejected),
                sorted({e.evidence_type.value for e in rejected}),
                ", ".join(sorted(t.value for t in PHASE8_CONSUMED_EVIDENCE_TYPES)),
            )

        usable = [e for e in raw if e.evidence_type in PHASE8_CONSUMED_EVIDENCE_TYPES]
        index = build_index(usable, issue_terms(issue or ""), self._config)
        index.raw_evidence_count = len(raw)
        index.rejected_count = len(rejected)
        index.rejected_types = tuple(
            sorted({e.evidence_type.value for e in rejected})
        )

        synthesis = index.to_synthesis()
        synthesis.conflicts = detect_conflicts(index)
        return synthesis, index

    def analyze(
        self,
        investigation: InvestigationResult,
        index: "EvidenceIndex",
        *,
        issue: str | None = None,
    ) -> RootCauseAnalysis:
        """Run the ``ROOT_CAUSE_ANALYSIS`` step over a prepared index.

        This is the second half of Phase 8: rank candidate root causes, score
        confidence with an auditable breakdown, and build the evidence chain.
        """
        if investigation is None:
            raise EvidenceSynthesisError("investigation is required")

        run_id = investigation.run_id
        subject = issue if issue is not None else ""
        terms = issue_terms(subject)
        conflicts = detect_conflicts(index)
        failed = [
            report.investigator.value
            for report in investigation.investigators
            if report.status is InvestigatorStatus.FAILED
        ]
        succeeded = [
            report.investigator.value
            for report in investigation.investigators
            if report.status is InvestigatorStatus.COMPLETED
        ]

        if not index.findings:
            return self._empty_analysis(
                investigation, subject, failed, index.raw_evidence_count,
                index.rejected_count,
            )

        candidates = detect_candidates(index, terms)[: self._config.max_candidates]
        top = candidates[0] if candidates else None

        supporting = self._supporting_findings(index, top)
        corroborating = sorted(
            {
                agent
                for finding in supporting
                for agent in finding.agents
            }
        )
        direct = len(
            [
                f
                for f in supporting
                if f.representative.evidence_type is EvidenceType.OBSERVATION
            ]
        )
        inferred = len(
            [
                f
                for f in supporting
                if f.representative.evidence_type is EvidenceType.HYPOTHESIS
            ]
        )

        breakdown = _score_confidence(
            top,
            corroborating_investigators=len(corroborating),
            direct_observations=direct,
            inferred_hypotheses=inferred,
            conflict_count=len(conflicts),
            untested_path=bool(index.untested_route_files),
            failed_investigators=len(failed),
        )

        questions = _build_questions(index, top, conflicts, self._config)
        status = self._status(investigation, top, failed)

        analysis = RootCauseAnalysis(
            run_id=run_id,
            issue=subject,
            status=status,
            root_cause=top.summary if top else None,
            root_cause_kind=top.kind if top else None,
            anchor=top.anchor if top else None,
            confidence=breakdown.score,
            confidence_level=breakdown.level,
            confidence_rationale=breakdown.rationale,
            confidence_breakdown=breakdown,
            evidence_chain=build_evidence_chain(index, top, terms, self._config),
            supporting_evidence=[
                ref for finding in supporting for ref in finding.refs()
            ][: self._config.max_supporting_evidence],
            rejected_candidates=list(candidates[1:]),
            contributing_factors=_build_factors(
                index, top, conflicts, failed, self._config
            ),
            conflicting_evidence=list(conflicts),
            unresolved_questions=questions,
            investigation_status=investigation.status,
            partial_investigation=bool(failed),
            missing_evidence=sorted(failed),
            evidence_counts=index.counts(),
            raw_evidence_count=index.raw_evidence_count,
            deduplicated_evidence_count=len(index.findings),
            tests_executed=False,
            verified_result_count=index.rejected_count,
            corroborating_investigators=corroborating or sorted(succeeded),
            recommended_next_investigation=_next_steps(questions, top),
        )
        return analysis

    def synthesize(
        self,
        investigation: InvestigationResult,
        *,
        issue: str | None = None,
    ) -> RootCauseAnalysis:
        """Run both Phase 8 steps and return the final analysis.

        The orchestrator calls :meth:`synthesize_evidence` and :meth:`analyze`
        separately, one per workflow state, so that the intermediate index is
        inspectable.  This one-shot form composes them for tests, scripts and any
        caller that does not drive the state machine.
        """
        _, index = self.synthesize_evidence(investigation, issue=issue)
        return self.analyze(investigation, index, issue=issue)

    # -- helpers -----------------------------------------------------------

    def _supporting_findings(
        self, index: EvidenceIndex, candidate: RootCauseCandidate | None
    ) -> list[Finding]:
        """Return the findings that back ``candidate``, in citation order.

        Order is: the candidate's own evidence, then the call chain reaching it,
        then the entry route.  The list is a ranking, not a coincidence of
        iteration.
        """
        if candidate is None:
            return []
        by_id = {f.representative.id: f for f in index.findings}
        ordered: list[Finding] = []
        seen: set[str] = set()
        for evidence_id in candidate.evidence_ids:
            finding = by_id.get(evidence_id)
            if finding is not None and finding.representative.id not in seen:
                seen.add(finding.representative.id)
                ordered.append(finding)
        return ordered

    @staticmethod
    def _status(
        investigation: InvestigationResult,
        candidate: RootCauseCandidate | None,
        failed: Sequence[str],
    ) -> RootCauseStatus:
        if investigation.status is InvestigationStatus.FAILED:
            return RootCauseStatus.NO_EVIDENCE
        if failed:
            return RootCauseStatus.PARTIAL
        if candidate is None:
            return RootCauseStatus.INSUFFICIENT_EVIDENCE
        return RootCauseStatus.DETERMINED

    def _empty_analysis(
        self,
        investigation: InvestigationResult,
        subject: str,
        failed: Sequence[str],
        raw_count: int,
        rejected_count: int,
    ) -> RootCauseAnalysis:
        """The honest answer when there is nothing to reason from."""
        breakdown = _score_confidence(
            None,
            corroborating_investigators=0,
            direct_observations=0,
            inferred_hypotheses=0,
            conflict_count=0,
            untested_path=False,
            failed_investigators=len(failed),
        )
        if investigation.status is InvestigationStatus.FAILED:
            status = RootCauseStatus.FAILED
            questions = [
                UnresolvedQuestion(
                    question="Why did the investigation produce no usable evidence?",
                    why_it_matters=(
                        "Without evidence there is nothing to reason from, so no "
                        "root cause can be proposed for this run."
                    ),
                    recommended_step=(
                        "Re-run the investigation and inspect the recorded "
                        "investigator errors."
                    ),
                    evidence_ids=[],
                )
            ]
        else:
            status = RootCauseStatus.NO_EVIDENCE
            questions = [
                UnresolvedQuestion(
                    question="Why did the investigation record no evidence?",
                    why_it_matters=(
                        "Synthesis consumed nothing, so no conclusion is possible."
                    ),
                    recommended_step=(
                        "Re-run the investigation against the repository and "
                        "check that relevant files are being read."
                    ),
                    evidence_ids=[],
                )
            ]

        return RootCauseAnalysis(
            run_id=investigation.run_id,
            issue=subject,
            status=status,
            root_cause=None,
            root_cause_kind=None,
            anchor=None,
            confidence=0.0,
            confidence_level=ConfidenceLevel.LOW,
            confidence_rationale=(
                "No evidence was available to synthesize, so no root cause is "
                "claimed. Confidence in a conclusion is zero by construction "
                "rather than low."
            ),
            confidence_breakdown=breakdown,
            evidence_chain=[],
            supporting_evidence=[],
            rejected_candidates=[],
            contributing_factors=[
                ContributingFactor(
                    category=FactorCategory.PARTIAL_INVESTIGATION,
                    description=(
                        f"{len(failed)} investigator(s) failed, so no evidence was "
                        f"available to reason from."
                    ),
                    evidence_ids=[],
                )
            ]
            if failed
            else [],
            conflicting_evidence=[],
            unresolved_questions=questions,
            investigation_status=investigation.status,
            partial_investigation=bool(failed),
            missing_evidence=sorted(failed),
            evidence_counts={},
            raw_evidence_count=raw_count,
            deduplicated_evidence_count=0,
            tests_executed=False,
            verified_result_count=rejected_count,
            corroborating_investigators=[],
            recommended_next_investigation=[
                q.recommended_step for q in questions
            ],
        )


def _dedup_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


#: A ready-to-use instance for callers that do not need custom bounds.
default_synthesizer = EvidenceSynthesizer()


def synthesize_evidence(
    investigation: InvestigationResult,
    *,
    issue: str | None = None,
    config: SynthesisConfig | None = None,
) -> RootCauseAnalysis:
    """Convenience wrapper around :class:`EvidenceSynthesizer`."""
    return EvidenceSynthesizer(config).synthesize(investigation, issue=issue)


__all__ = [
    "DEFAULT_SYNTHESIS_CONFIG",
    "EvidenceSynthesizer",
    "EvidenceSynthesisError",
    "Finding",
    "SynthesisConfig",
    "build_evidence_chain",
    "build_index",
    "deduplicate",
    "default_synthesizer",
    "detect_candidates",
    "detect_conflicts",
    "detect_truncation_before_scaling",
    "issue_terms",
    "normalize_title",
    "select_entry_route",
    "strip_comment",
    "synthesize_evidence",
]
