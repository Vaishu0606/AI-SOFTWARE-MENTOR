/**
 * TypeScript mirrors of the backend OpenAPI schemas.
 *
 * Every type here is transcribed from the live contract at
 * `GET http://127.0.0.1:8000/openapi.json` (components.schemas). Nothing is
 * invented: if a field is not in the backend response it is not in this file,
 * and the UI renders "not available" rather than a plausible-looking default.
 *
 * Verified against the real backend with an end-to-end run against
 * `demo-repository/ShopDemo`.
 */

/* ── Enums (exact backend members) ─────────────────────────────────────────── */

export type EvidenceType =
  | "OBSERVATION"
  | "HYPOTHESIS"
  | "PROPOSED_ACTION"
  | "VERIFIED_RESULT";

export type InvestigatorName =
  | "code_investigator"
  | "test_investigator"
  | "dependency_investigator";

export type InvestigatorStatus = "PENDING" | "RUNNING" | "COMPLETED" | "FAILED";

export type InvestigationStatus =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "PARTIAL"
  | "FAILED";

export type RootCauseStatus =
  | "DETERMINED"
  | "INSUFFICIENT_EVIDENCE"
  | "NO_EVIDENCE"
  | "PARTIAL"
  | "FAILED";

export type RootCauseKind =
  | "TRUNCATION_BEFORE_SCALING"
  | "UNEXERCISED_PATH"
  | "UNCOVERED_INTERACTION"
  | "UNRESOLVED";

export type ConfidenceLevel = "HIGH" | "MEDIUM" | "LOW";

export type ChainNodeKind = "ROUTE" | "CALL" | "SITE" | "DATA_FLOW" | "COVERAGE";

export type FactorCategory =
  | "MISSING_COVERAGE"
  | "UNEXERCISED_ENTRY_POINT"
  | "PARTIAL_INVESTIGATION"
  | "UNRESOLVED_CONFLICT";

export type FixProposalStatus =
  | "READY_FOR_REVIEW"
  | "INSUFFICIENT_EVIDENCE"
  | "INVALID";

export type RiskLevel = "LOW" | "MEDIUM" | "HIGH";

export type ChangeType = "MODIFY";

export type CoverageGapKind =
  | "CONCEPTS_NEVER_TESTED_TOGETHER"
  | "RELEVANT_FILE_UNTESTED"
  | "REQUEST_BOUNDARY_NOT_COVERED";

export type TestFileStatus = "EXISTING" | "PROPOSED_NEW";

export type FixApplicationStatus =
  | "APPLIED"
  | "ALREADY_APPLIED"
  | "REJECTED_STALE"
  | "REJECTED_UNSAFE"
  | "REJECTED_MISMATCH"
  | "FAILED_ROLLED_BACK"
  | "NOT_APPLIED";

export type VerificationStatus =
  | "PASSED"
  | "FAILED"
  | "TIMED_OUT"
  | "BLOCKED"
  | "NOT_RUN";

export type RunStatus = "PENDING" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";

export type WorkflowState =
  | "CREATED"
  | "REPOSITORY_CONTEXT"
  | "ARCHITECTURE_CONTEXT"
  | "INVESTIGATION_PENDING"
  | "EVIDENCE_PENDING"
  | "ROOT_CAUSE_ANALYSIS"
  | "ROOT_CAUSE_READY"
  | "FIX_PROPOSAL"
  | "FIX_PROPOSAL_READY"
  | "FIX_APPROVED"
  | "FIX_APPLYING"
  | "FIX_APPLIED"
  | "TEST_PLAN"
  | "VERIFICATION"
  | "VERIFIED"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED"
  | "VERIFICATION_FAILED";

export type WorkflowEventType =
  | "RUN_CREATED"
  | "STATE_CHANGED"
  | "STAGE_STARTED"
  | "STAGE_COMPLETED"
  | "STAGE_PENDING"
  | "STAGE_FAILED"
  | "RUN_COMPLETED"
  | "RUN_FAILED"
  | "RUN_CANCELLED"
  | "INVESTIGATION_STARTED"
  | "INVESTIGATOR_STARTED"
  | "INVESTIGATOR_COMPLETED"
  | "INVESTIGATOR_FAILED"
  | "INVESTIGATION_COMPLETED"
  | "EVIDENCE_SYNTHESIS_STARTED"
  | "EVIDENCE_SYNTHESIS_COMPLETED"
  | "ROOT_CAUSE_ANALYSIS_STARTED"
  | "ROOT_CAUSE_ANALYSIS_COMPLETED"
  | "ROOT_CAUSE_ANALYSIS_FAILED"
  | "FIX_PROPOSAL_STARTED"
  | "FIX_PROPOSAL_COMPLETED"
  | "FIX_PROPOSAL_FAILED"
  | "TEST_PLAN_STARTED"
  | "TEST_PLAN_COMPLETED"
  | "TEST_PLAN_FAILED"
  | "FIX_APPROVED"
  | "FIX_APPROVAL_REQUIRED"
  | "FIX_APPLICATION_STARTED"
  | "FIX_APPLIED"
  | "FIX_APPLICATION_FAILED"
  | "VERIFICATION_STARTED"
  | "VERIFICATION_COMPLETED"
  | "VERIFICATION_FAILED";

