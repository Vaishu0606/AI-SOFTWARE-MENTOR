"use client";

/**
 * Fix proposal / code review panel, and the human approval gate.
 *
 * Two rules this panel exists to enforce visually:
 *   1. The diff is shown in full before anything is written. There is no
 *      "apply" affordance anywhere in this component — the only action is
 *      approval, which posts to the backend's gate endpoint.
 *   2. `applied` and `tests_executed` are rendered as the hard-wired `false`
 *      values the backend sends. A proposal that reads as already applied, or
 *      as already tested, is a bug worth showing loudly.
 */

import { useState } from "react";
import type { FixProposalResponse } from "@/app/lib/types";
import { ChangeReview, UnifiedDiff } from "./diff";
import { Badge, Card, Code, Empty, Field, Honest, timestamp, statusTone } from "./primitives";

export function FixProposalPanel({ proposal }: { proposal: FixProposalResponse }) {
  const [view, setView] = useState<"diff" | "changes">("diff");

  return (
    <Card
      title="Fix proposal — for review"
      subtitle={`Drafted ${timestamp(proposal.created_at)} · read at state ${proposal.run_state} · nothing has been written`}
      tone={proposal.ready_for_review ? "accent" : "default"}
      right={
        <>
          <Badge tone={statusTone(proposal.status)}>{proposal.status}</Badge>
          <Badge tone={statusTone(proposal.risk.level)}>risk {proposal.risk.level}</Badge>
        </>
      }
    >
      <div className="mb-3 flex flex-wrap items-center gap-2 font-mono text-[10px]">
        <Badge tone="neutral">applied: {String(proposal.applied)}</Badge>
        <Badge tone="neutral">tests_executed: {String(proposal.tests_executed)}</Badge>
        <Badge tone="good">+{proposal.added_lines}</Badge>
        <Badge tone="bad">-{proposal.removed_lines}</Badge>
        <Badge tone="neutral">
          {proposal.changes.length} change(s) · {proposal.affected_files.length} file(s)
        </Badge>
      </div>

      {!proposal.ready_for_review ? (
        <div className="space-y-2">
          <Honest>
            The proposer returned <strong>{proposal.status}</strong> and produced no
            reviewable diff. That is an honest negative, not a crash — this tool refuses to
            propose a change it cannot support.
          </Honest>
          <p className="text-[13px] leading-relaxed text-slate-300">{proposal.rationale}</p>
        </div>
      ) : (
        <>
          <p className="mb-3 text-[13px] leading-relaxed text-slate-300">
            {proposal.rationale}
          </p>

          <dl className="mb-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {proposal.anchor && (
              <Field label="Target location">
                <span className="font-mono text-[12px]">
                  {proposal.anchor.file_path}
                  {proposal.anchor.line !== null && `:${proposal.anchor.line}`}
                  {proposal.anchor.function && (
                    <span className="text-slate-400"> in {proposal.anchor.function}()</span>
                  )}
                </span>
              </Field>
            )}
            <Field label="Affected files">
              <ul className="space-y-0.5">
                {proposal.affected_files.map((file) => (
                  <li key={file} className="font-mono text-[12px]">
                    {file}
                  </li>
                ))}
              </ul>
            </Field>
            <Field label="Proposal fingerprint">
              {proposal.proposal_fingerprint ? (
                <span
                  className="break-all font-mono text-[11px] text-sky-300"
                  title="Send this back as expected_fingerprint so the backend can prove you approved this exact diff"
                >
                  {proposal.proposal_fingerprint}
                </span>
              ) : (
                <span className="font-mono text-[12px] text-slate-500">—</span>
              )}
            </Field>
          </dl>

          <div className="mb-2 flex items-center gap-2">
            {(["diff", "changes"] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                onClick={() => setView(mode)}
                className={`rounded-md border px-2.5 py-0.5 font-mono text-[11px] ${
                  view === mode
                    ? "border-sky-700 bg-sky-950 text-sky-300"
                    : "border-slate-700 bg-slate-800 text-slate-400 hover:bg-slate-700"
                }`}
              >
                {mode === "diff" ? "unified diff" : "before / after"}
              </button>
            ))}
          </div>

          {view === "diff" ? (
            proposal.unified_diff ? (
              <UnifiedDiff diff={proposal.unified_diff} />
            ) : (
              <Empty>The backend returned no unified diff for this proposal.</Empty>
            )
          ) : (
            <div className="space-y-4">
              {proposal.changes.map((change, i) => (
                <ChangeReview key={i} change={change} />
              ))}
            </div>
          )}
        </>
      )}

      {/* ── Risk ─────────────────────────────────────────────────────────── */}
      {proposal.ready_for_review && (
        <div className="mt-5 rounded-lg border border-slate-800 bg-slate-950/50 p-3">
          <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wider text-slate-500">
            risk assessment — {proposal.risk.level}
          </p>
          {proposal.rationale && proposal.risk.rationale && (
            <p className="mb-2 text-[12px] leading-relaxed text-slate-400">
              {proposal.risk.rationale}
            </p>
          )}
          <ul className="space-y-0.5">
            {proposal.risk.factors.map((factor, i) => (
              <li key={i} className="text-[12px] text-slate-300">
                · {factor}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ── Validation plan ──────────────────────────────────────────────── */}
      {proposal.validation_plan?.steps?.length > 0 && (
        <div className="mt-4">
          <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wider text-slate-500">
            validation plan — what a developer should check, never what was checked
          </p>
          <ol className="list-inside list-decimal space-y-0.5">
            {proposal.validation_plan.steps.map((step, i) => (
              <li key={i} className="text-[12px] text-slate-300">
                {step}
              </li>
            ))}
          </ol>
          {proposal.validation_plan.notes && (
            <p className="mt-1.5 text-[12px] text-slate-500">
              {proposal.validation_plan.notes}
            </p>
          )}
        </div>
      )}

      {/* ── Assumptions and limitations ──────────────────────────────────── */}
      {proposal.assumptions.length > 0 && (
        <div className="mt-4">
          <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-slate-500">
            assumptions
          </p>
          <ul className="space-y-0.5">
            {proposal.assumptions.map((item, i) => (
              <li key={i} className="text-[12px] text-slate-400">
                · {item}
              </li>
            ))}
          </ul>
        </div>
      )}

      {proposal.limitations.length > 0 && (
        <div className="mt-4 space-y-1">
          <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
            limitations — what this proposal does not establish
          </p>
          {proposal.limitations.map((item, i) => (
            <Honest key={i}>{item}</Honest>
          ))}
        </div>
      )}

      {proposal.supporting_evidence.length > 0 && (
        <p className="mt-4 font-mono text-[10px] text-slate-600">
          grounded in {proposal.supporting_evidence.length} evidence item(s) ·{" "}
          {proposal.root_cause_kind ?? "unclassified"} · root-cause confidence{" "}
          {proposal.root_cause_confidence.toFixed(2)} ({proposal.root_cause_confidence_level})
        </p>
      )}
    </Card>
  );
}

/* ── The gate ──────────────────────────────────────────────────────────────── */

export function ApprovalGate({
  approvedBy,
  comment,
  fingerprint,
  onApproveByChange,
  onApproveCommentChange,
  onSubmit,
  busy,
  disabled,
  error,
}: {
  approvedBy: string;
  comment: string;
  fingerprint: string | null;
  onApproveByChange: (value: string) => void;
  onApproveCommentChange: (value: string) => void;
  onSubmit: () => void;
  busy: boolean;
  disabled: boolean;
  error: string | null;
}) {
  const canSubmit = approvedBy.trim().length > 0 && !busy && !disabled;

  return (
    <Card
      title="Human approval gate"
      subtitle="POST /api/mentor/runs/{run_id}/approve-fix"
      tone="accent"
    >
      <Honest>
        Approving is the only thing in this system that can write to a repository. The
        backend applies the stored proposal, re-reads the file to confirm what was
        written, and then runs the repository&apos;s own test suite. Nothing is applied
        without this step.
      </Honest>

      <div className="mt-4 space-y-3">
        <label className="block">
          <span className="mb-1 block font-mono text-[11px] uppercase tracking-wider text-slate-400">
            approved_by <span className="text-rose-400">*</span>
          </span>
          <input
            value={approvedBy}
            onChange={(e) => onApproveByChange(e.target.value)}
            placeholder="your name or team"
            maxLength={200}
            className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-[13px] text-slate-100 outline-none placeholder:text-slate-600 focus:border-sky-600"
          />
          <span className="mt-1 block text-[11px] text-slate-500">
            An audit label, not an authenticated identity — this project has no auth layer.
          </span>
        </label>

        <label className="block">
          <span className="mb-1 block font-mono text-[11px] uppercase tracking-wider text-slate-400">
            comment <span className="text-slate-600">(optional)</span>
          </span>
          <textarea
            value={comment}
            onChange={(e) => onApproveCommentChange(e.target.value)}
            rows={2}
            maxLength={4000}
            placeholder="What you checked in the diff, and why you are approving it"
            className="w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-[13px] text-slate-100 outline-none placeholder:text-slate-600 focus:border-sky-600"
          />
        </label>

        {fingerprint && (
          <div>
            <span className="mb-1 block font-mono text-[11px] uppercase tracking-wider text-slate-400">
              expected_fingerprint <span className="text-slate-600">(compare-and-swap)</span>
            </span>
            <Code className="text-sky-300">{fingerprint}</Code>
            <p className="mt-1 text-[11px] text-slate-500">
              Sent back so the backend can prove you approved <em>this</em> diff. If the
              proposal changed since you loaded it, the approval is refused with a 409.
            </p>
          </div>
        )}
      </div>

      {error && (
        <div className="mt-3 rounded-lg border border-rose-800/70 bg-rose-950/40 px-3 py-2">
          <p className="font-mono text-[10px] uppercase tracking-wider text-rose-300">
            approval refused
          </p>
          <p className="mt-0.5 text-[13px] leading-relaxed text-rose-200">{error}</p>
        </div>
      )}

      <button
        type="button"
        onClick={onSubmit}
        disabled={!canSubmit}
        className="mt-4 w-full rounded-lg border border-emerald-700 bg-emerald-950 px-4 py-2.5 text-[14px] font-semibold text-emerald-200 transition-colors hover:bg-emerald-900 disabled:cursor-not-allowed disabled:border-slate-700 disabled:bg-slate-800 disabled:text-slate-500"
      >
        {busy
          ? "Approving, applying and verifying…"
          : disabled
            ? "No reviewable proposal to approve"
            : "Approve Fix & run verification"}
      </button>

      <p className="mt-2 text-center font-mono text-[10px] text-slate-600">
        {approvedBy.trim().length === 0
          ? "Enter a reviewer name to enable approval"
          : disabled
            ? "The proposal is not READY_FOR_REVIEW"
            : "This will write to the repository"}
      </p>
    </Card>
  );
}
