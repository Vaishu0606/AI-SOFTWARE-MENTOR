/**
 * Typed client for the AI Software Mentor backend.
 *
 * Only endpoints that actually exist are wrapped here. Every path is
 * transcribed from the live OpenAPI document.
 *
 * One distinction matters more than the rest: a 409 from a phase read endpoint
 * does NOT mean something went wrong. It means the run exists but that phase
 * has not produced an answer yet, which the backend deliberately reports as a
 * different status from "investigated and found nothing". Callers therefore
 * get `StageNotReady` rather than a thrown error, and the UI renders "not
 * available yet" instead of a red failure.
 */

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
} from "./types";

/**
 * Backend origin. The backend's CORS policy allows `http://localhost:3000`
 * only, so the browser must reach it directly from the Next dev server.
 */
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/+$/, "") ??
  "http://127.0.0.1:8000";

/** The phase this endpoint reports on has not run yet. Not an error. */
export class StageNotReady extends Error {
  readonly detail: string;
  constructor(detail: string) {
    super(detail);
    this.name = "StageNotReady";
    this.detail = detail;
  }
}

/** A real failure: network down, 4xx we cannot act on, or 5xx. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  constructor(status: number, detail: string) {
    super(`HTTP ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/**
 * The approval gate said no. A 409 from `approve-fix` is *not* the same thing as
 * a 409 from a phase read: the phase exists and was read, the reviewer simply
 * was not allowed to apply it — the proposal was not reviewable, the fingerprint
 * no longer matches what the reviewer saw, or the run had already left the gate.
 * Collapsing this into `StageNotReady` would render a refusal as "not ready yet",
 * which is precisely the confusion the evidence-type rules exist to prevent.
 */
export class ApprovalRefused extends Error {
  readonly detail: string;
  constructor(detail: string) {
    super(detail);
    this.name = "ApprovalRefused";
    this.detail = detail;
  }
}

/**
 * Pull a human-readable message out of the backend's two error shapes:
 * FastAPI's `{"detail": "text"}` and its validation form
 * `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`.
 */
function readDetail(body: unknown, fallback: string): string {
  if (typeof body !== "object" || body === null) return fallback;
  const detail = (body as { detail?: unknown }).detail;

  if (typeof detail === "string") return detail;

  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (typeof item !== "object" || item === null) return String(item);
      const entry = item as { loc?: unknown[]; msg?: unknown };
      const field = Array.isArray(entry.loc)
        ? entry.loc.filter((p) => p !== "body").join(".")
        : "";
      const msg = typeof entry.msg === "string" ? entry.msg : JSON.stringify(item);
      return field ? `${field}: ${msg}` : msg;
    });
    if (parts.length) return parts.join("; ");
  }

  return fallback;
}

async function request<T>(
  method: "GET" | "POST",
  path: string,
  body?: unknown,
  // How a 409 should be classified. Phase reads treat it as "not ready";
  // the approval gate treats it as a refusal.
  conflict: "notReady" | "refused" = "notReady",
): Promise<T> {
  let response: Response;

  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    throw new ApiError(
      0,
      `Cannot reach the backend at ${API_BASE}. Is it running? (${String(cause)})`,
    );
  }

  const text = await response.text();
  let parsed: unknown = undefined;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = undefined;
    }
  }

  if (!response.ok) {
    const detail = readDetail(parsed, `${method} ${path} failed (${response.status})`);
    if (response.status === 409) {
      throw conflict === "notReady"
        ? new StageNotReady(detail)
        : new ApprovalRefused(detail);
    }
    throw new ApiError(response.status, detail);
  }

  return parsed as T;
}

/* ── Endpoints ─────────────────────────────────────────────────────────────── */

/**
 * `POST /api/mentor/runs` — create a run and drive it to its boundary.
 *
 * This call is **synchronous**: it walks the whole pipeline on the server
 * before responding, so the returned run is already at `FIX_PROPOSAL_READY`
 * waiting for a human. It is not fire-and-forget.
 */
export function createRun(
  repositoryPath: string,
  issue: string,
): Promise<MentorRunResponse> {
  return request<MentorRunResponse>("POST", "/api/mentor/runs", {
    repository_path: repositoryPath,
    issue,
  });
}

export function getRun(runId: string): Promise<MentorRunResponse> {
  return request<MentorRunResponse>("GET", `/api/mentor/runs/${runId}`);
}

export function getInvestigation(runId: string): Promise<InvestigationResponse> {
  return request<InvestigationResponse>("GET", `/api/mentor/runs/${runId}/investigation`);
}

export function getRootCause(runId: string): Promise<RootCauseResponse> {
  return request<RootCauseResponse>("GET", `/api/mentor/runs/${runId}/root-cause`);
}

export function getFixProposal(runId: string): Promise<FixProposalResponse> {
  return request<FixProposalResponse>("GET", `/api/mentor/runs/${runId}/fix-proposal`);
}

