"""The incident pipeline: signal in, pull request out.

Deliberately plain Python rather than an ADK multi-agent delegation tree. Each
agent gets its own run, its own budget and its own audit trail, and the handoffs
between them are explicit and inspectable. When the demo is a live break-and-fix
on camera, "I can point at exactly what happened and when" is worth more than
elegance.
"""

from __future__ import annotations

import logging
import uuid

from warden.agents.runtime import AgentRun, run_agent
from warden.control_plane.store import Store
from warden.models import (
    AgentManifest,
    Diagnosis,
    Incident,
    IncidentStatus,
    TriageVerdict,
    utcnow,
)
from warden.tools.toolbox import ToolBox

log = logging.getLogger("warden.orchestrator")


class IncidentResult:
    def __init__(self, incident: Incident) -> None:
        self.incident = incident
        self.triage: AgentRun | None = None
        self.diagnosis: AgentRun | None = None
        self.remediation: AgentRun | None = None
        self.stopped_at: str = ""

    @property
    def runs(self) -> list[AgentRun]:
        return [r for r in (self.triage, self.diagnosis, self.remediation) if r]

    @property
    def total_tokens(self) -> int:
        return sum(r.run.total_tokens for r in self.runs)

    @property
    def total_tool_calls(self) -> int:
        return sum(r.run.tool_calls for r in self.runs)


async def handle_incident(
    *,
    alert: dict,
    fleet: dict[str, AgentManifest],
    toolbox: ToolBox,
    store: Store,
    models: dict | None = None,
) -> IncidentResult:
    """Run one signal through the fleet.

    `models` maps agent name to a model override, used for scripted/offline runs.
    In production it is None and each agent uses the model from its manifest.
    """
    models = models or {}

    incident = Incident(
        id=f"INC-{uuid.uuid4().hex[:8].upper()}",
        source=alert.get("source", "unknown"),
        signature=alert.get("signature", ""),
        title=alert.get("title", ""),
        workload=None,
    )
    await store.put_incident(incident)
    result = IncidentResult(incident)
    log.info("opened %s — %s", incident.id, incident.title)

    # -- 1. Triage ---------------------------------------------------------
    result.triage = await run_agent(
        manifest=fleet["triage"],
        toolbox=toolbox,
        store=store,
        incident_id=incident.id,
        prompt=f"A signal arrived: {alert}. Triage it.",
        model_override=models.get("triage"),
    )
    verdict = result.triage.parse(TriageVerdict)

    if verdict:
        incident.severity = verdict.severity
        if not verdict.escalate:
            incident.status = IncidentStatus.abandoned
            incident.closed_at = utcnow()
            await store.put_incident(incident)
            result.stopped_at = "triage"
            log.info("%s closed at triage: %s", incident.id, verdict.reasoning)
            return result

    incident.status = IncidentStatus.diagnosing
    await store.put_incident(incident)

    # -- 2. Diagnose -------------------------------------------------------
    result.diagnosis = await run_agent(
        manifest=fleet["diagnostician"],
        toolbox=toolbox,
        store=store,
        incident_id=incident.id,
        prompt=(
            f"Incident {incident.id}: {alert.get('title')}. "
            f"The affected workload is {alert.get('workload', 'checkout-svc')} "
            f"in namespace {alert.get('namespace', 'demo')}. Diagnose it."
        ),
        model_override=models.get("diagnostician"),
    )
    diagnosis = result.diagnosis.parse(Diagnosis)

    if diagnosis is None:
        result.stopped_at = "diagnosis"
        log.warning("%s: diagnostician returned no structured diagnosis", incident.id)
        return result

    incident.status = IncidentStatus.remediating
    await store.put_incident(incident)

    # -- 3. Remediate ------------------------------------------------------
    result.remediation = await run_agent(
        manifest=fleet["remediator"],
        toolbox=toolbox,
        store=store,
        incident_id=incident.id,
        prompt=(
            f"Incident {incident.id}.\n"
            f"Hypothesis: {diagnosis.hypothesis}\n"
            f"Root cause: {diagnosis.root_cause}\n"
            f"Suggested fix: {diagnosis.suggested_fix}\n"
            f"Evidence:\n"
            + "\n".join(f"  - [{e.source}] {e.detail}" for e in diagnosis.evidence)
            + "\n\nOpen a pull request that fixes this."
        ),
        model_override=models.get("remediator"),
    )

    incident.status = IncidentStatus.awaiting_merge
    await store.put_incident(incident)
    log.info("%s awaiting merge", incident.id)
    return result
