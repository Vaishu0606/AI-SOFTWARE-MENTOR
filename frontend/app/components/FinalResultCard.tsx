"use client";

/**
 * The final result card, and the run header.
 *
 * The verdict text is derived from the backend's own verification status. There
 * is no code path that renders "Verified" unless the backend reported
 * `PASSED` with `regression_check` and `fix_applied` true — a run that failed
 * verification, or that was never verified, says so instead.
 */

import type { MentorRunResponse, VerificationResult } from "@/app/lib/types";
import { Badge, timestamp, statusTone } from "./primitives";

const STEPS = [
  "Issue",
  "Evidence",
  "Root Cause",
  "Reviewable Fix",
  "Human Approval",
  "Applied Fix",
  "Tests",
  "Verified Result",
];

export function FinalResultCard({
  run,
  verification,
}: {
  run: MentorRunResponse;
  verification: VerificationResult | null;
}) {
  if (!verification) {
    return (
      <section className="rounded-xl border border-slate-800 bg-slate-900/50 p-6">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400">
          Result
        </h2>
        <p className="mt-2 text-[15px] text-slate-300">
          This run is at{" "}
          <span className="font-mono text-sky-300">{run.current_state}</span> and has not
          been verified. No fix has been written to the repository.
        </p>
      </section>
    );
  }

  const v = verification;
  const passed = v.status === "PASSED";
  const fullyVerified = passed && v.regression_check && v.fix_applied;

  const claims: Array<{ label: string; ok: boolean }> = [
    { label: "Tests executed", ok: v.tests_executed },
    { label: "Regression check", ok: v.regression_check },
    { label: "Fix applied", ok: v.fix_applied },
    { label: "Defect demonstrated", ok: v.defect_demonstrated },
  ];

  return (
    <section
      className={`overflow-hidden rounded-xl border ${
        fullyVerified
          ? "border-emerald-700/70 bg-emerald-950/20"
          : passed
            ? "border-amber-700/70 bg-amber-950/20"
            : "border-rose-800/70 bg-rose-950/20"
      }`}
    >
      <div className="p-6">
        <div className="flex flex-wrap items-center gap-3">
          <h2
            className={`text-2xl font-bold tracking-tight ${
              fullyVerified
                ? "text-emerald-300"
                : passed
                  ? "text-amber-300"
                  : "text-rose-300"
            }`}
          >
            {fullyVerified
              ? "Verified"
              : passed
                ? "Regression Check Passed"
                : v.status === "NOT_RUN"
                  ? "Not Verified"
                  : `Verification ${v.status}`}
          </h2>
          <Badge tone={statusTone(v.status)}>{v.status}</Badge>
          <Badge tone={v.test_run.exit_code === 0 ? "good" : "bad"}>
            exit {String(v.test_run.exit_code)}
          </Badge>
        </div>

        <p className="mt-2 max-w-3xl text-[14px] leading-relaxed text-slate-300">
          {fullyVerified
            ? "The change was written to the repository, the post-image was re-read and confirmed, and the repository's own test suite exited 0 against the applied change."
            : passed
              ? "The repository's own suite passed against the applied change, so the change introduced no regression. Note the claims below — a pass here is not by itself a demonstration that the diagnosed defect is fixed."
              : v.status === "NOT_RUN"
                ? "No test was executed for this run, so nothing about the repository has been verified."
                : `The controlled test run did not establish a pass: status ${v.status}, exit code ${String(v.test_run.exit_code)}${v.test_run.timed_out ? ", stopped at the timeout" : ""}.`}
        </p>

        {/* The chain the demo is about. */}
        <ol className="mt-5 flex flex-wrap items-center gap-x-1.5 gap-y-2">
          {STEPS.map((step, i) => {
            const isLast = i === STEPS.length - 1;
            const done = isLast ? fullyVerified : true; // earlier steps are shown as traversed
            return (
              <li key={step} className="flex items-center gap-1.5">
                <span
                  className={`rounded-md border px-2 py-1 font-mono text-[11px] ${
                    done
                      ? "border-slate-600 bg-slate-800/70 text-slate-200"
                      : "border-slate-800 bg-slate-900 text-slate-600"
                  }`}
                >
                  {step}
                </span>
                {!isLast && <span className="text-slate-700">→</span>}
              </li>
            );
          })}
        </ol>

        {/* The four claims, kept separate. */}
        <ul className="mt-5 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {claims.map((claim) => (
            <li
              key={claim.label}
              className="rounded-lg border border-slate-800 bg-slate-950/50 px-3 py-2"
            >
              <Badge tone={claim.ok ? "good" : "warn"}>{String(claim.ok)}</Badge>
              <p className="mt-1 font-mono text-[11px] text-slate-300">{claim.label}</p>
            </li>
          ))}
        </ul>

        {v.test_run.tests_run !== null && (
          <p className="mt-4 font-mono text-[12px] text-slate-400">
            {v.test_run.tests_run} test(s) run · {String(v.test_run.passed)} passed ·{" "}
            {String(v.test_run.failed)} failed ·{" "}
            {v.test_run.duration_seconds.toFixed(2)}s
          </p>
        )}

        {passed && !v.defect_demonstrated && (
          <p className="mt-4 rounded-lg border border-amber-800/60 bg-amber-950/30 px-3 py-2 text-[12px] leading-relaxed text-amber-200/90">
            The pre-existing suite also passes against the tree with this change reverted,
            so it does not discriminate the diagnosed behaviour from correct behaviour.
            The run establishes the <strong>absence of regression</strong>. Demonstrating
            the defect is fixed would need a test that fails before the change and passes
            after it — which this tool deliberately does not fabricate, because generated
            test source that nobody has run is not a regression test.
          </p>
        )}
      </div>
    </section>
  );
}

export function RunHeader({ run }: { run: MentorRunResponse }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div className="rounded-lg border border-slate-800 bg-slate-900/50 px-3 py-2">
        <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
          run id
        </p>
        <p className="mt-0.5 break-all font-mono text-[12px] text-sky-300">
          {run.run_id}
        </p>
      </div>

      <div className="rounded-lg border border-slate-800 bg-slate-900/50 px-3 py-2">
        <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
          workflow state
        </p>
        <p className="mt-0.5 flex items-center gap-2">
          <Badge tone={statusTone(run.current_state)}>{run.current_state}</Badge>
          <Badge tone={statusTone(run.status)}>{run.status}</Badge>
        </p>
      </div>

      <div className="rounded-lg border border-slate-800 bg-slate-900/50 px-3 py-2">
        <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
          repository
        </p>
        <p className="mt-0.5 break-all font-mono text-[11px] text-slate-300">
          {run.repository_path}
        </p>
      </div>

      <div className="rounded-lg border border-slate-800 bg-slate-900/50 px-3 py-2">
        <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
          events · created
        </p>
        <p className="mt-0.5 font-mono text-[11px] text-slate-300">
          {run.event_count} · {timestamp(run.created_at)}
        </p>
      </div>
    </div>
  );
}
