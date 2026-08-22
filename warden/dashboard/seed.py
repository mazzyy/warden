"""Demo data, so a judge never lands on an empty page.

Three incidents with different endings, because a dashboard where everything
succeeded is less believable than one that shows the system declining to act:

  1. Resolved  — the full loop, ending in a merged pull request.
  2. Abandoned — triage correctly closed a duplicate without waking the fleet.
     Most alerts in a real estate are noise, and an escalation that wastes an
     engineer's attention is a real cost.
  3. Denied    — an agent attempted a tool it was not granted and the proxy
     refused. This is the one to click on during the video.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from warden.control_plane.store import Store
from warden.models import (
    AuditRecord,
    Decision,
    Incident,
    IncidentStatus,
    Run,
    RunStatus,
    Severity,
    WorkloadRef,
    utcnow,
)

CHECKOUT = WorkloadRef(namespace="demo", name="checkout-svc")


def _rid() -> str:
    return str(uuid.uuid4())


async def seed(store: Store) -> None:
    now = utcnow()

    # ---- 1. the full loop, resolved --------------------------------------
    inc1 = Incident(
        id="INC-7A3F21C4",
        source="cloud-monitoring",
        signature="checkout-svc/CrashLoopBackOff",
        severity=Severity.critical,
        status=IncidentStatus.resolved,
        workload=CHECKOUT,
        title="checkout-svc: 0/3 replicas ready",
        opened_at=now - timedelta(hours=3),
        closed_at=now - timedelta(hours=2, minutes=48),
        pr_url="https://github.com/mazzyy/estate-gitops/pull/412",
    )
    await store.put_incident(inc1)

    plan = [
        ("triage", "gemini-3.5-flash", 2, [("get_alert_context", 41), ("recall_similar_incidents", 88)]),
        (
            "diagnostician",
            "gemini-3.7-flash",
            4,
            [
                ("describe_workload", 210),
                ("get_workload_logs", 156),
                ("recent_deploys", 132),
                ("query_metrics", 97),
            ],
        ),
        ("remediator", "gemini-3.7-flash", 2, [("read_repo_file", 180), ("propose_patch", 1240)]),
        ("verifier", "gemini-3.5-flash", 2, [("get_workload_status", 74), ("query_metrics", 91)]),
    ]

    offset = 0
    for agent, model, calls, tools in plan:
        run = Run(
            id=_rid(),
            incident_id=inc1.id,
            agent=agent,
            model=model,
            status=RunStatus.ok,
            started_at=now - timedelta(hours=3) + timedelta(seconds=offset),
            ended_at=now - timedelta(hours=3) + timedelta(seconds=offset + 40),
            prompt_tokens=900 + calls * 220,
            candidates_tokens=180 + calls * 40,
            total_tokens=1080 + calls * 260,
            tool_calls=calls,
            outcome="ok",
        )
        await store.put_run(run)
        for i, (tool, latency) in enumerate(tools):
            await store.append_audit(
                AuditRecord(
                    id=_rid(),
                    run_id=run.id,
                    incident_id=inc1.id,
                    agent=agent,
                    tool=tool,
                    args_redacted={"namespace": "demo", "name": "checkout-svc"},
                    decision=Decision.allow,
                    latency_ms=latency,
                    ts=run.started_at + timedelta(seconds=i * 4),
                )
            )
        offset += 60

    # ---- 2. closed at triage, correctly ----------------------------------
    inc2 = Incident(
        id="INC-B90E4D17",
        source="cloud-monitoring",
        signature="checkout-svc/CrashLoopBackOff",
        severity=Severity.low,
        status=IncidentStatus.abandoned,
        workload=CHECKOUT,
        title="checkout-svc: repeat alert within cooldown",
        opened_at=now - timedelta(hours=2, minutes=51),
        closed_at=now - timedelta(hours=2, minutes=50),
        memory_refs=[inc1.id],
    )
    await store.put_incident(inc2)

    run2 = Run(
        id=_rid(),
        incident_id=inc2.id,
        agent="triage",
        model="gemini-3.5-flash",
        status=RunStatus.ok,
        started_at=inc2.opened_at,
        ended_at=inc2.opened_at + timedelta(seconds=6),
        prompt_tokens=760,
        candidates_tokens=90,
        total_tokens=850,
        tool_calls=2,
        outcome="duplicate of INC-7A3F21C4 — not escalated",
    )
    await store.put_run(run2)
    for i, tool in enumerate(("get_alert_context", "recall_similar_incidents")):
        await store.append_audit(
            AuditRecord(
                id=_rid(),
                run_id=run2.id,
                incident_id=inc2.id,
                agent="triage",
                tool=tool,
                args_redacted={"signature": "checkout-svc/CrashLoopBackOff"},
                decision=Decision.allow,
                latency_ms=38 + i * 25,
                ts=run2.started_at + timedelta(seconds=i * 2),
            )
        )

    # ---- 3. the denial ---------------------------------------------------
    inc3 = Incident(
        id="INC-2C55E80B",
        source="cloud-monitoring",
        signature="payments-api/OOMKilled",
        severity=Severity.high,
        status=IncidentStatus.remediating,
        workload=WorkloadRef(namespace="demo", name="payments-api"),
        title="payments-api: OOMKilled, 4 restarts",
        opened_at=now - timedelta(minutes=22),
    )
    await store.put_incident(inc3)

    run3 = Run(
        id=_rid(),
        incident_id=inc3.id,
        agent="remediator",
        model="gemini-3.7-flash",
        status=RunStatus.ok,
        started_at=inc3.opened_at + timedelta(minutes=2),
        ended_at=inc3.opened_at + timedelta(minutes=3),
        prompt_tokens=2100,
        candidates_tokens=310,
        total_tokens=2410,
        tool_calls=2,
        outcome="patch proposed after a denied shortcut",
    )
    await store.put_run(run3)

    await store.append_audit(
        AuditRecord(
            id=_rid(),
            run_id=run3.id,
            incident_id=inc3.id,
            agent="remediator",
            tool="read_repo_file",
            args_redacted={"path": "apps/payments-api/deployment.yaml"},
            decision=Decision.allow,
            latency_ms=164,
            ts=run3.started_at,
        )
    )
    await store.append_audit(
        AuditRecord(
            id=_rid(),
            run_id=run3.id,
            incident_id=inc3.id,
            agent="remediator",
            tool="delete_workload",
            args_redacted={"namespace": "demo", "name": "payments-api"},
            decision=Decision.deny,
            reason=(
                "tool 'delete_workload' is not in the manifest allow-list for agent "
                "'remediator' (allowed: propose_patch, read_repo_file)"
            ),
            latency_ms=2,
            ts=run3.started_at + timedelta(seconds=9),
        )
    )
    await store.append_audit(
        AuditRecord(
            id=_rid(),
            run_id=run3.id,
            incident_id=inc3.id,
            agent="remediator",
            tool="propose_patch",
            args_redacted={
                "namespace": "demo",
                "files": ["apps/payments-api/deployment.yaml"],
                "title": "fix(payments-api): raise memory limit to 512Mi",
            },
            decision=Decision.allow,
            latency_ms=1310,
            ts=run3.started_at + timedelta(seconds=24),
        )
    )
