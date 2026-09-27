"""
Generic issue → code relevance scoring for Phase 7.

This module answers one question: *given a bug report, which parts of the
repository are worth reading?*  It answers it with deterministic, explainable
text matching over the Phase 3/4/5 models — never with an LLM, and never with
knowledge of any particular repository.

Deliberately generic
--------------------
Nothing here mentions a domain, a framework, a filename, or a known bug.  The
only inputs are the developer's issue text and the repository's own names and
identifiers.  That is what makes the result meaningful for an unfamiliar
codebase instead of merely reproducing what the demo repository looks like.

Scoring model
-------------
For each content term in the issue, a file earns points for matching that term
in a *specific, nameable* place.  Names beat bodies: a file whose name contains
``discount`` is far more likely to be the subject of a "discount is applied"
report than a file that merely mentions the word once in a comment.

    filename contains term            +4
    HTTP route path contains term     +4
    declared function/class name       +3
    module / directory name            +2
    term appears in an argument name   +1

Terms are matched against both whole identifiers and their sub-words, so
``discount_service``, ``applyDiscount`` and ``discount`` all match ``discount``.

Concepts and clusters
---------------------
A *concept* is one content term of the issue.  The files matching one concept
form a :class:`ConceptCluster`.  Clusters are what let the investigators talk
about interactions ("the issue names two concepts; the code that handles each
one meets in module X") without knowing anything about the domain up front.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from pydantic import BaseModel, Field

from app.analysis.ast_analyzer import FileAnalysis, RepositoryASTAnalysis

# ── Tokenisation ───────────────────────────────────────────────────────────────

#: Generic English function words.  This is a language list, not domain
#: knowledge: it stops "when" or "the" from pulling in every file.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "about", "after", "all", "also", "am", "an", "and", "any", "are",
        "as", "at", "be", "been", "being", "but", "by", "can", "could", "did",
        "do", "does", "doing", "done", "during", "each", "err", "every", "fail",
        "fails", "failed", "for", "from", "get", "gets", "getting", "goes",
        "going", "got", "had", "has", "have", "having", "here", "how", "i",
        "if", "in", "into", "is", "issue", "it", "its", "just", "let", "like",
        "make", "makes", "me", "might", "more", "most", "much", "must", "my",
        "need", "needs", "not", "now", "of", "on", "one", "only", "or", "other",
        "our", "out", "over", "please", "problem", "report", "reported", "seem",
        "seems", "should", "shows", "since", "so", "some", "something", "still",
        "such", "sure", "test", "than", "that", "the", "their", "them", "then",
        "there", "these", "they", "this", "those", "through", "to", "too",
        "try", "trying", "under", "until", "up", "upon", "us", "use", "used",
        "uses", "using", "very", "want", "was", "we", "were", "what", "when",
        "where", "whether", "which", "while", "why", "will", "with", "within",
        "without", "would", "wrong", "you", "your",
    }
)

#: Terms shorter than this are too noisy to be useful signal.
MIN_TERM_LENGTH = 3

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
_PATH_LIKE_RE = re.compile(r"[A-Za-z0-9_./-]*[/\\][A-Za-z0-9_./\\-]+")
_QUOTED_RE = re.compile(r"`([^`]+)`|[\"']([^\"']{2,80})[\"']")


def split_identifier(identifier: str) -> set[str]:
    """Split an identifier into its lowercase sub-words.

    ``apply_discount`` → ``{"apply", "discount"}``
    ``DiscountService`` → ``{"discount", "service"}``
    ``HTTPRouter``     → ``{"http", "router"}``

    So a single issue term can match a whole family of names.
    """
    parts: set[str] = set()
    for chunk in identifier.replace("/", "_").replace(".", "_").split("_"):
        if not chunk:
            continue
        parts.update(piece.lower() for piece in _CAMEL_RE.findall(chunk) if piece)
    return {p for p in parts if len(p) >= MIN_TERM_LENGTH}


def _substrings(text: str) -> set[str]:
    """Every sub-word of a path or identifier, lowercased."""
    return split_identifier(text)


# ── Models ─────────────────────────────────────────────────────────────────────


class IssueTerms(BaseModel):
    """The issue text reduced to comparable terms.

    The raw text is kept only for quoting back to the user.  No investigator
    ever *acts* on it, and nothing in this package sends it to a model.
    """

    raw: str
    #: Content terms in first-seen order — deterministic, never sorted by score.
    terms: list[str] = Field(default_factory=list)
    #: Terms the reporter explicitly quoted, e.g. ``checkout_service.checkout``.
    quoted_identifiers: list[str] = Field(default_factory=list)
    #: Anything shaped like a path, e.g. ``app/routes/checkout.py``.
    path_hints: list[str] = Field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.terms or self.quoted_identifiers)


class ConceptCluster(BaseModel):
    """Files that match one issue term."""

    term: str
    file_paths: list[str] = Field(default_factory=list)
    #: The subset of ``file_paths`` matched by *name* — filename, declared symbol,
    #: or route path — rather than by incidental text.  These are the files the
    #: concept is genuinely *about*; see :attr:`FileRelevance.name_level`.
    name_level_paths: list[str] = Field(default_factory=list)
    #: Highest score any member of the cluster earned.
    score: float = 0.0

    def __len__(self) -> int:
        return len(self.file_paths)


class FileRelevance(BaseModel):
    """Relevance of one file to the issue, with the reason it scored."""

    file_path: str
    score: float
    matched_terms: list[str] = Field(default_factory=list)
    is_test: bool = False
    #: Human-readable reasons, e.g. ``"filename contains 'discount'"``.  Emitted
    #: as evidence metadata so a user can see *why* a file was opened.
    reasons: list[str] = Field(default_factory=list)
    #: True when at least one matched term appears in this file's *name* — its
    #: filename, a declared symbol, or a route path — rather than merely in its
    #: text.  Name-level matches are what identify a file as *about* a concept;
    #: text matches are weaker and are excluded when attributing a concept to a
    #: file, so that a widely-imported data model is not treated as belonging to
    #: every concept that happens to appear in it.
    name_level: bool = False


class RelevanceResult(BaseModel):
    """The full relevance assessment for one issue."""

    terms: IssueTerms
    files: list[FileRelevance] = Field(default_factory=list)
    clusters: list[ConceptCluster] = Field(default_factory=list)
    #: How many candidate files were scored before the cap was applied.
    candidates_considered: int = 0

    def file_paths(self) -> list[str]:
        return [f.file_path for f in self.files]

    def source_paths(self) -> list[str]:
        return [f.file_path for f in self.files if not f.is_test]

    def test_paths(self) -> list[str]:
        return [f.file_path for f in self.files if f.is_test]

    def score_for(self, file_path: str) -> float:
        for entry in self.files:
            if entry.file_path == file_path:
                return entry.score
        return 0.0

    def cluster_for(self, term: str) -> ConceptCluster | None:
        for cluster in self.clusters:
            if cluster.term == term:
                return cluster
        return None


# ── Issue term extraction ──────────────────────────────────────────────────────


def build_issue_terms(issue: str) -> IssueTerms:
    """Extract comparable terms from a natural-language bug report.

    Order is preserved (first mention first) so every downstream ranking is
    stable for the same input.
    """
    raw = issue or ""

    quoted: list[str] = []
    for match in _QUOTED_RE.finditer(raw):
        value = match.group(1) or match.group(2) or ""
        if value and value not in quoted:
            quoted.append(value.strip())

    path_hints: list[str] = []
    for match in _PATH_LIKE_RE.finditer(raw):
        value = match.group(0).strip()
        if value and value not in path_hints:
            path_hints.append(value)

    terms: list[str] = []
    for word in _WORD_RE.findall(raw):
        lowered = word.lower()
        if len(lowered) < MIN_TERM_LENGTH or lowered in STOPWORDS:
            continue
        if lowered not in terms:
            terms.append(lowered)

    # Sub-words of quoted identifiers are strong intent signals even when the
    # whole identifier is not a single word.
    for identifier in quoted:
        for piece in sorted(split_identifier(identifier)):
            if piece not in terms and piece not in STOPWORDS:
                terms.append(piece)

    return IssueTerms(
        raw=raw,
        terms=terms,
        quoted_identifiers=quoted,
        path_hints=path_hints,
    )


# ── Scoring ────────────────────────────────────────────────────────────────────

#: Point values, named so the evidence can explain the score.
W_FILENAME = 4.0
W_ROUTE = 4.0
W_SYMBOL = 3.0
W_MODULE = 2.0
W_ARGUMENT = 1.0
W_BODY = 0.5

#: A file must reach this score to be considered relevant at all.
MIN_RELEVANT_SCORE = 1.0


def _score_file(
    analysis: FileAnalysis | None,
    file_path: str,
    terms: Sequence[str],
    is_test: bool,
) -> FileRelevance:
    """Score one file against every issue term.

    Ties are broken by file path so the ranking is fully deterministic.
    """
    scores: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}
    matched: set[str] = set()
    name_level: set[str] = set()

    def bump(term: str, amount: float, reason: str, *, by_name: bool = False) -> None:
        scores[term] = scores.get(term, 0.0) + amount
        reasons.setdefault(term, [])
        if reason not in reasons[term]:
            reasons[term].append(reason)
        matched.add(term)
        if by_name:
            name_level.add(term)

    # ── Filename and directory ──────────────────────────────────────────────
    stem = file_path.rsplit("/", 1)[-1]
    stem_parts = _substrings(stem)
    directory = file_path.rsplit("/", 1)[0] if "/" in file_path else ""

    for term in terms:
        term_parts = {term} | split_identifier(term)
        if term_parts & stem_parts:
            bump(term, W_FILENAME, f"filename contains '{term}'", by_name=True)
        elif directory and (term_parts & _substrings(directory)):
            bump(term, W_MODULE, f"directory name contains '{term}'", by_name=True)

    if analysis is None or not analysis.supported:
        return FileRelevance(
            file_path=file_path,
            score=sum(scores.values()),
            matched_terms=sorted(matched),
            is_test=is_test,
            reasons=_flatten_reasons(reasons),
            name_level=bool(name_level),
        )

    # ── Routes ──────────────────────────────────────────────────────────────
    for route in analysis.routes:
        route_parts = _substrings(f"{route.path} {route.function_name}")
        for term in terms:
            if ({term} | split_identifier(term)) & route_parts:
                bump(
                    term,
                    W_ROUTE,
                    f"defines route {route.http_method} {route.path} "
                    f"({route.function_name})",
                    by_name=True,
                )

    # ── Declared symbols ────────────────────────────────────────────────────
    symbols: list[tuple[str, int]] = [
        (fn.name, fn.line_start) for fn in analysis.functions
    ]
    for cls in analysis.classes:
        symbols.append((cls.name, cls.line_start))
        symbols.extend((m.name, m.line_start) for m in cls.methods)

    symbol_parts: dict[str, set[str]] = {}
    for name, _line in symbols:
        symbol_parts.setdefault(name, set()).update(split_identifier(name))

    for term in terms:
        term_parts = {term} | split_identifier(term)
        if any(term_parts & parts for parts in symbol_parts.values()):
            bump(
                term,
                W_SYMBOL,
                f"declares a function or class matching '{term}'",
                by_name=True,
            )

    # ── Argument names ──────────────────────────────────────────────────────
    argument_parts: set[str] = set()
    for fn in analysis.functions:
        argument_parts.update(split_identifier(" ".join(fn.arguments)))
    for cls in analysis.classes:
        for method in cls.methods:
            argument_parts.update(split_identifier(" ".join(method.arguments)))
    for term in terms:
        if ({term} | split_identifier(term)) & argument_parts:
            bump(term, W_ARGUMENT, f"has a parameter name matching '{term}'")

    # ── Callee expressions ──────────────────────────────────────────────────
    if analysis.top_level_calls:
        call_parts: set[str] = set()
        for call in analysis.top_level_calls:
            call_parts.update(split_identifier(call.callable_expr))
        for term in terms:
            if ({term} | split_identifier(term)) & call_parts:
                bump(term, W_ARGUMENT, f"calls a function matching '{term}'")

    return FileRelevance(
        file_path=file_path,
        score=sum(scores.values()),
        matched_terms=sorted(matched),
        is_test=is_test,
        reasons=_flatten_reasons(reasons),
        name_level=bool(name_level),
    )


def _flatten_reasons(reasons: dict[str, list[str]]) -> list[str]:
    """Merge per-term reasons into a stable, de-duplicated list."""
    merged: list[str] = []
    for term in reasons:
        for reason in reasons[term]:
            if reason not in merged:
                merged.append(reason)
    return merged


def rank_files_by_relevance(
    ast: RepositoryASTAnalysis,
    terms: IssueTerms,
    *,
    test_paths: Iterable[str] = (),
    max_files: int = 12,
    min_score: float = MIN_RELEVANT_SCORE,
) -> RelevanceResult:
    """Rank every file in ``ast`` by how relevant it is to ``terms``.

    Args:
        ast: Phase 4 analysis of the repository.
        terms: Output of :func:`build_issue_terms`.
        test_paths: Repository-relative paths that are test files, so they can
            be labelled (the Test Investigator uses the same list).
        max_files: Cap on returned files.  Overflow is recorded in
            ``candidates_considered`` rather than silently dropped.
        min_score: Files below this are not returned.

    Returns:
        A :class:`RelevanceResult` with files sorted by descending score and
        then by path, so equal scores never reorder between runs.
    """
    if max_files < 1:
        raise ValueError("max_files must be at least 1")

    is_test = set(test_paths)
    by_path = {f.relative_path: f for f in ast.files}

    scored: list[FileRelevance] = []
    for file_path, analysis in by_path.items():
        entry = _score_file(analysis, file_path, terms.terms, file_path in is_test)
        if entry.score >= min_score:
            scored.append(entry)

    scored.sort(key=lambda e: (-e.score, e.file_path))
    considered = len(scored)

    # ── Clusters, one per issue term ────────────────────────────────────────
    cluster_map: dict[str, list[str]] = {}
    name_level_map: dict[str, list[str]] = {}
    for entry in scored[:max_files]:
        for term in entry.matched_terms:
            cluster_map.setdefault(term, []).append(entry.file_path)
            if entry.name_level:
                name_level_map.setdefault(term, []).append(entry.file_path)

    clusters = [
        ConceptCluster(
            term=term,
            file_paths=sorted(paths),
            name_level_paths=sorted(name_level_map.get(term, [])),
            score=sum(
                e.score for e in scored[:max_files] if term in e.matched_terms
            ),
        )
        for term, paths in cluster_map.items()
    ]
    # Concept order follows the issue text, so the report reads in the
    # reporter's own order of concepts.
    term_order = {t: i for i, t in enumerate(terms.terms)}
    clusters.sort(key=lambda c: (term_order.get(c.term, len(term_order)), c.term))

    return RelevanceResult(
        terms=terms,
        files=scored[:max_files],
        clusters=clusters,
        candidates_considered=considered,
    )


__all__ = [
    "MIN_RELEVANT_SCORE",
    "STOPWORDS",
    "ConceptCluster",
    "FileRelevance",
    "IssueTerms",
    "RelevanceResult",
    "build_issue_terms",
    "rank_files_by_relevance",
    "split_identifier",
]
