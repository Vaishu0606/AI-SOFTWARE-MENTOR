"use client";

/**
 * AI Software Mentor — operator console.
 *
 * This page is a thin client over the backend's mentor API. Every figure it
 * shows comes from a real response; nothing is computed optimistically and
 * nothing is fabricated. In particular:
 *
 * * `POST /runs` is synchronous on the server, so the run it returns is already
 *   at the approval gate. The UI shows a busy state for the duration rather
 *   than pretending to poll.
 * * The event timeline comes from the SSE stream, which replays the run's own
 *   append-only history. There is no polling.
 * * A 409 from a phase read endpoint means "that phase has not answered yet",
 *   not "error", and is rendered as an honest empty state.
 * * No fix is ever applied without the reviewer pressing Approve, and the
 *   fingerprint the client displays is echoed back so the backend can prove
 *   which diff was approved.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  API_BASE,
  ApiError,
  ApprovalRefused,
  StageNotReady,
  approveFix,
  createRun,
  getRun,
  getFixProposal,
  getInvestigation,
  getRootCause,
  getTestPlan,
  getVerification,
  getFixApplication,
  openEventStream,
} from "@/app/lib/api";
import type {
  ApproveFixResponse,
  Evidence,
  FixApplicationResponse,
  FixProposalResponse,
  InvestigationResponse,
  MentorRunResponse,
  RootCauseResponse,
  TestPlanResponse,
  VerificationResult,
  WorkflowEvent,
} from "@/app/lib/types";

import { Badge, Card, Empty, Honest, Mono } from "@/app/components/primitives";
import { StageTimeline, EventLog } from "@/app/components/StageTimeline";
import { EvidencePanel } from "@/app/components/EvidencePanel";
import { RootCausePanel } from "@/app/components/RootCausePanel";
import { ApprovalGate, FixProposalPanel } from "@/app/components/FixProposalPanel";
import {
  ApplicationPanel,
  TestPlanPanel,
  VerificationPanel,
} from "@/app/components/VerificationPanel";
import { FinalResultCard, RunHeader } from "@/app/components/FinalResultCard";

const DEMO_ISSUE = "Checkout is failing when a discount code is applied.";

/** The controlled ShopDemo checkout app, which contains the seeded defect. */
const DEMO_REPO = "../demo-repository/ShopDemo";

type Phase<T> =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "ready"; data: T }
  | { state: "notReady"; detail: string }
  | { state: "error"; detail: string };

const IDLE: Phase<never> = { state: "idle" };

/**
 * Loads one phase artefact for the current run, distinguishing "not answered
 * yet" (a 409) from a genuine failure.
 *
 * The `loading` state is *derived* rather than assigned: a stored phase only
 * counts as current if it was fetched for the run id now on screen. That keeps
 * the effect body free of synchronous setState, and it means switching runs
 * immediately shows a loader instead of the previous run's data.
 */
function usePhase<T>(runId: string | null, load: (id: string) => Promise<T>) {
  const [fetched, setFetched] = useState<{
    runId: string | null;
    phase: Phase<T>;
  }>({ runId: null, phase: IDLE });
  const generation = useRef(0);

  const run = useCallback(
    async (id: string, ticket: number) => {
      try {
        const data = await load(id);
        if (ticket === generation.current) {
          setFetched({ runId: id, phase: { state: "ready", data } });
        }
      } catch (error) {
        if (ticket !== generation.current) return;
        const phase: Phase<T> =
          error instanceof StageNotReady
            ? { state: "notReady", detail: error.detail }
            : {
                state: "error",
                detail:
                  error instanceof ApiError ? error.detail : String(error),
              };
        setFetched({ runId: id, phase });
      }
    },
    [load],
  );

  // Initial fetch on mount and whenever the run changes. State is only ever set
  // from a promise callback, never synchronously in the effect body.
  useEffect(() => {
    if (!runId) return;
    const ticket = ++generation.current;
    void run(runId, ticket);
  }, [runId, run]);

  // Explicit re-fetch (used after approval), which may set "loading" first.
  const reload = useCallback(async () => {
    if (!runId) {
      setFetched({ runId: null, phase: IDLE });
      return;
    }
    const ticket = ++generation.current;
    setFetched({ runId, phase: { state: "loading" } });
    await run(runId, ticket);
  }, [runId, run]);

  const phase: Phase<T> =
    !runId
      ? IDLE
      : fetched.runId === runId
        ? fetched.phase
        : { state: "loading" };

  // Spread so callers narrow on `.state` directly (`phase.state === "ready"`).
  return { ...phase, reload };
}

