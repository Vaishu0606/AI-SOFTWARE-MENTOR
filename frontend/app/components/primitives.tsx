"use client";

/**
 * Small shared presentational primitives.
 *
 * Dark, dense, monospace-leaning: this is meant to read as an engineer's
 * instrumentation console rather than a marketing page. No animation beyond a
 * single progress bar transition.
 */

import type { ReactNode } from "react";

export function Card({
  title,
  subtitle,
  right,
  children,
  tone = "default",
  id,
}: {
  title?: string;
  subtitle?: string;
  right?: ReactNode;
  children: ReactNode;
  tone?: "default" | "accent" | "good" | "bad";
  id?: string;
}) {
  const toneRing = {
    default: "border-slate-800",
    accent: "border-sky-800/70",
    good: "border-emerald-800/70",
    bad: "border-rose-800/70",
  }[tone];

  return (
    <section
      id={id}
      className={`rounded-xl border ${toneRing} bg-slate-900/50 shadow-lg shadow-black/20`}
    >
      {(title || right) && (
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-800 px-4 py-3">
          <div className="min-w-0">
            {title && (
              <h2 className="text-sm font-semibold tracking-wide text-slate-100">
                {title}
              </h2>
            )}
            {subtitle && (
              <p className="mt-0.5 text-xs text-slate-400">{subtitle}</p>
            )}
          </div>
          {right && <div className="flex shrink-0 items-center gap-2">{right}</div>}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

const BADGE_TONES = {
  neutral: "bg-slate-800 text-slate-300 border-slate-700",
  info: "bg-sky-950 text-sky-300 border-sky-800",
  good: "bg-emerald-950 text-emerald-300 border-emerald-800",
  warn: "bg-amber-950 text-amber-300 border-amber-800",
  bad: "bg-rose-950 text-rose-300 border-rose-800",
  purple: "bg-violet-950 text-violet-300 border-violet-800",
} as const;

export type BadgeTone = keyof typeof BADGE_TONES;

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: ReactNode;
  tone?: BadgeTone;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-md border px-2 py-0.5 font-mono text-[11px] font-medium tracking-wide whitespace-nowrap ${BADGE_TONES[tone]}`}
    >
      {children}
    </span>
  );
}

/** Maps a backend status string to a badge tone. Unknown values stay neutral. */
export function statusTone(status: string): BadgeTone {
  switch (status) {
    case "COMPLETED":
    case "PASSED":
    case "APPLIED":
    case "DETERMINED":
    case "READY_FOR_REVIEW":
    case "HIGH":
      return "good";
    case "RUNNING":
    case "PENDING":
    case "PARTIAL":
    case "MEDIUM":
    case "IN_PROGRESS":
      return "info";
    case "FAILED":
    case "BLOCKED":
    case "TIMED_OUT":
    case "VERIFICATION_FAILED":
    case "REJECTED_STALE":
    case "REJECTED_UNSAFE":
    case "REJECTED_MISMATCH":
    case "FAILED_ROLLED_BACK":
    case "INSUFFICIENT_EVIDENCE":
    case "INVALID":
      return "bad";
    case "CANCELLED":
    case "NOT_RUN":
    case "NOT_APPLIED":
    case "ALREADY_APPLIED":
    case "UNRESOLVED":
    case "LOW":
      return "warn";
    default:
      return "neutral";
  }
}

export function Mono({ children }: { children: ReactNode }) {
  return <span className="font-mono text-[12px] text-slate-200">{children}</span>;
}

export function Code({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <pre
      className={`overflow-x-auto rounded-lg border border-slate-800 bg-slate-950/80 p-3 font-mono text-[12px] leading-relaxed text-slate-200 ${className}`}
    >
      {children}
    </pre>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] font-medium uppercase tracking-wider text-slate-500">
        {label}
      </dt>
      <dd className="mt-0.5 break-words text-[13px] text-slate-200">{children}</dd>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-lg border border-dashed border-slate-800 bg-slate-950/40 px-3 py-4 text-center text-[13px] text-slate-500">
      {children}
    </p>
  );
}

/** A one-line reason, used wherever the backend declines to conclude. */
export function Honest({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-lg border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-[12px] leading-relaxed text-amber-200/90">
      {children}
    </p>
  );
}

export function ProgressBar({ value }: { value: number }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div
      className="h-1.5 w-full overflow-hidden rounded-full bg-slate-800"
      role="progressbar"
      aria-valuenow={pct}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="h-full rounded-full bg-sky-500 transition-[width] duration-500"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

export function ConfidenceMeter({
  value,
  level,
}: {
  value: number;
  level: string;
}) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-slate-800">
        <div
          className="h-full rounded-full bg-violet-500"
          style={{ width: `${pct}%` }}
        />
      </div>
      <Mono>
        {value.toFixed(2)} · {level}
      </Mono>
    </div>
  );
}

/** Formats an ISO timestamp as local wall-clock time, tolerating bad input. */
export function timestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
