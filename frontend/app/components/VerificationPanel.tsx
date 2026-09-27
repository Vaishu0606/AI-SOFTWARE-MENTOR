"use client";

/**
 * Verification, application and test-plan panels.
 *
 * The four verification booleans are four different claims and are rendered as
 * four separate rows, never as one verdict. `defect_demonstrated` in
 * particular is false on the demo repository, and the panel says so plainly
 * rather than hiding it behind a green "Verified" banner — a pass here means
 * "no regression", which is not the same as "the bug is fixed".
 */

import type {
  FixApplicationResponse,
  FixApproval,
  TestPlanResponse,
  VerificationResult,
} from "@/app/lib/types";
import { Badge, Card, Code, Empty, Field, Honest, statusTone } from "./primitives";

function ClaimRow({ label, value, note }: { label: string; value: boolean; note: string }) {
  return (
    <li className="flex flex-wrap items-center gap-2 rounded border border-slate-800 bg-slate-950/40 px-3 py-2">
      <Badge tone={value ? "good" : "warn"}>{String(value)}</Badge>
      <span className="font-mono text-[12px] text-slate-200">{label}</span>
      <span className="min-w-0 flex-1 text-[12px] text-slate-500">{note}</span>
    </li>
  );
}

export function VerificationPanel({ verification }: { verification: VerificationResult }) {
  const run = verification.test_run;
  // exit_code is null when the runner never started, which is not the same as
  // any particular number, so the badge colour is chosen explicitly.
  const exitTone =
    run.exit_code === null ? "neutral" : run.exit_code === 0 ? "good" : "bad";

  return (
    <Card
      title="Verification"
      subtitle="GET /api/mentor/runs/{run_id}/verification"
      tone={
        verification.status === "PASSED"
          ? "good"
          : verification.status === "FAILED" || verification.status === "TIMED_OUT"
            ? "bad"
            : "default"
      }
      right={
        <>
          <Badge tone={statusTone(verification.status)}>{verification.status}</Badge>
          <Badge tone={exitTone}>exit {String(run.exit_code)}</Badge>
        </>
      }
    >
      {/* ── The four separate claims ─────────────────────────────────────── */}
      <ul className="space-y-1.5">
        <ClaimRow
          label="tests_executed"
          value={verification.tests_executed}
          note="A test runner actually started and returned an exit code."
        />
        <ClaimRow
          label="regression_check"
          value={verification.regression_check}
          note="The repository's own suite passed against the applied change. This is a claim about the absence of regression."
        />
        <ClaimRow
          label="defect_demonstrated"
          value={verification.defect_demonstrated}
          note="A test that discriminates the diagnosed behaviour from correct behaviour was executed and passed."
        />
        <ClaimRow
          label="fix_applied"
          value={verification.fix_applied}
          note="The repository currently holds the approved change."
        />
      </ul>

      {!verification.defect_demonstrated && verification.tests_executed && (
        <Honest>
          <strong>No defect-demonstrating test was executed.</strong> The suite passed
          both with and without the change, so it cannot tell the broken behaviour apart
          from correct behaviour. This run establishes that the fix introduced no
          regression — not, on its own, that it fixed the bug. That is the backend&apos;s
          own finding, reported here rather than smoothed over.
        </Honest>
      )}

      {/* ── What was actually run ────────────────────────────────────────── */}
      <div className="mt-4">
        <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wider text-slate-500">
          executed test run
        </p>
        <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Runner started">
            <Badge tone={run.started ? "good" : "bad"}>{String(run.started)}</Badge>
          </Field>
          <Field label="Exit code">
            <span
              className={`font-mono ${
                run.exit_code === 0 ? "text-emerald-300" : "text-rose-300"
              }`}
            >
              {String(run.exit_code)}
            </span>
          </Field>
          <Field label="Duration">{run.duration_seconds.toFixed(2)}s</Field>
          <Field label="Timed out">
            <Badge tone={run.timed_out ? "bad" : "neutral"}>{String(run.timed_out)}</Badge>
          </Field>
          <Field label="Tests run">{String(run.tests_run)}</Field>
          <Field label="Passed">
            <span className="text-emerald-300">{String(run.passed)}</span>
          </Field>
          <Field label="Failed">
            <span className={run.failed ? "text-rose-300" : "text-slate-300"}>
              {String(run.failed)}
            </span>
          </Field>
          <Field label="Targets requested">{run.tests_requested}</Field>
        </dl>

        {run.targets.length > 0 && (
          <div className="mt-3">
            <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-slate-500">
              targets
            </p>
            <ul className="space-y-0.5">
              {run.targets.map((target) => (
                <li key={target} className="font-mono text-[12px] text-slate-300">
                  {target}
                </li>
              ))}
            </ul>
          </div>
        )}

        {run.command.length > 0 && (
          <div className="mt-3">
            <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-slate-500">
              command — built by the server, shell-free, never taken from a request
            </p>
            <Code>{run.command.join(" ")}</Code>
          </div>
        )}

        {run.stdout_excerpt && (
          <div className="mt-3">
            <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-slate-500">
              stdout{run.output_truncated ? " (truncated)" : ""}
            </p>
            <Code className="whitespace-pre-wrap text-slate-300">{run.stdout_excerpt}</Code>
          </div>
        )}

        {run.stderr_excerpt && (
          <div className="mt-3">
            <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-slate-500">
              stderr
            </p>
            <Code className="whitespace-pre-wrap text-rose-300">{run.stderr_excerpt}</Code>
          </div>
        )}
      </div>

      {/* ── Findings and limitations ─────────────────────────────────────── */}
      {verification.findings.length > 0 && (
        <div className="mt-4">
          <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wider text-slate-500">
            findings
          </p>
          <ul className="space-y-1.5">
            {verification.findings.map((finding, i) => (
              <li
                key={i}
                className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-[13px] leading-relaxed text-slate-300"
              >
                {finding}
              </li>
            ))}
          </ul>
        </div>
      )}

      {verification.limitations.length > 0 && (
        <div className="mt-4 space-y-1.5">
          <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
            limitations — what this result does not establish
          </p>
          {verification.limitations.map((limitation, i) => (
            <Honest key={i}>{limitation}</Honest>
          ))}
        </div>
      )}

      {Object.keys(verification.verified_file_hashes ?? {}).length > 0 && (
        <p className="mt-4 break-all font-mono text-[10px] text-slate-600">
          verified file hashes{" "}
          {Object.entries(verification.verified_file_hashes).map(([file, hash]) => (
            <span key={file} className="ml-2">
              {file}={hash.slice(0, 16)}…
            </span>
          ))}
        </p>
      )}
    </Card>
  );
}