export function getTestPlan(runId: string): Promise<TestPlanResponse> {
  return request<TestPlanResponse>("GET", `/api/mentor/runs/${runId}/test-plan`);
}

export function getFixApplication(runId: string): Promise<FixApplicationResponse> {
  return request<FixApplicationResponse>("GET", `/api/mentor/runs/${runId}/fix-application`);
}

export function getVerification(runId: string): Promise<VerificationResult> {
  return request<VerificationResult>("GET", `/api/mentor/runs/${runId}/verification`);
}

/**
 * `POST /api/mentor/runs/{run_id}/approve-fix` — the human approval gate.
 *
 * The whole body is a reviewer's name, an optional note and optionally the
 * fingerprint the client displayed. There is deliberately no way to submit a
 * patch, a path or a command: the change that will be written is the one
 * already stored on the run, and `extra="forbid"` on the backend turns any
 * attempt to smuggle one into a 422.
 */
export function approveFix(
  runId: string,
  body: {
    approved_by: string;
    comment?: string;
    expected_fingerprint?: string | null;
  },
): Promise<ApproveFixResponse> {
  return request<ApproveFixResponse>(
    "POST",
    `/api/mentor/runs/${runId}/approve-fix`,
    {
      approved_by: body.approved_by,
      comment: body.comment ?? null,
      expected_fingerprint: body.expected_fingerprint ?? null,
    },
    "refused",
  );
}

/**
 * `GET /api/mentor/runs/{run_id}/events` — the SSE stream.
 *
 * The backend tails its own append-only event store, so the stream replays the
 * run's full history on connect and closes cleanly once a terminal run has
 * been drained. That makes it correct to open once per run and use for the
 * whole timeline: there is no polling, and no window where an event is lost
 * between requests.
 *
 * Returns a disposer.
 */
export function openEventStream(
  runId: string,
  onEvent: (event: WorkflowEvent) => void,
  onError?: (message: string) => void,
  onClose?: () => void,
): () => void {
  const url = `${API_BASE}/api/mentor/runs/${runId}/events?timeout=60`;

  let source: EventSource | null = null;
  let closed = false;

  const handle = (raw: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(raw.data) as WorkflowEvent);
    } catch {
      /* A frame we cannot parse is skipped rather than crashing the timeline. */
    }
  };

  try {
    source = new EventSource(url);
  } catch (cause) {
    onError?.(`Could not open the event stream: ${String(cause)}`);
    onClose?.();
    return () => {};
  }

  // The backend names each frame after its event type. A generic listener plus
  // the per-type listeners below would double-deliver, so every event type in
  // the contract is subscribed explicitly.
  const TYPES = [
    "RUN_CREATED", "STATE_CHANGED", "STAGE_STARTED", "STAGE_COMPLETED",
    "STAGE_PENDING", "STAGE_FAILED", "RUN_COMPLETED", "RUN_FAILED",
    "RUN_CANCELLED", "INVESTIGATION_STARTED", "INVESTIGATOR_STARTED",
    "INVESTIGATOR_COMPLETED", "INVESTIGATOR_FAILED", "INVESTIGATION_COMPLETED",
    "EVIDENCE_SYNTHESIS_STARTED", "EVIDENCE_SYNTHESIS_COMPLETED",
    "ROOT_CAUSE_ANALYSIS_STARTED", "ROOT_CAUSE_ANALYSIS_COMPLETED",
    "ROOT_CAUSE_ANALYSIS_FAILED", "FIX_PROPOSAL_STARTED",
    "FIX_PROPOSAL_COMPLETED", "FIX_PROPOSAL_FAILED", "TEST_PLAN_STARTED",
    "TEST_PLAN_COMPLETED", "TEST_PLAN_FAILED", "FIX_APPROVED",
    "FIX_APPROVAL_REQUIRED", "FIX_APPLICATION_STARTED", "FIX_APPLIED",
    "FIX_APPLICATION_FAILED", "VERIFICATION_STARTED", "VERIFICATION_COMPLETED",
    "VERIFICATION_FAILED",
  ] as const;

  for (const type of TYPES) source.addEventListener(type, handle as EventListener);

  source.onerror = () => {
    // EventSource retries on its own. Only a closed stream (terminal run) is
    // worth reporting; a transient drop should not surface as a failure.
    if (source?.readyState === EventSource.CLOSED && !closed) {
      onError?.("Event stream closed before the run finished.");
      onClose?.();
    }
  };

  return () => {
    if (closed) return;
    closed = true;
    for (const type of TYPES) source?.removeEventListener(type, handle as EventListener);
    source?.close();
  };
}

/** The evidence already on the run record, used before the phase endpoints load. */
export function evidenceFromRun(run: MentorRunResponse): Evidence[] {
  return run.evidence ?? [];
}
