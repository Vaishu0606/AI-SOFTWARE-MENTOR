"use client";

/**
 * The ten-stage pipeline, plus the live workflow event log.
 *
 * Stage status comes exclusively from the backend's own events (see
 * `lib/stages.ts`). The event log is the raw, unedited stream, so an evaluator
 * can check any claim this UI makes against what the server actually sent.
 */

import { useState } from "react";
import type { WorkflowEvent } from "@/app/lib/types";
import { deriveStages, pipelineProgress, type StageStatus } from "@/app/lib/stages";
import { Badge, Card, Empty, ProgressBar, timestamp } from "./primitives";

const STAGE_STYLE: Record<
  StageStatus,
  { dot: string; text: string; label: string; ring: string }
> = {
  completed: {
    dot: "bg-emerald-500",
    text: "text-emerald-300",
    label: "completed",
    ring: "border-emerald-800/70 bg-emerald-950/30",
  },
  running: {
    dot: "bg-sky-400",
    text: "text-sky-300",
    label: "running",
    ring: "border-sky-800/70 bg-sky-950/30",
  },
  failed: {
    dot: "bg-rose-500",
    text: "text-rose-300",
    label: "failed",
    ring: "border-rose-800/70 bg-rose-950/30",
  },
  pending: {
    dot: "bg-slate-700",
    text: "text-slate-500",
    label: "pending",
    ring: "border-slate-800 bg-slate-900/30",
  },
};

export function StageTimeline({ events }: { events: WorkflowEvent[] }) {
  const stages = deriveStages(events);
  const progress = pipelineProgress(stages);
  const completed = stages.filter((s) => s.status === "completed").length;

  return (
    <Card
      title="Investigation pipeline"
      subtitle={`${completed} of ${stages.length} stages completed · derived from ${events.length} workflow events`}
      right={<Badge tone="info">{Math.round(progress * 100)}%</Badge>}
    >
      <div className="mb-4">
        <ProgressBar value={progress} />
      </div>

      <ol className="space-y-1.5">
        {stages.map((stage, index) => {
          const style = STAGE_STYLE[stage.status];
          return (
            <li
              key={stage.def.id}
              className={`flex items-start gap-3 rounded-lg border px-3 py-2 ${style.ring}`}
            >
              <div className="flex flex-col items-center pt-0.5">
                <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${style.dot}`} />
                {index < stages.length - 1 && (
                  <span className="mt-1 h-4 w-px bg-slate-800" />
                )}
              </div>

              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="text-[13px] font-medium text-slate-100">
                    {index + 1}. {stage.def.label}
                  </span>
                  <span
                    className={`font-mono text-[10px] uppercase tracking-wider ${style.text}`}
                  >
                    {style.label}
                  </span>
                </div>

                {stage.summary && (
                  <p className="mt-0.5 font-mono text-[11px] text-slate-400">
                    {stage.summary}
                  </p>
                )}

                {stage.event && (
                  <p className="mt-0.5 truncate font-mono text-[10px] text-slate-600">
                    seq {stage.event.sequence} · {stage.event.event_type} ·{" "}
                    {timestamp(stage.event.timestamp)}
                  </p>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}

const FAILURE_TYPES = new Set([
  "STAGE_FAILED",
  "RUN_FAILED",
  "INVESTIGATOR_FAILED",
  "ROOT_CAUSE_ANALYSIS_FAILED",
  "FIX_PROPOSAL_FAILED",
  "TEST_PLAN_FAILED",
  "FIX_APPLICATION_FAILED",
  "VERIFICATION_FAILED",
]);

export function EventLog({ events }: { events: WorkflowEvent[] }) {
  const [showAll, setShowAll] = useState(false);
  const [filter, setFilter] = useState<"all" | "failures">("all");

  const visible = filter === "failures" ? events.filter((e) => FAILURE_TYPES.has(e.event_type)) : events;
  const shown = showAll ? visible : visible.slice(-40);

  return (
    <Card
      title="Workflow event log"
      subtitle="Raw, unedited events from GET /api/mentor/runs/{run_id}/events"
      right={
        <>
          <button
            type="button"
            onClick={() => setFilter((f) => (f === "all" ? "failures" : "all"))}
            className={`rounded-md border px-2 py-0.5 font-mono text-[11px] ${
              filter === "failures"
                ? "border-rose-800 bg-rose-950 text-rose-300"
                : "border-slate-700 bg-slate-800 text-slate-300"
            }`}
          >
            {filter === "failures" ? "showing failures" : "showing all"}
          </button>
          <Badge tone="neutral">{events.length} events</Badge>
        </>
      }
    >
      {visible.length === 0 ? (
        <Empty>
          {filter === "failures"
            ? "No failure events in this run."
            : "No events yet. The stream connects as soon as a run exists."}
        </Empty>
      ) : (
        <>
          <ol className="max-h-96 space-y-0.5 overflow-y-auto pr-1">
            {shown.map((event) => {
              const failed = FAILURE_TYPES.has(event.event_type);
              return (
                <li
                  key={event.event_id}
                  className={`flex gap-2 rounded px-2 py-1 font-mono text-[11px] leading-relaxed ${
                    failed ? "bg-rose-950/40" : "hover:bg-slate-800/40"
                  }`}
                >
                  <span className="w-8 shrink-0 text-right text-slate-600">
                    {event.sequence}
                  </span>
                  <span
                    className={`w-52 shrink-0 truncate ${
                      failed ? "text-rose-300" : "text-sky-300"
                    }`}
                  >
                    {event.event_type}
                  </span>
                  <span className="w-16 shrink-0 text-right text-slate-600">
                    {timestamp(event.timestamp)}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-slate-400">
                    {event.message}
                  </span>
                </li>
              );
            })}
          </ol>

          {visible.length > shown.length && (
            <button
              type="button"
              onClick={() => setShowAll(true)}
              className="mt-2 w-full rounded-md border border-slate-700 py-1 font-mono text-[11px] text-slate-400 hover:bg-slate-800"
            >
              show all {visible.length} events
            </button>
          )}
        </>
      )}
    </Card>
  );
}