export function ApplicationPanel({
  approval,
  application,
}: {
  approval: FixApproval;
  application: FixApplicationResponse;
}) {
  return (
    <Card
      title="Applied fix"
      subtitle="GET /api/mentor/runs/{run_id}/fix-application"
      tone={application.repository_modified ? "good" : "default"}
      right={
        <>
          <Badge tone={statusTone(application.status)}>{application.status}</Badge>
          <Badge tone={application.repository_modified ? "good" : "neutral"}>
            repository_modified: {String(application.repository_modified)}
          </Badge>
        </>
      }
    >
      <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Approved by">
          <span className="font-mono text-[12px]">{approval.approved_by}</span>
        </Field>
        <Field label="Approved fingerprint">
          <span className="break-all font-mono text-[11px] text-sky-300">
            {approval.proposal_fingerprint}
          </span>
        </Field>
        <Field label="Post-image confirmed">
          <Badge tone={application.verified ? "good" : "bad"}>
            verified: {String(application.verified)}
          </Badge>
        </Field>
        <Field label="Lines applied">{application.total_lines_applied}</Field>
      </dl>

      {approval.comment && (
        <div className="mt-3">
          <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-slate-500">
            reviewer comment
          </p>
          <p className="text-[13px] italic text-slate-300">“{approval.comment}”</p>
        </div>
      )}

      {application.files.length > 0 && (
        <div className="mt-4">
          <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wider text-slate-500">
            files written — digests of the bytes on disk, not of the proposal
          </p>
          <ul className="space-y-1.5">
            {application.files.map((file) => (
              <li
                key={file.file_path}
                className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[12px] text-slate-200">
                    {file.file_path}
                  </span>
                  <Badge tone={file.modified ? "good" : "neutral"}>
                    {file.lines_applied} line(s)
                  </Badge>
                </div>
                <p className="mt-1 break-all font-mono text-[10px] text-slate-500">
                  before {file.before_sha256}
                  <br />
                  after&nbsp; {file.after_sha256}
                </p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {application.reason && (
        <p className="mt-3 font-mono text-[11px] text-amber-300/80">{application.reason}</p>
      )}

      {application.rolled_back && (
        <div className="mt-3">
          <Honest>
            The application failed and was rolled back: every file the applier had written
            was restored to its pre-image, and the digests above are the restored ones.
          </Honest>
        </div>
      )}

      {application.limitations.length > 0 && (
        <div className="mt-3 space-y-1">
          {application.limitations.map((limitation, i) => (
            <p key={i} className="font-mono text-[10px] text-slate-600">
              · {limitation}
            </p>
          ))}
        </div>
      )}
    </Card>
  );
}

export function TestPlanPanel({ plan }: { plan: TestPlanResponse }) {
  return (
    <Card
      title="Regression test plan"
      subtitle="GET /api/mentor/runs/{run_id}/test-plan · outlined in prose, no test source written"
      tone="accent"
      right={
        <>
          <Badge tone={statusTone(plan.status)}>{plan.status}</Badge>
          <Badge tone="neutral">execution: {plan.execution_status}</Badge>
        </>
      }
    >
      <div className="mb-3 flex flex-wrap items-center gap-2 font-mono text-[10px]">
        <Badge tone="neutral">tests_executed: {String(plan.tests_executed)}</Badge>
        <Badge tone="neutral">test_source_written: {String(plan.test_source_written)}</Badge>
        <Badge tone="neutral">{plan.scenarios.length} scenario(s)</Badge>
        <Badge tone="neutral">{plan.coverage_gaps.length} coverage gap(s)</Badge>
      </div>

      <p className="mb-4 text-[13px] leading-relaxed text-slate-300">{plan.rationale}</p>

      {plan.scenarios.length === 0 ? (
        <Empty>This plan is a documented decline — no scenarios were proposed.</Empty>
      ) : (
        <ol className="space-y-3">
          {plan.scenarios.map((scenario) => (
            <li
              key={scenario.id}
              className="rounded-lg border border-slate-800 bg-slate-950/40 p-3"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13px] font-medium text-slate-100">
                  {scenario.name}
                </span>
                <Badge tone="info">{scenario.suggested_file}</Badge>
                <Badge tone="neutral">{scenario.file_status}</Badge>
                {scenario.target_function && <Badge>{scenario.target_function}()</Badge>}
              </div>

              <p className="mt-1.5 text-[13px] leading-relaxed text-slate-300">
                {scenario.purpose}
              </p>

              {scenario.preconditions.length > 0 && (
                <div className="mt-2">
                  <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
                    preconditions
                  </p>
                  <ul className="mt-0.5 space-y-0.5">
                    {scenario.preconditions.map((item, i) => (
                      <li key={i} className="text-[12px] text-slate-400">
                        · {item}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {scenario.steps.length > 0 && (
                <div className="mt-2">
                  <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
                    steps
                  </p>
                  <ol className="mt-0.5 list-inside list-decimal space-y-0.5">
                    {scenario.steps.map((step, i) => (
                      <li key={i} className="text-[12px] text-slate-400">
                        {step}
                      </li>
                    ))}
                  </ol>
                </div>
              )}

              {scenario.expected_behavior && (
                <p className="mt-2 text-[12px] text-slate-300">
                  <span className="text-slate-500">expected behaviour (invariant):</span>{" "}
                  {scenario.expected_behavior}
                </p>
              )}

              {scenario.discriminating_condition && (
                <p className="mt-1 text-[12px] text-slate-300">
                  <span className="text-slate-500">discriminating condition:</span>{" "}
                  {scenario.discriminating_condition}
                </p>
              )}

              {scenario.covers_gaps.length > 0 && (
                <p className="mt-1 font-mono text-[10px] text-slate-600">
                  closes gap(s): {scenario.covers_gaps.join(", ")}
                </p>
              )}
            </li>
          ))}
        </ol>
      )}

      {plan.coverage_gaps.length > 0 && (
        <div className="mt-5">
          <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wider text-slate-500">
            coverage gaps recorded — including any no scenario closes
          </p>
          <ul className="space-y-1.5">
            {plan.coverage_gaps.map((gap) => (
              <li
                key={gap.id}
                className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="warn">{gap.kind}</Badge>
                  <span className="text-[12px] text-slate-200">{gap.title}</span>
                </div>
                <p className="mt-0.5 text-[12px] text-slate-500">{gap.rationale}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {plan.limitations.length > 0 && (
        <div className="mt-4 space-y-1">
          {plan.limitations.map((limitation, i) => (
            <Honest key={i}>{limitation}</Honest>
          ))}
        </div>
      )}
    </Card>
  );
}
