from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
import uuid


class AccountProfile(BaseModel):
    """Mutable per-account settings. Populated incrementally — Phase 4 fills
    most of these in via the onboarding intake and learned defaults."""
    time_zone: Optional[str] = None
    amount_tolerance_abs: float = 0.01
    amount_tolerance_pct: float = 0.005
    # Materiality: what counts as a "major" discrepancy for THIS brand.
    materiality_abs: float = 100.0
    materiality_pct: float = 0.03
    # Phase 4 will add: custom_fee_rates, known_source_labels, etc.


class Account(BaseModel):
    """An account is the unit of memory + access in v3.

    Created on first visit (no auth in the pilot). The UUID acts as the
    access token via the X-Account-Id header. Every memory write is scoped
    to a single account.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    display_name: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    profile: AccountProfile = Field(default_factory=AccountProfile)


ReconType = Literal[
    "orders_vs_payments",
    "inventory_cross_check",
    "po_vs_invoices",
    "custom",
]


Provenance = Literal["inferred", "user_confirmed", "rule_applied"]


class SemanticBinding(BaseModel):
    """Binds a column in a specific file to a concept in the ontology."""
    column_name: str
    concept_id: str            # e.g. "order.gross_total"
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: Provenance = "inferred"
    evidence: List[str] = Field(default_factory=list)
    alternatives: List[Dict[str, Any]] = Field(default_factory=list)
    # Each alternative: {"concept_id": str, "confidence": float, "reason": str}


class BindingSet(BaseModel):
    """All bindings for one side (Source A or Source B)."""
    bindings: List[SemanticBinding] = Field(default_factory=list)

    def by_role(self, role: str) -> Optional[SemanticBinding]:
        """Return the highest-confidence binding whose concept has the given role."""
        from .ontology import concept_by_id
        candidates = [
            b for b in self.bindings
            if (c := concept_by_id(b.concept_id)) is not None and c.role == role
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda b: b.confidence)

    def by_concept(self, concept_id: str) -> Optional[SemanticBinding]:
        for b in self.bindings:
            if b.concept_id == concept_id:
                return b
        return None


class ReconcileConfig(BaseModel):
    recon_type: ReconType = "custom"
    source_a: BindingSet
    source_b: BindingSet
    label_a: str = "Source A"
    label_b: str = "Source B"
    amount_tolerance_abs: float = 0.01
    amount_tolerance_pct: float = 0.005  # 0.5%
    # Escape hatch for the mixed-currency guard (user explicitly accepts risk)
    allow_mixed_currency: bool = False


class PreviewResponse(BaseModel):
    columns: List[str]
    rows: List[Dict[str, Any]]
    row_count: int
    # legacy field; kept for any unchanged consumer. Frontend now uses /api/bind.
    suggested: Dict[str, Optional[str]] = Field(default_factory=dict)


class BindResponse(BaseModel):
    filename: str
    row_count: int
    columns: List[str]
    bindings: List[SemanticBinding]


class JobStatus(BaseModel):
    job_id: str
    status: Literal["processing", "complete", "error"]
    error: Optional[str] = None
    created_at: str
    recon_type: Optional[str] = None
    label_a: Optional[str] = None
    label_b: Optional[str] = None


class Summary(BaseModel):
    total_a: int
    total_b: int
    matched: int
    matched_pct: float
    unmatched_a: int
    unmatched_b: int
    discrepancies: int
    fuzzy_matches: int
    total_discrepancy_value: float
    total_amount_a: float
    total_amount_b: float
    # Many-to-one support: rows folded into their key group before matching
    aggregated_a: int = 0
    aggregated_b: int = 0


# -----------------------------------------------------------------------------
# v3: Rationale objects (Phase 2)
#
# Every classification of a matched row carries a Rationale: the verdict
# (status), how confident we are, *which rules fired* (Evidence), and what
# else we considered (alternatives). This is the auditability substrate the
# agent (Phase 3) and the memory layer (Phase 4) will write into.
# -----------------------------------------------------------------------------

# Tightened status enum so the Excel report's conditional formatting and the
# frontend status pills always agree on the vocabulary.
RowStatus = Literal["match", "minor", "major", "fee_offset"]


class Evidence(BaseModel):
    """One reason a classification was reached.

    `source` is a stable rule identifier (e.g. "stripe_fee_pattern",
    "threshold_major", "tolerance"). `evidence` is a human-readable
    explanation containing the actual numbers that fired the rule. `weight`
    is how much this piece of evidence contributed to the final verdict —
    Phase 3's LLM tool can return multiple Evidence entries with weights
    summing to ~1.0.
    """
    source: str
    evidence: str
    weight: float = 1.0


class Alt(BaseModel):
    """An alternative classification that was considered but not chosen."""
    status: RowStatus
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class Rationale(BaseModel):
    """Per-row classification with full provenance.

    The free-text `user_reason` field is the highest-signal capture surface
    in the product — when a user disagrees with `status`, the one-line
    justification they type is stored here and later becomes the
    `user_origin_text` of any rule the system proposes. Phase 5 wires the
    write path; Phase 2 just makes the field exist.
    """
    row_key: str
    status: RowStatus
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: List[Evidence] = Field(default_factory=list)
    alternatives: List[Alt] = Field(default_factory=list)
    user_reason: Optional[str] = None


# -----------------------------------------------------------------------------
# v3: Rule stub (full implementation lands in Phase 4)
#
# Kept here so `user_origin_text` exists in the schema before Phase 4 starts
# writing to it — avoids a schema migration mid-flight.
# -----------------------------------------------------------------------------

RuleState = Literal["pending", "active", "revoked"]
RuleKind = Literal[
    "fee_pattern",            # diff matches rate*amount + flat → fee_offset
    "expected_unmatched",     # rows with key matching pattern are expected to be unmatched
    "tolerance_override",     # per-account tolerance for a status bucket
    "force_status",           # rows matching a signature pattern get a specific status
    "custom",
]


class Rule(BaseModel):
    """An account-scoped rule that the agent applies *before* LLM classification.

    `when` and `then` are small structured dicts (not raw Python) so the
    pilot doesn't need a real expression evaluator. The dispatcher in
    memory/rules_store.py knows how to interpret each `kind`.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    account_id: str
    kind: RuleKind
    description: str = ""              # human-readable for UI
    when: Dict[str, Any] = Field(default_factory=dict)
    then: Dict[str, Any] = Field(default_factory=dict)
    origin: str = "system"             # "system" | "user_confirmed_Nx" | "imported"
    user_origin_text: Optional[str] = None  # the free-text justification (if any) that seeded this rule
    created_by: Optional[str] = None   # email of the user who taught this rule (None = system)
    confidence: float = 1.0
    state: RuleState = "pending"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # Phase 4: track which signatures this rule has been applied to
    applied_signatures: List[str] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# v3: Phase 4 entities — TriageItem, BrandNote, DecisionLogEntry, AccountMetrics
