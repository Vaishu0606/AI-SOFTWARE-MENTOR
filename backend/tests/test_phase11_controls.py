"""
Phase 11 static controls — the human approval gate cannot be bypassed.

Phases 6, 8, 9 and 10 each ship an ``ast``-based control file that pins the
capabilities their modules are *not* allowed to have, because those capabilities
are what the phase's honesty depends on.  Phase 11 needs one too, for a sharper
reason than the earlier phases: it is the first phase that can write to somebody's
source tree, so the question is no longer "can it write?" but "what stops it
writing the wrong thing?".

Three invariants are checked here, and each is a way the gate could fail that a
functional test would not necessarily catch:

* **The HTTP layer writes nothing itself.**  ``app/api/mentor.py`` is already
  covered by the Phase 6 "never write repository files" control, and it stayed
  covered after Phase 11 added a route that *causes* writes.  That is worth
  asserting on its own terms: the approve route must delegate, and the reason it
  can is that every stage below it already owns its own preconditions.

* **The approval request cannot carry a patch.**  Enumerated from the model
  rather than from the source, so adding a ``diff`` or a ``file_path`` field later
  fails here instead of quietly giving the API a write primitive.

* **``approve_fix`` is the only door.**  Nothing outside the orchestrator
  transitions a run into ``FIX_APPROVED``, and nothing writes the approval
  context key.  If a second door existed, the HTTP tests above would still pass
  while the gate had quietly become advisory.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.api.mentor import ApproveFixRequest
from app.models.fix_approval import FIX_APPROVAL_KEY
from app.models.workflow import WorkflowState

BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: The modules Phase 11 introduced or extended.
PHASE11_MODULES = [
    "app/api/mentor.py",
    "app/services/fix_approval.py",
    "app/services/fix_applier.py",
    "app/services/fix_application_stage.py",
    "app/services/verification_stage.py",
    "app/services/test_executor.py",
]

#: Attributes that would let a module write to a repository or run a command.
FORBIDDEN_WRITE_CALLS = {
    "write_text",
    "write_bytes",
    "writelines",
    "unlink",
    "remove",
    "rmdir",
    "rename",
    "replace",
    "move",
    "copy",
    "copytree",
    "mkdir",
    "rmtree",
    "run",
    "Popen",
    "call",
    "check_output",
    "system",
}

#: The context key that records an approval.  One writer, named explicitly.
APPROVAL_CONTEXT_KEY = FIX_APPROVAL_KEY


def _read(module: str) -> str:
    return (BACKEND_ROOT / module).read_text(encoding="utf-8")


def _tree(module: str) -> ast.Module:
    return ast.parse(_read(module))


def _attribute_calls(module: str) -> list[str]:
    return [
        node.func.attr
        for node in ast.walk(_tree(module))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]


# ── The HTTP layer delegates ──────────────────────────────────────────────────


def test_the_api_module_writes_no_files_itself() -> None:
    """``app/api/mentor.py`` gained a write-*causing* route in Phase 11.

    The Phase 6 control already lists this module among those that must never
    write a repository file, and it still holds — the route calls the
    orchestrator, which calls the applier, which owns the preconditions.  Asserted
    here because "the module with a write route performs no writes" is the whole
    design, and it is the kind of thing a later convenience refactor breaks.
    """
    mutations = [
        name
        for name in _attribute_calls("app/api/mentor.py")
        if name in FORBIDDEN_WRITE_CALLS
    ]
    assert mutations == [], (
        "app/api/mentor.py must delegate the write to the orchestrator, but calls "
        f"{mutations}"
    )


def test_the_approve_route_goes_through_the_orchestrator() -> None:
    """The route's only way to change a run is the orchestrator's approval.

    Checked structurally: the function body names ``approve_and_continue`` and
    does not reach for the applier, the store, or a state constant.
    """
    tree = _tree("app/api/mentor.py")
    route = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "approve_fix"
    )
    called = {
        node.func.attr
        for node in ast.walk(route)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "approve_and_continue" in called
    assert "apply" not in called
    assert "save" not in called
    assert "transition" not in called


# ── The request cannot carry a patch ──────────────────────────────────────────


def test_the_approval_request_has_no_field_that_could_carry_a_change() -> None:
    """Enumerated from the model, so adding one later fails here.

    ``extra="forbid"`` handles a client that posts a patch, and this handles a
    developer who adds a field: the gate's integrity must not depend on nobody
    later deciding that ``file_path`` would be a useful addition.
    """
    fields = set(ApproveFixRequest.model_fields)

    assert fields == {"approved_by", "comment", "expected_fingerprint", "approved"}
    assert ApproveFixRequest.model_config.get("extra") == "forbid"

    forbidden = {
        "diff",
        "unified_diff",
        "patch",
        "file_path",
        "files",
        "line",
        "line_end",
        "before",
        "after",
        "content",
        "command",
        "current_state",
        "state",
        "status",
        "run_id",
    }
    assert not (fields & forbidden), f"the approval body may not carry {fields & forbidden}"


def test_the_approval_field_cannot_express_a_refusal() -> None:
    """``approved`` is ``Literal[True]``, so the type refuses ``false``.

    A stored ``approved=False`` would be a permanent veto that later phases have
    to learn to honour — which is exactly how a gate acquires a bypass.  "No" is
    an HTTP 409 that records nothing.
    """
    annotation = ApproveFixRequest.model_fields["approved"].annotation
    assert "Literal" in str(annotation) and "True" in str(annotation)


# ── One door, named ───────────────────────────────────────────────────────────


def test_only_the_orchestrator_transitions_into_fix_approved() -> None:
    """No other module may name ``FIX_APPROVED`` as a transition target.

    If a stage, the store or the API could move a run into ``FIX_APPROVED``
    directly, the approval would be recorded *after* the fact rather than being
    the thing that permitted the write.  Checked over every module under
    ``app/`` so a new file cannot add a second door.
    """
    offenders: list[str] = []
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        relative = path.relative_to(BACKEND_ROOT).as_posix()
        if relative == "app/services/orchestrator.py":
            continue
        if "FIX_APPROVED" not in _read(relative):
            continue
        # Naming the state in a guard, a message or a __all__ is fine; the only
        # thing that matters is that nobody else *writes* it.
        if _writes_fix_approved(_read(relative)):
            offenders.append(relative)

    assert offenders == [], f"only the orchestrator may transition to FIX_APPROVED: {offenders}"


def _writes_fix_approved(source: str) -> bool:
    """True when ``FIX_APPROVED`` appears as a call argument to something.

    A conservative textual check rather than a full dataflow analysis: it is
    looking for a *transition call*, so it flags ``_transition(run,
    WorkflowState.FIX_APPROVED, ...)`` and leaves a comparison or a message
    alone.  The full guarantee is the state machine's own assertion, which every
    transition already goes through.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Attribute) and arg.attr == "FIX_APPROVED":
                return True
    return False