/* ── Shared shapes ─────────────────────────────────────────────────────────── */

export interface Evidence {
  id: string;
  session_id: string;
  evidence_type: EvidenceType;
  agent: string;
  title: string;
  description: string;
  source_file: string | null;
  source_line: number | null;
  raw_content: string | null;
  confidence: number | null;
  timestamp: string;
  metadata: Record<string, unknown>;
}

export interface EvidenceRef {
  evidence_id: string;
  agent: string;
  evidence_type: EvidenceType;
  title: string;
  source_file: string | null;
  source_line: number | null;
  method: string | null;
}

export interface SourceAnchor {
  file_path: string;
  line: number | null;
  line_end: number | null;
  function: string | null;
  excerpt: string | null;
}

export interface AnchorLocation {
  file_path: string;
  line: number | null;
  function: string | null;
}

export interface InvestigatorReport {
  investigator: InvestigatorName;
  status: InvestigatorStatus;
  duration_seconds: number;
  evidence_count: number;
  error: string | null;
  summary: Record<string, unknown>;
}

export interface ConfidenceBreakdown {
  base: number;
  corroboration_bonus: number;
  penalties: Record<string, number>;
  score: number;
  level: ConfidenceLevel;
  independent_investigators: number;
  direct_observations: number;
  inferred_hypotheses: number;
  rationale: string;
}

export interface EvidenceChainLink {
  kind: ChainNodeKind;
  label: string;
  anchor: SourceAnchor | null;
  evidence_ids: string[];
  detail: string;
}

export interface RootCauseCandidate {
  kind: RootCauseKind;
  summary: string;
  rationale: string;
  anchor: SourceAnchor;
  evidence_ids: string[];
  support_score: number;
}

export interface ContributingFactor {
  category: FactorCategory;
  description: string;
  evidence_ids: string[];
}

export interface ConflictingEvidence {
  description: string;
  evidence_ids: string[];
}

export interface UnresolvedQuestion {
  question: string;
  why_it_matters: string;
  recommended_step: string;
  evidence_ids: string[];
}

export interface FixChange {
  file_path: string;
  change_type: ChangeType;
  line: number;
  line_end: number | null;
  function: string | null;
  before: string;
  after: string;
  description: string;
}

export interface RiskAssessment {
  level: RiskLevel;
  factors: string[];
  rationale: string;
}

export interface ValidationPlan {
  steps: string[];
  tests_executed: boolean;
  notes: string;
}

export interface CoverageGap {
  id: string;
  kind: CoverageGapKind;
  title: string;
  rationale: string;
  evidence_ids: string[];
  concepts: string[];
  modules: string[];
  files: string[];
}

export interface TestScenario {
  id: string;
  name: string;
  purpose: string;
  suggested_file: string;
  file_status: TestFileStatus;
  target_function: string | null;
  preconditions: string[];
  steps: string[];
  expected_behavior: string;
  discriminating_condition: string | null;
  covers_gaps: string[];
  notes: string;
}

export interface AppliedFile {
  file_path: string;
  before_sha256: string;
  after_sha256: string;
  lines_applied: number;
  modified: boolean;
}

export interface FixApproval {
  run_id: string;
  proposal_fingerprint: string;
  approved: true;
  approved_by: string;
  comment: string;
  proposal_status: FixProposalStatus;
  affected_files: string[];
  change_count: number;
  added_lines: number;
  removed_lines: number;
}

export interface FixApplicationResponse {
  run_id: string;
  status: FixApplicationStatus;
  proposal_fingerprint: string;
  approved_by: string;
  files: AppliedFile[];
  total_lines_applied: number;
  rolled_back: boolean;
  verified: boolean;
  reason: string;
  limitations: string[];
  repository_modified: boolean;
}