// Module-level loaders: stable identities, so no hook is needed to keep them.
const loadInvestigation = (id: string) => getInvestigation(id);
const loadRootCause = (id: string) => getRootCause(id);
const loadFixProposal = (id: string) => getFixProposal(id);
const loadTestPlan = (id: string) => getTestPlan(id);
const loadVerification = (id: string) => getVerification(id);
const loadApplication = (id: string) => getFixApplication(id);

export default function MentorConsole() {
  const [repositoryPath, setRepositoryPath] = useState(DEMO_REPO);
  const [issue, setIssue] = useState(DEMO_ISSUE);
  const [approvedBy, setApprovedBy] = useState("");
  const [comment, setComment] = useState("");

  const [run, setRun] = useState<MentorRunResponse | null>(null);
  const [events, setEvents] = useState<WorkflowEvent[]>([]);
  const [streamError, setStreamError] = useState<string | null>(null);

  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [approving, setApproving] = useState(false);
  const [approvalError, setApprovalError] = useState<string | null>(null);
  const [approval, setApproval] = useState<ApproveFixResponse | null>(null);
  const [runIdInput, setRunIdInput] = useState("");

  const runId = run?.run_id ?? null;

  const investigation = usePhase<InvestigationResponse>(runId, loadInvestigation);
  const rootCause = usePhase<RootCauseResponse>(runId, loadRootCause);
  const proposal = usePhase<FixProposalResponse>(runId, loadFixProposal);
  const testPlan = usePhase<TestPlanResponse>(runId, loadTestPlan);

  // After approval the authoritative records come from the approval response
  // and the read endpoints; both must agree, so the reads are still performed.
  const verification = usePhase<VerificationResult>(runId, loadVerification);
  const application = usePhase<FixApplicationResponse>(runId, loadApplication);

  /* ── SSE: one stream per run, replaying the server's event history ───── */
  useEffect(() => {
    // Both `events` and `streamError` are reset by the handlers that change the
    // run, so this effect only opens and closes the subscription.
    if (!runId) return;

    const seen = new Set<string>();
    const buffer: WorkflowEvent[] = [];

    const handle = (event: WorkflowEvent) => {
      if (seen.has(event.event_id)) return;
      seen.add(event.event_id);
      buffer.push(event);
      setEvents([...buffer].sort((a, b) => a.sequence - b.sequence));
    };

    return openEventStream(
      runId,
      handle,
      (message) => setStreamError(message),
    );
  }, [runId]);

  /* ── Start a run ─────────────────────────────────────────────────────── */
  const startRun = async () => {
    setStarting(true);
    setStartError(null);
    setApprovalError(null);
    setApproval(null);
    setEvents([]);
    setStreamError(null);

    try {
      const created = await createRun(repositoryPath.trim(), issue.trim());
      setRun(created);
      setRunIdInput(created.run_id);
    } catch (error) {
      setStartError(
        error instanceof ApiError || error instanceof StageNotReady
          ? error.detail
          : String(error),
      );
    } finally {
      setStarting(false);
    }
  };

  /* ── Attach to an existing run id ────────────────────────────────────── */
  const attachToRun = async () => {
    const id = runIdInput.trim();
    if (!id) return;
    setStarting(true);
    setStartError(null);
    setApproval(null);
    setEvents([]);
    setStreamError(null);
    try {
      setRun(await getRun(id));
    } catch (error) {
      setRun(null);
      setStartError(
        error instanceof ApiError || error instanceof StageNotReady
          ? error.detail
          : String(error),
      );
    } finally {
      setStarting(false);
    }
  };

  /* ── The approval gate ───────────────────────────────────────────────── */
  const submitApproval = async () => {
    if (!runId) return;
    setApproving(true);
    setApprovalError(null);

    const fingerprint =
      proposal.state === "ready" ? proposal.data.proposal_fingerprint : null;

    try {
      const result = await approveFix(runId, {
        approved_by: approvedBy.trim(),
        comment: comment.trim(),
        // Echo the fingerprint we displayed. The backend refuses the approval
        // if the proposal changed since it was loaded.
        expected_fingerprint: fingerprint,
      });

      setApproval(result);
      setRun(result.run);

      // The run is now terminal, so re-read the durable records rather than
      // trusting the POST body alone.
      await Promise.all([
        investigation.reload(),
        application.reload(),
        verification.reload(),
        testPlan.reload(),
      ]);
    } catch (error) {
      setApprovalError(
        error instanceof ApiError || error instanceof ApprovalRefused
          ? error.detail
          : String(error),
      );
    } finally {
      setApproving(false);
    }
  };

  /* ── Derived view state ──────────────────────────────────────────────── */
  const proposalData: FixProposalResponse | null =
    proposal.state === "ready" ? proposal.data : null;
  const verificationData: VerificationResult | null =
    verification.state === "ready" ? verification.data : null;
  const applicationData: FixApplicationResponse | null =
    application.state === "ready" ? application.data : null;
  const approvalRecord = approval?.approval ?? null;

  const atGate =
    proposalData?.ready_for_review === true &&
    run?.current_state === "FIX_PROPOSAL_READY";

  const evidence: Evidence[] =
    investigation.state === "ready" ? investigation.data.evidence : (run?.evidence ?? []);

  const evidenceTotals = (investigation.state === "ready"
    ? investigation.data.evidence_counts
    : undefined) ?? {};

  return (
    <main className="min-h-screen bg-slate-950 text-slate-200">
      {/* ── Masthead ─────────────────────────────────────────────────── */}
      <header className="border-b border-slate-800 bg-slate-900/40">
        <div className="mx-auto max-w-7xl px-5 py-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h1 className="text-lg font-semibold tracking-tight text-slate-100">
                AI Software Mentor
              </h1>
              <p className="text-[13px] text-slate-400">
                Agentic debugging and maintenance for unfamiliar codebases
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="neutral">backend: {API_BASE}</Badge>
              {run && <Badge tone={run.status === "COMPLETED" ? "good" : "info"}>{run.status}</Badge>}
              {streamError && <Badge tone="warn">stream: {streamError}</Badge>}
            </div>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-7xl space-y-5 px-5 py-6">
        {/* ── Launcher ──────────────────────────────────────────────── */}
        <Card
          title="Start an investigation"
          subtitle="POST /api/mentor/runs — the server walks the whole pipeline synchronously and stops at the human approval gate"
        >
          <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
            <label className="block">
              <span className="mb-1 block font-mono text-[11px] uppercase tracking-wider text-slate-400">
                issue
              </span>
              <textarea
                value={issue}
                onChange={(e) => setIssue(e.target.value)}
                rows={2}
                className="w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-[13px] text-slate-100 outline-none focus:border-sky-600"
              />
            </label>

            <label className="block">
              <span className="mb-1 block font-mono text-[11px] uppercase tracking-wider text-slate-400">
                repository path
              </span>
              <input
                value={repositoryPath}
                onChange={(e) => setRepositoryPath(e.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-[13px] text-slate-100 outline-none focus:border-sky-600"
              />
              <span className="mt-1 block text-[11px] text-slate-500">
                A path the backend server can read. The seeded defect lives in{" "}
                <span className="font-mono">{DEMO_REPO}</span>.
              </span>
            </label>
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={startRun}
              disabled={starting || issue.trim().length === 0 || repositoryPath.trim().length === 0}
              className="rounded-lg border border-sky-700 bg-sky-950 px-5 py-2.5 text-[14px] font-semibold text-sky-200 transition-colors hover:bg-sky-900 disabled:cursor-not-allowed disabled:border-slate-700 disabled:bg-slate-800 disabled:text-slate-500"
            >
              {starting ? "Investigating…" : "Start Investigation"}
            </button>

            <div className="flex items-center gap-2">
              <input
                value={runIdInput}
                onChange={(e) => setRunIdInput(e.target.value)}
                placeholder="or paste an existing run id"
                className="w-64 rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-[12px] text-slate-200 outline-none placeholder:text-slate-600 focus:border-sky-600"
              />
              <button
                type="button"
                onClick={attachToRun}
                disabled={starting || runIdInput.trim().length === 0}
                className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 font-mono text-[12px] text-slate-300 hover:bg-slate-700 disabled:opacity-50"
              >
                attach
              </button>
            </div>

            {starting && (
              <span className="font-mono text-[11px] text-slate-500">
                the server is ingesting, analysing and investigating — this takes a few
                seconds
              </span>
            )}
          </div>

          {startError && (
            <div className="mt-4">
              <Honest>{startError}</Honest>
            </div>
          )}

          <p className="mt-3 font-mono text-[10px] leading-relaxed text-slate-600">
            Note: approving a fix writes to the repository on the server. Running this demo
            against {DEMO_REPO} leaves that checkout with the fix applied, so a second run
            will honestly find nothing to fix.
          </p>
        </Card>

        {/* ── Run header ─────────────────────────────────────────────── */}
        {run && (
          <>
            <RunHeader run={run} />
            {run.error && <Honest>{run.error}</Honest>}
          </>
        )}

        {/* ── Final result ───────────────────────────────────────────── */}
        {run && <FinalResultCard run={run} verification={verificationData} />}

        {/* ── Pipeline + gate ────────────────────────────────────────── */}
        {run && (
          <div className="grid gap-5 lg:grid-cols-[1.4fr_1fr]">
            <StageTimeline events={events} />

            <div className="space-y-5">
              {atGate && !applicationData && (
                <ApprovalGate
                  approvedBy={approvedBy}
                  comment={comment}
                  fingerprint={proposalData?.proposal_fingerprint ?? null}
                  onApproveByChange={setApprovedBy}
                  onApproveCommentChange={setComment}
                  onSubmit={submitApproval}
                  busy={approving}
                  disabled={!proposalData?.ready_for_review}
                  error={approvalError}
                />
              )}

              {approving && (
                <Card title="Approving…">
                  <p className="text-[13px] text-slate-300">
                    The backend is recording the approval, writing the file, re-reading it
                    to confirm the post-image, and then running the repository&apos;s own
                    suite. Nothing is reported until it finishes.
                  </p>
                </Card>
              )}

              {approvalRecord && applicationData && (
                <ApplicationPanel
                  approval={approvalRecord}
                  application={applicationData}
                />
              )}

              {approvalError && !atGate && (
                <Card title="Approval refused">
                  <Honest>{approvalError}</Honest>
                </Card>
              )}

              {!run.is_terminal && !atGate && (
                <Card title="Waiting on the backend">
                  <p className="text-[13px] text-slate-400">
                    This run is at{" "}
                    <Mono>{run.current_state}</Mono>. There is no reviewable proposal to
                    approve yet, so the gate is closed.
                  </p>
                </Card>
              )}
            </div>
          </div>
        )}

        {/* ── Evidence ──────────────────────────────────────────────── */}
        {run && (
          <EvidencePanel
            investigation={
              investigation.state === "ready"
                ? investigation.data
                : {
                    run_id: run.run_id,
                    run_state: run.current_state,
                    status: "PENDING",
                    investigators: [],
                    evidence,
                    evidence_counts: evidenceTotals,
                    errors: [],
                    started_at: null,
                    completed_at: null,
                    tests_executed: false,
                    root_cause_claimed: rootCause.state === "ready",
                  }
            }
          />
        )}

        {/* ── Root cause ────────────────────────────────────────────── */}
        {run && rootCause.state === "ready" && (
          <RootCausePanel analysis={rootCause.data} />
        )}

        {run && rootCause.state === "loading" && (
          <Card title="Root cause">
            <p className="text-[13px] text-slate-500">Loading…</p>
          </Card>
        )}

        {run && rootCause.state === "notReady" && (
          <Card title="Root cause">
            <Honest>
              No root-cause analysis is on record for this run yet. That is the absence of
              an answer, not a failure to answer.
            </Honest>
          </Card>
        )}

        {run && rootCause.state === "error" && (
          <Card title="Root cause">
            <Honest>{rootCause.detail}</Honest>
          </Card>
        )}

        {/* ── Fix proposal ──────────────────────────────────────────── */}
        {run && proposal.state === "ready" && (
          <FixProposalPanel proposal={proposal.data} />
        )}

        {run && proposal.state === "loading" && (
          <Card title="Fix proposal">
            <p className="text-[13px] text-slate-500">Loading…</p>
          </Card>
        )}

        {run && proposal.state === "notReady" && (
          <Card title="Fix proposal">
            <Honest>
              No fix proposal has been recorded for this run. The backend only drafts one
              after a root cause it can support from evidence.
            </Honest>
          </Card>
        )}

        {run && proposal.state === "error" && (
          <Card title="Fix proposal">
            <Honest>{proposal.detail}</Honest>
          </Card>
        )}

        {/* ── Test plan ─────────────────────────────────────────────── */}
        {run && testPlan.state === "ready" && <TestPlanPanel plan={testPlan.data} />}

        {run && testPlan.state === "notReady" && (
          <Card title="Regression test plan">
            <Honest>
              No test plan yet. The planner runs <em>after</em> the approval gate, so this
              is the expected answer for a run that is still waiting for a reviewer.
            </Honest>
          </Card>
        )}

        {run && testPlan.state === "error" && (
          <Card title="Regression test plan">
            <Honest>{testPlan.detail}</Honest>
          </Card>
        )}

        {/* ── Verification ──────────────────────────────────────────── */}
        {run && verification.state === "ready" && (
          <VerificationPanel verification={verification.data} />
        )}

        {run && verification.state === "loading" && (
          <Card title="Verification">
            <p className="text-[13px] text-slate-500">Loading…</p>
          </Card>
        )}

        {run && verification.state === "notReady" && (
          <Card title="Verification">
            <Honest>
              Verification has not been attempted. No test has been executed for this run,
              so nothing about the repository has been verified.
            </Honest>
          </Card>
        )}

        {run && verification.state === "error" && (
          <Card title="Verification">
            <Honest>{verification.detail}</Honest>
          </Card>
        )}

        {run && application.state === "error" && (
          <Card title="Applied fix">
            <Honest>{application.detail}</Honest>
          </Card>
        )}

        {/* ── Event log ─────────────────────────────────────────────── */}
        {run && <EventLog events={events} />}

        {/* ── Empty state before any run ────────────────────────────── */}
        {!run && (
          <Card title="No run yet">
            <Empty>
              Enter the issue and a repository path, then press Start Investigation. The
              pipeline, evidence, root cause, reviewable diff, approval gate and
              verification will appear here as the backend reports them.
            </Empty>
          </Card>
        )}
      </div>

      <footer className="border-t border-slate-800 py-6 text-center font-mono text-[10px] text-slate-600">
        AI Software Mentor · every figure on this page came from the backend · no
        fabricated results
      </footer>
    </main>
  );
}
