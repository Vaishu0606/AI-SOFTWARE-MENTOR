"use client";

/**
 * Unified-diff and before/after rendering.
 *
 * The diff text is the backend's own `unified_diff` string, produced by
 * Phase 9 and shown to a reviewer before anything is written. This file only
 * colourises it — it never regenerates, edits or applies it.
 */

import type { FixChange } from "@/app/lib/types";
import { Badge } from "./primitives";

type DiffLine = { kind: "add" | "del" | "ctx" | "meta" | "hunk"; text: string; oldNo?: number; newNo?: number };

/** Minimal unified-diff parser: enough for one file, no dependencies. */
function parseUnifiedDiff(diff: string): DiffLine[] {
  const out: DiffLine[] = [];
  let oldNo: number | undefined;
  let newNo: number | undefined;

  for (const raw of diff.split("\n")) {
    if (raw.startsWith("--- ") || raw.startsWith("+++ ")) {
      out.push({ kind: "meta", text: raw });
      continue;
    }
    if (raw.startsWith("@@")) {
      const m = /@@\s*-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s*@@/.exec(raw);
      oldNo = m ? Number(m[1]) : undefined;
      newNo = m ? Number(m[2]) : undefined;
      out.push({ kind: "hunk", text: raw });
      continue;
    }
    if (raw.startsWith("+")) {
      out.push({ kind: "add", text: raw.slice(1), newNo });
      if (newNo !== undefined) newNo += 1;
      continue;
    }
    if (raw.startsWith("-")) {
      out.push({ kind: "del", text: raw.slice(1), oldNo });
      if (oldNo !== undefined) oldNo += 1;
      continue;
    }
    if (raw.startsWith(" ")) {
      out.push({ kind: "ctx", text: raw.slice(1), oldNo, newNo });
      if (oldNo !== undefined) oldNo += 1;
      if (newNo !== undefined) newNo += 1;
      continue;
    }
    out.push({ kind: "ctx", text: raw });
  }

  return out;
}

const LINE_STYLE: Record<DiffLine["kind"], string> = {
  add: "bg-emerald-950/70 text-emerald-200",
  del: "bg-rose-950/70 text-rose-200",
  ctx: "text-slate-400",
  meta: "text-slate-500 bg-slate-950",
  hunk: "text-sky-400 bg-sky-950/40",
};

const SIGN: Record<DiffLine["kind"], string> = {
  add: "+",
  del: "-",
  ctx: " ",
  meta: "",
  hunk: "",
};

export function UnifiedDiff({ diff }: { diff: string }) {
  const lines = parseUnifiedDiff(diff);

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800 bg-slate-950">
      <table className="w-full border-collapse font-mono text-[12px] leading-relaxed">
        <tbody>
          {lines.map((line, i) => (
            <tr key={i} className={LINE_STYLE[line.kind]}>
              <td className="w-10 select-none border-r border-slate-800/70 px-1.5 text-right align-top text-[10px] text-slate-600">
                {line.oldNo ?? ""}
              </td>
              <td className="w-10 select-none border-r border-slate-800/70 px-1.5 text-right align-top text-[10px] text-slate-600">
                {line.newNo ?? ""}
              </td>
              <td className="w-4 select-none px-1 align-top text-center">
                {SIGN[line.kind]}
              </td>
              <td className="whitespace-pre-wrap break-all px-2 align-top">
                {line.text}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The same change expressed as exact before/after, for a line-level review. */
export function ChangeReview({ change }: { change: FixChange }) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="info">{change.file_path}:{change.line}</Badge>
        {change.function && <Badge>{change.function}</Badge>}
        <Badge tone="neutral">{change.change_type}</Badge>
        {change.line_end !== null && (
          <Badge tone="neutral">
            lines {change.line}–{change.line_end}
          </Badge>
        )}
      </div>

      {change.description && (
        <p className="text-[13px] text-slate-300">{change.description}</p>
      )}

      <div className="grid gap-2 md:grid-cols-2">
        <div className="overflow-hidden rounded-lg border border-rose-900/60">
          <div className="border-b border-rose-900/60 bg-rose-950/50 px-2.5 py-1 font-mono text-[10px] font-semibold uppercase tracking-wider text-rose-300">
            Before
          </div>
          <pre className="overflow-x-auto bg-rose-950/20 p-2.5 font-mono text-[12px] leading-relaxed whitespace-pre-wrap text-rose-100">
            {change.before}
          </pre>
        </div>

        <div className="overflow-hidden rounded-lg border border-emerald-900/60">
          <div className="border-b border-emerald-900/60 bg-emerald-950/50 px-2.5 py-1 font-mono text-[10px] font-semibold uppercase tracking-wider text-emerald-300">
            After
          </div>
          <pre className="overflow-x-auto bg-emerald-950/20 p-2.5 font-mono text-[12px] leading-relaxed whitespace-pre-wrap text-emerald-100">
            {change.after}
          </pre>
        </div>
      </div>
    </div>
  );
}
