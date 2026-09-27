"use client";

/**
 * Root cause panel.
 *
 * The backend distinguishes "determined a root cause" from "insufficient
 * evidence", "no evidence" and "failed", and this panel keeps that distinction
 * visible rather than collapsing it into a verdict. It also surfaces the
 * supporting evidence IDs and the confidence breakdown, because a bare
 * confidence number invites a reader to treat it as a probability.
 */

import type { RootCauseResponse } from "@/app/lib/types";
import { Badge, Card, Code, ConfidenceMeter, Field, Honest, timestamp, statusTone } from "./primitives";

export function RootCausePanel({ analysis }: { analysis: RootCauseResponse }) {
  const breakdown = analysis.confidence_breakdown;
  const penalties = Object.entries(breakdown?.penalties ?? {});

  return (
    <Card
      title="Root cause"
      subtitle={`Synthesised at ${timestamp(analysis.synthesized_at)} · read at state ${analysis.run_state}`}
      tone={analysis.root_cause_claimed ? "good" : "default"}
      right={
        <>
          <Badge tone={statusTone(analysis.status)}>{analysis.status}</Badge>
          {analysis.root_cause_kind && (
            <Badge tone="purple">{analysis.root_cause_kind}</Badge>
          )}
        </>
      }
    >
      {analysis.root_cause ? (
        <>
          <p className="text-[15px] leading-relaxed text-slate-100">
            {analysis.root_cause}
          </p>

          <dl className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <Field label="Confidence">
              <ConfidenceMeter value={analysis.confidence} level={analysis.confidence_level} />
            </Field>

            {analysis.anchor && (
              <Field label="Affected location">
                <span className="font-mono text-[12px]">
                  {analysis.anchor.file_path}
                  {analysis.anchor.line !== null && `:${analysis.anchor.line}`}
                  {analysis.anchor.function && (
                    <span className="text-slate-400"> in {analysis.anchor.function}()</span>
                  )}
                </span>
              </Field>
            )}

            <Field label="Corroborating investigators">
              <span className="font-mono text-[12px]">
                {analysis.corroborating_investigators.length
                  ? analysis.corroborating_investigators.join(", ")
                  : "—"}
              </span>
            </Field>
          </dl>

          {analysis.anchor?.excerpt && (
            <div className="mt-3">
              <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-slate-500">
                source excerpt (read by the analyser, not by us)
              </p>
              <Code>{analysis.anchor.excerpt}</Code>
            </div>
          )}

          {analysis.confidence_rationale && (
            <p className="mt-3 text-[13px] leading-relaxed text-slate-400">
              {analysis.confidence_rationale}
            </p>
          )}
        </>
      ) : (
        <Honest>
          The analyser returned <strong>{analysis.status}</strong> and no root cause. That is
          an answer, not a failure to answer — the backend refuses to name a defect it
          cannot support from stored evidence.
        </Honest>
      )}

      {/* ── Why the confidence is what it is ─────────────────────────────── */}
      {breakdown && (
        <div className="mt-5 rounded-lg border border-slate-800 bg-slate-950/50 p-3">
          <p className="mb-2 font-mono text-[10px] uppercase tracking-wider text-slate-500">
            confidence breakdown — the score is auditable, not a probability
          </p>
          <dl className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Base">{breakdown.base.toFixed(2)}</Field>
            <Field label="Corroboration">
              +{breakdown.corroboration_bonus.toFixed(2)}
            </Field>
            <Field label="Independent investigators">
              {breakdown.independent_investigators}
            </Field>
            <Field label="Final score">
              <span className="font-mono">{breakdown.score.toFixed(2)}</span>{" "}
              <Badge tone={statusTone(breakdown.level)}>{breakdown.level}</Badge>
            </Field>
          </dl>
          {breakdown.rationale && (
            <p className="mt-2 text-[12px] leading-relaxed text-slate-400">
              {breakdown.rationale}
            </p>
          )}
          {penalties.length > 0 && (
            <ul className="mt-2 space-y-0.5">
              {penalties.map(([name, value]) => (
                <li key={name} className="font-mono text-[11px] text-amber-300/80">
                  penalty · {name} {value.toFixed(2)}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* ── Evidence chain ───────────────────────────────────────────────── */}
      {analysis.evidence_chain.length > 0 && (
        <div className="mt-5">
          <p className="mb-2 font-mono text-[10px] uppercase tracking-wider text-slate-500">
            evidence chain — reported path to defect site
          </p>
          <ol className="space-y-1.5">
            {analysis.evidence_chain.map((link, i) => (
              <li
                key={i}
                className="flex gap-3 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2"
              >
                <span className="w-5 shrink-0 text-right font-mono text-[11px] text-slate-600">
                  {i + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone="info">{link.kind}</Badge>
                    <span className="text-[13px] text-slate-200">{link.label}</span>
                  </div>
                  {link.detail && (
                    <p className="mt-0.5 text-[12px] text-slate-400">{link.detail}</p>
                  )}
                  {link.anchor && (
                    <p className="mt-0.5 font-mono text-[10px] text-slate-500">
                      {link.anchor.file_path}
                      {link.anchor.line !== null && `:${link.anchor.line}`}
                    </p>
                  )}
                  {link.evidence_ids.length > 0 && (
                    <p className="mt-0.5 font-mono text-[10px] text-slate-600">
                      cites {link.evidence_ids.length} evidence item(s)
                    </p>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </div>
      )}

      {/* ── Supporting evidence ──────────────────────────────────────────── */}
      {analysis.supporting_evidence.length > 0 && (
        <div className="mt-5">
          <p className="mb-2 font-mono text-[10px] uppercase tracking-wider text-slate-500">
            supporting evidence — pointers into the evidence store
          </p>
          <ul className="space-y-1">
            {analysis.supporting_evidence.map((ref) => (
              <li
                key={ref.evidence_id}
                className="flex flex-wrap items-center gap-2 rounded border border-slate-800 bg-slate-950/40 px-2.5 py-1.5"
              >
                <Badge tone="neutral">{ref.evidence_type}</Badge>
                <span className="min-w-0 flex-1 text-[12px] text-slate-300">
                  {ref.title}
                </span>
                {ref.source_file && (
                  <span className="font-mono text-[10px] text-slate-500">
                    {ref.source_file}
                    {ref.source_line !== null && `:${ref.source_line}`}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ── Candidates that were rejected ────────────────────────────────── */}
      {analysis.rejected_candidates.length > 0 && (
        <div className="mt-5">
          <p className="mb-2 font-mono text-[10px] uppercase tracking-wider text-slate-500">
            rejected candidates
          </p>
          <ul className="space-y-1.5">
            {analysis.rejected_candidates.map((candidate, i) => (
              <li
                key={i}
                className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="neutral">{candidate.kind}</Badge>
                  <span className="text-[12px] text-slate-300">{candidate.summary}</span>
                  <span className="font-mono text-[10px] text-slate-600">
                    support {candidate.support_score.toFixed(2)}
                  </span>
                </div>
                <p className="mt-1 text-[12px] text-slate-500">{candidate.rationale}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ── Contributing factors, conflicts, open questions ───────────────── */}
      {analysis.contributing_factors.length > 0 && (
        <div className="mt-5">
          <p className="mb-2 font-mono text-[10px] uppercase tracking-wider text-slate-500">
            contributing factors
          </p>
          <ul className="space-y-1">
            {analysis.contributing_factors.map((factor, i) => (
              <li key={i} className="text-[12px] text-slate-300">
                <Badge tone="warn">{factor.category}</Badge>{" "}
                <span className="ml-1">{factor.description}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {analysis.conflicting_evidence.length > 0 && (
        <div className="mt-5 space-y-2">
          <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
            conflicting evidence — surfaced, not averaged away
          </p>
          {analysis.conflicting_evidence.map((conflict, i) => (
            <Honest key={i}>{conflict.description}</Honest>
          ))}
        </div>
      )}

      {analysis.unresolved_questions.length > 0 && (
        <div className="mt-5 space-y-2">
          <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
            unresolved questions — what static evidence could not answer
          </p>
          {analysis.unresolved_questions.map((question, i) => (
            <div
              key={i}
              className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2"
            >
              <p className="text-[13px] text-slate-200">{question.question}</p>
              <p className="mt-1 text-[12px] text-slate-400">
                <span className="text-slate-500">why it matters:</span>{" "}
                {question.why_it_matters}
              </p>
              <p className="mt-0.5 text-[12px] text-slate-400">
                <span className="text-slate-500">suggested step:</span>{" "}
                {question.recommended_step}
              </p>
            </div>
          ))}
        </div>
      )}

      <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-slate-800 pt-3 font-mono text-[10px] text-slate-600">
        <Badge tone="neutral">tests_executed: {String(analysis.tests_executed)}</Badge>
        <Badge tone="neutral">verified_result_count: {analysis.verified_result_count}</Badge>
        <Badge tone="neutral">
          evidence {analysis.raw_evidence_count} raw → {analysis.deduplicated_evidence_count} deduped
        </Badge>
        {analysis.partial_investigation && <Badge tone="warn">partial investigation</Badge>}
      </div>

      {analysis.missing_evidence.length > 0 && (
        <div className="mt-3 space-y-1">
          <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
            missing evidence
          </p>
          <ul className="space-y-0.5">
            {analysis.missing_evidence.map((item, i) => (
              <li key={i} className="text-[12px] text-amber-300/80">
                {item}
              </li>
            ))}
          </ul>
        </div>
      )}

      {analysis.recommended_next_investigation.length > 0 && (
        <div className="mt-3 space-y-1">
          <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
            recommended next investigation
          </p>
          <ul className="space-y-0.5">
            {analysis.recommended_next_investigation.map((item, i) => (
              <li key={i} className="text-[12px] text-slate-400">
                {item}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