export interface TestRunResult {
  command: string[];
  exit_code: number | null;
  duration_seconds: number;
  timed_out: boolean;
  stdout_excerpt: string;
  stderr_excerpt: string;
  output_truncated: boolean;
  targets: string[];
  tests_requested: number;
  tests_run: number | null;
  passed: number | null;
  failed: number | null;
  /** Read-only computed field: inferred from command + targets, never set. */
  readonly started: boolean;
}

export interface VerificationResult {
  run_id: string;
  status: VerificationStatus;
  test_run: TestRunResult;
  targets: string[];
  verified_file_hashes: Record<string, string>;
  proposal_fingerprint: string;
  tests_executed: boolean;
  regression_check: boolean;
  defect_demonstrated: boolean;
  fix_applied: boolean;
  findings: string[];
  limitations: string[];
}

export interface WorkflowEvent {
  event_id: string;
  run_id: string;
  sequence: number;
  event_type: WorkflowEventType;
  state: WorkflowState;
  status: RunStatus;
  message: string;
  timestamp: string;
  payload: Record<string, unknown>;
}

/* ── Endpoint responses ────────────────────────────────────────────────────── */

export interface MentorRunResponse {
  run_id: string;
  repository_path: string;
  issue: string;
  current_state: WorkflowState;
  status: RunStatus;
  error: string | null;
  created_at: string;
  updated_at: string;
  is_terminal: boolean;
  event_count: number;
  last_event_sequence: number;
  evidence: Evidence[];
  context: Record<string, unknown>;
}

export interface InvestigationResponse {
  run_id: string;
  run_state: WorkflowState;
  status: InvestigationStatus;
  investigators: InvestigatorReport[];
  evidence: Evidence[];
  evidence_counts: Record<string, number>;
  errors: string[];
  started_at: string | null;
  completed_at: string | null;
  /** Always false — Phase 7 executes nothing. */
  tests_executed: boolean;
  root_cause_claimed: boolean;
}

export interface RootCauseResponse {
  run_id: string;
  issue: string;
  status: RootCauseStatus;
  root_cause: string | null;
  root_cause_kind: RootCauseKind | null;
  anchor: SourceAnchor | null;
  confidence: number;
  confidence_level: ConfidenceLevel;
  confidence_rationale: string;
  confidence_breakdown: ConfidenceBreakdown;
  evidence_chain: EvidenceChainLink[];
  supporting_evidence: EvidenceRef[];
  rejected_candidates: RootCauseCandidate[];
  contributing_factors: ContributingFactor[];
  conflicting_evidence: ConflictingEvidence[];
  unresolved_questions: UnresolvedQuestion[];
  investigation_status: InvestigationStatus;
  partial_investigation: boolean;
  missing_evidence: string[];
  evidence_counts: Record<string, number>;
  raw_evidence_count: number;
  deduplicated_evidence_count: number;
  /** Always false in Phase 8 — a verified result requires a real exit code. */
  tests_executed: boolean;
  verified_result_count: number;
  corroborating_investigators: string[];
  recommended_next_investigation: string[];
  run_state: WorkflowState;
  root_cause_claimed: boolean;
  synthesized_at: string | null;
}

export interface FixProposalResponse {
  run_id: string;
  issue: string;
  status: FixProposalStatus;
  rationale: string;
  root_cause: string | null;
  root_cause_kind: RootCauseKind | null;
  root_cause_confidence: number;
  root_cause_confidence_level: ConfidenceLevel;
  root_cause_status: string;
  anchor: AnchorLocation | null;
  supporting_evidence: EvidenceRef[];
  affected_files: string[];
  changes: FixChange[];
  unified_diff: string | null;
  risk: RiskAssessment;
  validation_plan: ValidationPlan;
  assumptions: string[];
  limitations: string[];
  tests_executed: boolean;
  run_state: WorkflowState;
  ready_for_review: boolean;
  applied: boolean;
  added_lines: number;
  removed_lines: number;
  proposal_fingerprint: string | null;
  created_at: string | null;
}

export interface TestPlanResponse {
  run_id: string;
  issue: string;
  run_state: WorkflowState;
  status: string;
  rationale: string;
  anchor: AnchorLocation | null;
  root_cause_kind: string | null;
  root_cause_confidence: number;
  fix_proposal_status: string | null;
  coverage_gaps: CoverageGap[];
  scenarios: TestScenario[];
  execution_status: string;
  ready_for_review: boolean;
  tests_executed: boolean;
  test_source_written: boolean;
  assumptions: string[];
  limitations: string[];
  created_at: string | null;
}

export interface ApproveFixResponse {
  run: MentorRunResponse;
  approval: FixApproval;
  application: FixApplicationResponse;
  verification: VerificationResult;
  events: WorkflowEvent[];
}
