/**
 * The ten-stage pipeline, derived from the backend's real workflow events.
 *
 * Nothing here is a guess about what "should" have happened. Each stage
 * declares the exact event types the orchestrator emits for it, and a stage is
 * only ever `completed` when the backend actually emitted its completion
 * event. A stage the backend never reached stays `pending` — which is what
 * makes the pipeline honest when a run is sitting at the approval gate.
 */

import type { WorkflowEvent } from "./types";

export type StageStatus = "pending" | "running" | "completed" | "failed";

export interface StageDef {
  id: string;
  label: string;
  /** Backend event type that marks this stage complete. */
  done: string;
  /** Backend event type that marks this stage as having started. */
  started: string;
  /** Backend event type that marks this stage as failed, if it has one. */
  failed?: string;
  /** Restrict matching to a specific `payload.stage` / `payload.investigator`. */
  match?: (event: WorkflowEvent) => boolean;
}

const byPayloadStage =
  (stage: string) =>
  (event: WorkflowEvent): boolean =>
    event.payload?.stage === stage;

const byInvestigator =
  (name: string) =>
  (event: WorkflowEvent): boolean =>
    event.payload?.investigator === name;

/**
 * Ordered exactly as a reviewer reads the work. The first seven are driven by
 * the server during `POST /api/mentor/runs`; the last three only happen after a
 * human approves the diff.
 */
export const STAGES: StageDef[] = [
  {
    id: "explore",
    label: "Explore repository",
    done: "STAGE_COMPLETED",
    started: "STAGE_STARTED",
    failed: "STAGE_FAILED",
    match: byPayloadStage("REPOSITORY_CONTEXT"),
  },
  {
    id: "code",
    label: "Investigate code",
    done: "INVESTIGATOR_COMPLETED",
    started: "INVESTIGATOR_STARTED",
    failed: "INVESTIGATOR_FAILED",
    match: byInvestigator("code_investigator"),
  },
  {
    id: "tests",
    label: "Investigate tests",
    done: "INVESTIGATOR_COMPLETED",
    started: "INVESTIGATOR_STARTED",
    failed: "INVESTIGATOR_FAILED",
    match: byInvestigator("test_investigator"),
  },
  {
    id: "dependencies",
    label: "Investigate dependencies & data flow",
    done: "INVESTIGATOR_COMPLETED",
    started: "INVESTIGATOR_STARTED",
    failed: "INVESTIGATOR_FAILED",
    match: byInvestigator("dependency_investigator"),
  },
  {
    id: "synthesis",
    label: "Synthesize evidence",
    done: "EVIDENCE_SYNTHESIS_COMPLETED",
    started: "EVIDENCE_SYNTHESIS_STARTED",
    failed: "STAGE_FAILED",
    match: byPayloadStage("EVIDENCE_PENDING"),
  },
  {
    id: "root_cause",
    label: "Root cause analysis",
    done: "ROOT_CAUSE_ANALYSIS_COMPLETED",
    started: "ROOT_CAUSE_ANALYSIS_STARTED",
    failed: "ROOT_CAUSE_ANALYSIS_FAILED",
    match: byPayloadStage("ROOT_CAUSE_ANALYSIS"),
  },
  {
    id: "proposal",
    label: "Fix proposal",
    done: "FIX_PROPOSAL_COMPLETED",
    started: "FIX_PROPOSAL_STARTED",
    failed: "FIX_PROPOSAL_FAILED",
    match: byPayloadStage("FIX_PROPOSAL"),
  },
  {
    id: "approval",
    label: "Human approval",
    done: "FIX_APPROVED",
    // The backend announces the gate with FIX_APPROVAL_REQUIRED and
    // STAGE_PENDING(FIX_PROPOSAL_READY): the run is waiting on a person, which
    // is a real, observable "running" state rather than an absence of one.
    started: "FIX_APPROVAL_REQUIRED",
    match: () => true,
  },
  {
    id: "application",
    label: "Apply fix",
    done: "FIX_APPLIED",
    started: "FIX_APPLICATION_STARTED",
    failed: "FIX_APPLICATION_FAILED",
    match: () => true,
  },
  {
    id: "verification",
    label: "Verification",
    done: "VERIFICATION_COMPLETED",
    started: "VERIFICATION_STARTED",
    failed: "VERIFICATION_FAILED",
    match: () => true,
  },
];

export interface StageState {
  def: StageDef;
  status: StageStatus;
  /** The backend event that decided this status, for the tooltip/details. */
  event?: WorkflowEvent;
  /** Backend event count of the same type, e.g. files or items processed. */
  summary?: string;
}

/** Human summary of what a stage's own completion event reported. */
function summarise(def: StageDef, event: WorkflowEvent): string | undefined {
  const p = event.payload ?? {};

  switch (def.id) {
    case "explore":
      return typeof p.repository_name === "string"
        ? `${String(p.repository_name)} · ${String(p.file_count ?? "?")} files`
        : undefined;
    case "code":
    case "tests":
    case "dependencies":
      return typeof p.evidence_count === "number"
        ? `${p.evidence_count} evidence items in ${Number(p.duration_seconds ?? 0).toFixed(2)}s`
        : undefined;
    case "synthesis":
      return typeof p.finding_count === "number"
        ? `${p.finding_count} findings · ${String(p.coverage_gap_count ?? 0)} coverage gaps`
        : undefined;
    case "root_cause":
      return typeof p.root_cause_kind === "string"
        ? `${String(p.root_cause_kind)} · ${String(p.confidence_level ?? "")} ${Number(p.confidence ?? 0).toFixed(2)}`
        : undefined;
    case "proposal":
      return typeof p.change_count === "number"
        ? `${p.change_count} change(s) · +${String(p.added_lines ?? 0)}/-${String(p.removed_lines ?? 0)} · risk ${String(p.risk_level ?? "?")}`
        : undefined;
    case "approval":
      return typeof p.approved_by === "string" ? `approved by ${p.approved_by}` : undefined;
    case "application":
      return typeof p.file_hashes === "object" && p.file_hashes !== null
        ? `${Object.keys(p.file_hashes as Record<string, string>).length} file(s) confirmed on disk`
        : undefined;
    case "verification":
      return typeof p.status === "string"
        ? `${String(p.status)} · exit ${String(p.exit_code ?? "—")}`
        : undefined;
    default:
      return undefined;
  }
}

/** Replay the whole event history into pipeline state. Pure and deterministic. */
export function deriveStages(events: WorkflowEvent[]): StageState[] {
  return STAGES.map((def) => {
    let status: StageStatus = "pending";
    let decided: WorkflowEvent | undefined;

    for (const event of events) {
      if (def.match && !def.match(event)) continue;

      if (def.failed && event.event_type === def.failed) {
        status = "failed";
        decided = event;
        break;
      }
      if (event.event_type === def.done) {
        status = "completed";
        decided = event;
        break;
      }
      if (event.event_type === def.started && status === "pending") {
        status = "running";
        decided = event;
      }
    }

    return {
      def,
      status,
      event: decided,
      summary: decided && status === "completed" ? summarise(def, decided) : undefined,
    };
  });
}

/** 0–1 progress, used for the single progress bar. */
export function pipelineProgress(stages: StageState[]): number {
  if (!stages.length) return 0;
  const scored = stages.reduce((sum, stage) => {
    if (stage.status === "completed") return sum + 1;
    if (stage.status === "running" || stage.status === "failed") return sum + 0.5;
    return sum;
  }, 0);
  return scored / stages.length;
}
