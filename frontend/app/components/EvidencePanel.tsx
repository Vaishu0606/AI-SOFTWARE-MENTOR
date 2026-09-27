"use client";

/**
 * Evidence panel.
 *
 * The backend's evidence type system is the point of this panel, so the four
 * types are separated and never merged: an OBSERVATION was read out of the
 * source, a HYPOTHESIS is an inference that may be wrong, a PROPOSED_ACTION is
 * awaiting review, and a VERIFIED_RESULT came from a real exit code. The UI
 * never relabels one as another, and never invents an item.
 */

import { useMemo, useState } from "react";
import type { Evidence, EvidenceType, InvestigationResponse } from "@/app/lib/types";
import { Badge, Card, Empty, Honest, timestamp, type BadgeTone } from "./primitives";

const TYPE_TONE: Record<EvidenceType, BadgeTone> = {
  OBSERVATION: "good",
  HYPOTHESIS: "warn",
  PROPOSED_ACTION: "info",
  VERIFIED_RESULT: "purple",
};

const TYPE_MEANING: Record<EvidenceType, string> = {
  OBSERVATION: "A fact read directly from the source, files or test output.",
  HYPOTHESIS: "A reasoned inference. It may be incorrect and is not promoted without execution.",
  PROPOSED_ACTION: "A suggested fix or check awaiting human review.",
  VERIFIED_RESULT: "Confirmed by an actual test execution with an exit code.",
};

function EvidenceRow({ item }: { item: Evidence }) {
  const [open, setOpen] = useState(false);
  const where =
    item.source_file !== null
      ? `${item.source_file}${item.source_line !== null ? `:${item.source_line}` : ""}`
      : null;

  return (
    <li className="rounded-lg border border-slate-800 bg-slate-950/40">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-slate-800/30"
      >
        <Badge tone={TYPE_TONE[item.evidence_type]} title={TYPE_MEANING[item.evidence_type]}>
          {item.evidence_type}
        </Badge>

        <span className="min-w-0 flex-1">
          <span className="block text-[13px] font-medium text-slate-100">
            {item.title}
          </span>
          <span className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[10px] text-slate-500">
            <span>{item.agent}</span>
            {where && <span className="text-slate-400">{where}</span>}
            {item.confidence !== null && (
              <span title="Confidence reported by the investigator">
                conf {item.confidence.toFixed(2)}
              </span>
            )}
            <span>{timestamp(item.timestamp)}</span>
          </span>
        </span>

        <span className="shrink-0 font-mono text-[10px] text-slate-600">
          {open ? "hide" : "detail"}
        </span>
      </button>

      {open && (
        <div className="space-y-2 border-t border-slate-800 px-3 py-2">
          <p className="text-[13px] leading-relaxed text-slate-300">
            {item.description}
          </p>

          {item.raw_content && (
            <pre className="overflow-x-auto rounded border border-slate-800 bg-slate-950 p-2 font-mono text-[11px] whitespace-pre-wrap text-slate-400">
              {item.raw_content}
            </pre>
          )}

          <p className="font-mono text-[10px] text-slate-600">id {item.id}</p>

          {Object.keys(item.metadata ?? {}).length > 0 && (
            <details>
              <summary className="cursor-pointer font-mono text-[10px] text-slate-500">
                metadata
              </summary>
              <pre className="mt-1 overflow-x-auto rounded border border-slate-800 bg-slate-950 p-2 font-mono text-[10px] text-slate-400">
                {JSON.stringify(item.metadata, null, 2)}
              </pre>
            </details>
          )}
        </div>
      )}
    </li>
  );
}

export function EvidencePanel({ investigation }: { investigation: InvestigationResponse }) {
  const [filter, setFilter] = useState<EvidenceType | "ALL">("ALL");

  const groups = useMemo(() => {
    const order: EvidenceType[] = ["OBSERVATION", "HYPOTHESIS", "PROPOSED_ACTION", "VERIFIED_RESULT"];
    const map = new Map<EvidenceType, Evidence[]>();
    for (const item of investigation.evidence) {
      const list = map.get(item.evidence_type) ?? [];
      list.push(item);
      map.set(item.evidence_type, list);
    }
    return order
      .map((type) => ({ type, items: map.get(type) ?? [] }))
      .filter((group) => group.items.length > 0);
  }, [investigation.evidence]);

  const visible = filter === "ALL" ? investigation.evidence : investigation.evidence.filter((e) => e.evidence_type === filter);
  const shown = visible.slice(0, 60);

  const totals = Object.entries(investigation.evidence_counts ?? {});

  return (
    <Card
      title="Evidence"
      subtitle={`${investigation.evidence.length} stored items · investigation ${investigation.status}`}
      right={
        <>
          {totals.map(([type, count]) => (
            <Badge key={type} tone={TYPE_TONE[type as EvidenceType] ?? "neutral"}>
              {type} {count}
            </Badge>
          ))}
        </>
      }
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {(["ALL", "OBSERVATION", "HYPOTHESIS", "PROPOSED_ACTION", "VERIFIED_RESULT"] as const).map(
          (type) => (
            <button
              key={type}
              type="button"
              onClick={() => setFilter(type)}
              className={`rounded-md border px-2 py-0.5 font-mono text-[11px] ${
                filter === type
                  ? "border-sky-700 bg-sky-950 text-sky-300"
                  : "border-slate-700 bg-slate-800 text-slate-400 hover:bg-slate-700"
              }`}
            >
              {type}
            </button>
          ),
        )}
      </div>

      {filter === "ALL" ? (
        <div className="space-y-4">
          {groups.length === 0 && <Empty>No evidence recorded for this run.</Empty>}
          {groups.map((group) => (
            <div key={group.type}>
              <div className="mb-1.5 flex items-center gap-2">
                <Badge tone={TYPE_TONE[group.type]}>{group.type}</Badge>
                <span className="text-[11px] text-slate-500">
                  {TYPE_MEANING[group.type]}
                </span>
              </div>
              <ul className="space-y-1.5">
                {group.items.slice(0, 8).map((item) => (
                  <EvidenceRow key={item.id} item={item} />
                ))}
              </ul>
              {group.items.length > 8 && (
                <p className="mt-1.5 pl-1 font-mono text-[10px] text-slate-600">
                  +{group.items.length - 8} more of this type — use the filter above to
                  see them
                </p>
              )}
            </div>
          ))}
        </div>
      ) : visible.length === 0 ? (
        <Empty>No {filter} items in this run.</Empty>
      ) : (
        <>
          <ul className="space-y-1.5">
            {shown.map((item) => (
              <EvidenceRow key={item.id} item={item} />
            ))}
          </ul>
          {visible.length > shown.length && (
            <p className="mt-2 font-mono text-[10px] text-slate-600">
              showing 60 of {visible.length}
            </p>
          )}
        </>
      )}

      {investigation.errors.length > 0 && (
        <div className="mt-4 space-y-1.5">
          <Honest>
            The investigation reported {investigation.errors.length} error(s). Any evidence
            that was still collected is shown above and is kept.
          </Honest>
          <ul className="space-y-1">
            {investigation.errors.map((error, i) => (
              <li key={i} className="font-mono text-[11px] text-rose-300">
                {error}
              </li>
            ))}
          </ul>
        </div>
      )}

      {!investigation.tests_executed && (
        <p className="mt-4 font-mono text-[10px] text-slate-600">
          Note: nothing here has been executed. tests_executed is false, so no item in this
          panel is a VERIFIED_RESULT.
        </p>
      )}
    </Card>
  );
}
