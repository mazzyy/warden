"""Domain types. These are the contract between every layer — keep them boring."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Estate — what the adapters return. Deliberately cloud-agnostic (ADR-005).
# --------------------------------------------------------------------------


class WorkloadRef(BaseModel):
    namespace: str
    name: str
    kind: Literal["Deployment", "Service", "CloudRunService"] = "Deployment"

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.kind.lower()}/{self.namespace}/{self.name}"


class Workload(BaseModel):
    ref: WorkloadRef
    replicas_desired: int = 0
    replicas_ready: int = 0
    image: str = ""


class ContainerState(BaseModel):
    name: str
    ready: bool
    restart_count: int = 0
    reason: str | None = None
    exit_code: int | None = None


class WorkloadDetail(BaseModel):
    ref: WorkloadRef
    replicas_desired: int = 0
    replicas_ready: int = 0
    image: str = ""
    containers: list[ContainerState] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    conditions: list[str] = Field(default_factory=list)


class LogLine(BaseModel):
    ts: datetime
    container: str
    message: str


class Deploy(BaseModel):
    ts: datetime
    revision: str
    image: str
    changed_by: str = "unknown"
    commit_sha: str | None = None
    summary: str = ""


class MetricPoint(BaseModel):
    ts: datetime
    value: float


class MetricSeries(BaseModel):
    metric: str
    unit: str = ""
    points: list[MetricPoint] = Field(default_factory=list)


class Status(BaseModel):
    ref: WorkloadRef
    healthy: bool
    summary: str


# --------------------------------------------------------------------------
# Agent manifests — the registry's source of truth (ADR-003)
# --------------------------------------------------------------------------


class BlastRadius(BaseModel):
    namespace: str = "demo"
    max_files_per_patch: int = Field(default=3, alias="maxFilesPerPatch")
    model_config = {"populate_by_name": True}


class BudgetSpec(BaseModel):
    max_tokens_per_run: int = Field(default=120_000, alias="maxTokensPerRun")
    max_tool_calls: int = Field(default=25, alias="maxToolCalls")
    model_config = {"populate_by_name": True}


class CircuitBreakerSpec(BaseModel):
    failures_before_open: int = Field(default=3, alias="failuresBeforeOpen")
    cooldown_seconds: int = Field(default=900, alias="cooldownSeconds")
    model_config = {"populate_by_name": True}


class AgentSpec(BaseModel):
    model: str
    tools: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    blast_radius: BlastRadius = Field(default_factory=BlastRadius, alias="blastRadius")
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    approval: Literal["auto", "required"] = "auto"
    circuit_breaker: CircuitBreakerSpec = Field(
        default_factory=CircuitBreakerSpec, alias="circuitBreaker"
    )
    model_config = {"populate_by_name": True}


class AgentMetadata(BaseModel):
    name: str
    description: str = ""


class AgentManifest(BaseModel):
    api_version: str = Field(default="warden.dev/v1", alias="apiVersion")
    kind: Literal["Agent"] = "Agent"
    metadata: AgentMetadata
    spec: AgentSpec
    model_config = {"populate_by_name": True}

    @property
    def name(self) -> str:
        return self.metadata.name


# --------------------------------------------------------------------------
# Control plane records
# --------------------------------------------------------------------------


class Severity(StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    noise = "noise"


class IncidentStatus(StrEnum):
    open = "open"
    diagnosing = "diagnosing"
    remediating = "remediating"
    awaiting_merge = "awaiting_merge"
    verifying = "verifying"
    resolved = "resolved"
    abandoned = "abandoned"


class Incident(BaseModel):
    id: str
    source: str
    signature: str
    severity: Severity = Severity.medium
    status: IncidentStatus = IncidentStatus.open
    workload: WorkloadRef | None = None
    title: str = ""
    opened_at: datetime = Field(default_factory=utcnow)
    closed_at: datetime | None = None
    pr_url: str | None = None
    memory_refs: list[str] = Field(default_factory=list)


class RunStatus(StrEnum):
    running = "running"
    ok = "ok"
    failed = "failed"
    budget_exceeded = "budget_exceeded"
    killed = "killed"


class Run(BaseModel):
    id: str
    incident_id: str
    agent: str
    model: str
    status: RunStatus = RunStatus.running
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    # usage_metadata is emitted PER MODEL CALL, so a tool-using turn produces
    # several. These are running sums, never a last-value read.
    prompt_tokens: int = 0
    candidates_tokens: int = 0
    total_tokens: int = 0
    tool_calls: int = 0
    outcome: str = ""


class Decision(StrEnum):
    allow = "allow"
    deny = "deny"


class AuditRecord(BaseModel):
    id: str
    run_id: str
    incident_id: str
    agent: str
    tool: str
    args_redacted: dict[str, Any] = Field(default_factory=dict)
    decision: Decision
    reason: str = ""
    latency_ms: int = 0
    ts: datetime = Field(default_factory=utcnow)


class FleetState(BaseModel):
    kill_switch: bool = False
    drained_at: datetime | None = None
    note: str = ""


# --------------------------------------------------------------------------
# Agent outputs — used as ADK output_schema. NOTE: ADK stores output_key as a
# plain dict, not the pydantic instance, so re-hydrate with model_validate.
# --------------------------------------------------------------------------


class TriageVerdict(BaseModel):
    severity: Severity = Field(description="How serious this signal is")
    escalate: bool = Field(description="True if the fleet should investigate")
    duplicate_of: str | None = Field(
        default=None, description="Existing incident id if this is a repeat"
    )
    reasoning: str = Field(description="One or two sentences on why")


class Evidence(BaseModel):
    source: str = Field(description="Which tool produced this, e.g. get_workload_logs")
    detail: str = Field(description="The specific line, value or fact observed")


class Diagnosis(BaseModel):
    hypothesis: str = Field(description="What is wrong, in one sentence")
    root_cause: str = Field(description="The specific change or condition responsible")
    evidence: list[Evidence] = Field(description="Facts supporting the hypothesis")
    suggested_fix: str = Field(description="What change would resolve it")
    confidence: float = Field(ge=0.0, le=1.0, description="0-1")


class ProposedPatch(BaseModel):
    pr_url: str | None = Field(default=None, description="URL of the opened pull request")
    files_changed: list[str] = Field(default_factory=list)
    rationale: str = Field(description="Why this patch fixes the incident")