def test_only_the_orchestrator_writes_the_approval_context_key() -> None:
    """One writer for the stored approval, so it cannot be forged from elsewhere.

    ``run.context`` is a plain dict, which means anything holding a run could in
    principle insert an approval.  Nothing in this codebase does, and this test
    is what keeps that true as the project grows.
    """
    offenders: list[str] = []
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        relative = path.relative_to(BACKEND_ROOT).as_posix()
        if relative in (
            "app/services/orchestrator.py",
            "app/models/fix_approval.py",
            "app/api/mentor.py",
        ):
            continue
        if APPROVAL_CONTEXT_KEY in _read(relative):
            offenders.append(relative)

    assert offenders == [], f"only the orchestrator may store an approval: {offenders}"


# ── The read endpoints are read-only ──────────────────────────────────────────


@pytest.mark.parametrize(
    "route_name", ["get_fix_application", "get_verification"]
)
def test_the_phase11_read_routes_never_write(route_name: str) -> None:
    """A read endpoint that writes is a read endpoint nobody can trust.

    The Phase 6 control would catch a raw ``write_text``; this catches the subtler
    version, where a read route calls something that has a side effect.
    """
    tree = _tree("app/api/mentor.py")
    route = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == route_name
    )
    called = {
        node.func.attr
        for node in ast.walk(route)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "approve_and_continue" not in called
    assert "approve_fix" not in called
    assert "advance" not in called
    assert "complete_run" not in called
    # A read route re-derives nothing; it must load from stored context.
    assert called & {"load_fix_application", "load_verification"}


def test_the_approve_route_is_the_only_one_that_can_reach_completed() -> None:
    """``COMPLETED`` requires a real exit code, and only approval drives a run there.

    ``complete_run`` is server-side and reachable from ``approve_and_continue``
    alone.  Asserted as a state-machine fact rather than a source scan, because
    that is where the guarantee actually lives.
    """
    from app.services.state_machine import WorkflowStateMachine

    machine = WorkflowStateMachine()
    # Nothing in the forward graph enters COMPLETED from a stage state directly:
    # the only predecessor is VERIFIED, which itself requires a stored result.
    predecessors = {
        source
        for source, targets in machine.transitions.items()
        if WorkflowState.COMPLETED in targets
    }
    assert predecessors == {WorkflowState.VERIFIED}