# -----------------------------------------------------------------------------

TriageState = Literal["open", "resolved", "deferred", "recurring"]


class TriageItem(BaseModel):
    """A persistent, cross-job item the user can act on.

    Signature-based dedup: when the same kind of gap recurs across jobs,
    the existing TriageItem accumulates `source_job_ids` rather than
    creating a new copy in the inbox.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    account_id: str
    signature: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)
    state: TriageState = "open"
    source_job_ids: List[str] = Field(default_factory=list)
    # Display payload from the most recent occurrence
    row_key: Optional[str] = None
    status: Optional[str] = None
    side: Optional[Literal["a", "b", "both"]] = None
    amount_a: Optional[float] = None
    amount_b: Optional[float] = None
    diff_abs: Optional[float] = None
    fee_pattern: Optional[str] = None
    rationale: Optional[Rationale] = None
    # Resolution captured when the user acts
    resolution: Optional[Dict[str, Any]] = None


class BrandNote(BaseModel):
    """One entry in the account's notes log — onboarding intake, drop-in note,
    or per-decision justification — with whatever the extractor pulled out."""
    at: datetime = Field(default_factory=datetime.utcnow)
    kind: Literal["intake", "note", "justification"] = "note"
    text: str
    parsed_proposals: Dict[str, Any] = Field(default_factory=dict)
    # Optional links back to the context that produced the note
    job_id: Optional[str] = None
    row_key: Optional[str] = None


class DecisionLogEntry(BaseModel):
    """One record of the user disagreeing with the system's verdict."""
    at: datetime = Field(default_factory=datetime.utcnow)
    job_id: Optional[str] = None
    row_key: Optional[str] = None
    signature: Optional[str] = None
    original_status: Optional[str] = None
    user_status: Optional[str] = None    # "expected" | "investigate" | force a status | etc.
    user_reason: Optional[str] = None    # the free-text justification
    # Who made this decision (Phase 2.1 auth) — None on pre-auth entries
    user_id: Optional[str] = None
    user_email: Optional[str] = None


class AccountMetrics(BaseModel):
    """Per-job insight-density snapshot."""
    job_id: str
    at: datetime = Field(default_factory=datetime.utcnow)
    total_rows: int = 0
    auto_handled: int = 0
    needed_user: int = 0
    insight_density: float = 0.0
    override_rate: float = 0.0
    revocation_rate: float = 0.0
    trust_adjusted_density: float = 0.0
    llm_calls: int = 0


# -----------------------------------------------------------------------------
# Job result — now carries rationales + bindings (v3 shape)
# -----------------------------------------------------------------------------


class ReconcileResult(BaseModel):
    job_id: str
    created_at: str
    config: ReconcileConfig
    summary: Summary
    # v3: matched rows are now structured Rationale objects + a small row-context dict
    # for display (the raw key/amount/date columns). Both shapes coexist for one phase
    # so the storage shim can read v1 jobs without crashing.
    matched: List[Dict[str, Any]] = Field(default_factory=list)
    unmatched_a: List[Dict[str, Any]] = Field(default_factory=list)
    unmatched_b: List[Dict[str, Any]] = Field(default_factory=list)
    discrepancies: List[Dict[str, Any]] = Field(default_factory=list)
    insights: str = ""
    timing: Optional[Dict[str, Any]] = None
    # v3 additions:
    bindings_a: Optional[BindingSet] = None
    bindings_b: Optional[BindingSet] = None
