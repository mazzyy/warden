"""The dashboard API — and the URL judges will actually open.

One Cloud Run service serves both this API and the built React SPA. One service,
one deploy, no CORS, nothing extra to break on the day you record.

    make dashboard        # local, seeded with demo data
    uvicorn warden.dashboard.api:app --port 8080
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from warden import ingest
from warden.config import credential_mode, settings
from warden.control_plane.budget import estimate_usd
from warden.control_plane.jsonl_store import JsonlStore
from warden.control_plane.registry import load_all
from warden.control_plane.store import FirestoreStore, InMemoryStore, Store
from warden.models import Decision, FleetState, IncidentStatus, RunStatus

# Configure the root logger here, not only in server.py.
#
# server.py has always done this, and this module never did — which was
# invisible until the two were consolidated and Cloud Run started serving
# `warden.dashboard.api:app` instead. The root logger then sat at WARNING, so
# every log.info() in ingest, the orchestrator, the runtime and the policy
# proxy was dropped. Uvicorn configures its own logging, so the access log
# still appeared: the service looked like it was answering requests and doing
# nothing else, while the fleet ran silently behind it.
#
# An audit trail that reaches Firestore but not the logs is half an audit
# trail, and the missing half is the one you read while something is on fire.
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

log = logging.getLogger("warden.dashboard")

WEB_DIST = Path(__file__).parent / "web" / "dist"

_state: dict[str, Any] = {}


def get_store() -> Store:
    return _state["store"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = settings()
    kind = os.environ.get("WARDEN_STORE", "jsonl")

    # jsonl by default, because the dashboard and `make demo-live` are separate
    # processes. With InMemoryStore they each had their own, so the dashboard
    # could only ever render seed.py — three invented incidents, one of them
    # linking to a pull request that does not exist. Everything else in this
    # project is real against real infrastructure; the screen was the exception.
    if kind == "firestore":
        _state["store"] = FirestoreStore(project=s.gcp_project)
        _state["seeded_ids"] = set()
        log.info("dashboard: Firestore store, project %s", s.gcp_project)
    elif kind == "memory":
        _state["store"] = InMemoryStore()
        from warden.dashboard.seed import seed

        await seed(_state["store"])
        _state["seeded_ids"] = {i.id for i in await _state["store"].list_incidents(limit=50)}
        log.info("dashboard: in-memory store, seeded")
    else:
        _state["store"] = JsonlStore()
        existing = await _state["store"].list_incidents(limit=1)
        _state["seeded_ids"] = set()
        if not existing:
            # Nothing has run yet. Seed so the dashboard is not an empty page —
            # but flag it, and the UI says SAMPLE DATA in the header until a
            # real incident arrives. An unlabelled demo fixture on screen is
            # the same lie as an empty pull request that claims to fix
            # something: it looks like the system worked.
            from warden.dashboard.seed import seed

            await seed(_state["store"])
            # Remember WHICH incidents are fixtures rather than a boolean
            # "we seeded once". The boolean was decided at startup and never
            # revisited, so the banner went on claiming sample data long after
            # a real run had replaced it — a label that lies about being a
            # label is worse than no label.
            _state["seeded_ids"] = {i.id for i in await _state["store"].list_incidents(limit=50)}
            log.info("dashboard: jsonl store at %s, empty — seeded", _state["store"].dir)
        else:
            log.info("dashboard: jsonl store at %s, real data", _state["store"].dir)

    _state["fleet"] = load_all(s.manifest_dir)

    # Hand the ingest router the same store and fleet. One deployed service
    # answers the GitHub webhook, receives Pub/Sub alerts and serves this
    # screen — which is what makes the webhook URL stable without a tunnel,
    # and what guarantees the incident the webhook closes is the incident the
    # dashboard is showing.
    ingest.configure(store=_state["store"], fleet=_state["fleet"])
    yield


app = FastAPI(title="Warden", version="0.1.0", lifespan=lifespan)

# Mounted before the SPA catch-all, which claims every remaining path.
app.include_router(ingest.router)


# --------------------------------------------------------------------------
# Health — W-107. Live probes, not expiry parsing: probing catches revocation,
# rotation, quota exhaustion and expiry at once, where reading an expiry date
# catches only the last of those.
#
# The same endpoint is behind a Cloud Monitoring uptime check, so it also
# guards the demo URL through the Sept 1 - Oct 1 judging window.
# --------------------------------------------------------------------------


# Two paths, one handler.
#
# On Cloud Run, GET /healthz never reaches this container. Google's front end
# answers it with its own 404 page — no `server: Google Frontend` header, no
# x-cloud-trace-context, none of the headers a response from here carries. `/`,
# `/api/*`, /pubsub, /webhook/github and unrecognised paths all route normally;
# that one path does not. Whatever the reason, it is above us and we cannot
# fix it from inside the app.
#
# So /healthz stays — it works locally, in Docker, and anywhere that is not
# Cloud Run — and /api/healthz is the alias that is reachable in production,
# under a prefix already proven to route.
@app.get("/healthz")
@app.get("/api/healthz")
async def healthz() -> JSONResponse:
    s = settings()
    store = get_store()
    checks: dict[str, dict[str, Any]] = {}

    async def probe(name: str, coro, critical: bool = True) -> None:
        try:
            detail = await asyncio.wait_for(coro, timeout=5)
            checks[name] = {"ok": True, "detail": detail, "critical": critical}
        except Exception as exc:
            checks[name] = {
                "ok": False,
                "detail": f"{type(exc).__name__}: {exc}"[:200],
                "critical": critical,
            }

    async def _store_probe() -> str:
        state = await store.get_fleet_state()
        return f"reachable · kill_switch={state.kill_switch}"

    async def _budget_probe() -> str:
        runs = await store.list_runs(limit=500)
        spent = sum(estimate_usd(r.model, r.prompt_tokens, r.candidates_tokens) for r in runs)
        ratio = spent / s.budget_usd_cap if s.budget_usd_cap else 0.0
        if ratio > s.budget_warn_ratio:
            raise RuntimeError(f"${spent:.4f} is {ratio:.0%} of the ${s.budget_usd_cap} cap")
        return f"${spent:.4f} of ${s.budget_usd_cap} ({ratio:.0%})"

    await probe("store", _store_probe())
    await probe("budget", _budget_probe())

    checks["credentials"] = {"ok": True, "detail": credential_mode(), "critical": False}
    for name, detail in ingest.status().items():
        checks[name] = {"ok": True, "detail": detail, "critical": False}
    checks["estate"] = {"ok": True, "detail": f"adapter={s.estate_adapter}", "critical": False}

    fleet_state = await store.get_fleet_state()
    healthy = all(c["ok"] for c in checks.values() if c["critical"])

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "kill_switch": fleet_state.kill_switch,
            "checks": checks,
        },
    )


# --------------------------------------------------------------------------
# Fleet
# --------------------------------------------------------------------------


@app.get("/api/fleet")
async def fleet() -> dict:
    store = get_store()
    state = await store.get_fleet_state()
    runs = await store.list_runs(limit=500)

    agents = []
    for name, manifest in sorted(_state["fleet"].items()):
        mine = [r for r in runs if r.agent == name]
        agents.append(
            {
                "name": name,
                "description": manifest.metadata.description,
                "model": manifest.spec.model,
                "tools": manifest.spec.tools,
                "scopes": manifest.spec.scopes,
                "approval": manifest.spec.approval,
                "blastRadius": {
                    "namespace": manifest.spec.blast_radius.namespace,
                    "maxFilesPerPatch": manifest.spec.blast_radius.max_files_per_patch,
                },
                "budget": {
                    "maxTokensPerRun": manifest.spec.budget.max_tokens_per_run,
                    "maxToolCalls": manifest.spec.budget.max_tool_calls,
                },
                "runs": len(mine),
                "tokens": sum(r.total_tokens for r in mine),
                "costUsd": sum(
                    estimate_usd(r.model, r.prompt_tokens, r.candidates_tokens) for r in mine
                ),
            }
        )

    return {
        "agents": agents,
        "killSwitch": state.kill_switch,
        "totals": {
            "runs": len(runs),
            "tokens": sum(r.total_tokens for r in runs),
            "costUsd": sum(
                estimate_usd(r.model, r.prompt_tokens, r.candidates_tokens) for r in runs
            ),
        },
    }


# --------------------------------------------------------------------------
# Incidents and runs
# --------------------------------------------------------------------------


@app.get("/api/incidents")
async def incidents() -> dict:
    store = get_store()
    items = await store.list_incidents(limit=50)
    runs = await store.list_runs(limit=500)
    audit = await store.list_audit(limit=2000)

    out = []
    for inc in items:
        inc_runs = [r for r in runs if r.incident_id == inc.id]
        inc_audit = [a for a in audit if a.incident_id == inc.id]
        out.append(
            {
                **inc.model_dump(mode="json"),
                "runCount": len(inc_runs),
                "toolCalls": len(inc_audit),
                "denials": sum(1 for a in inc_audit if a.decision is Decision.deny),
                "tokens": sum(r.total_tokens for r in inc_runs),
            }
        )
    return {"incidents": out}


@app.get("/api/incidents/{incident_id}")
async def incident_detail(incident_id: str) -> dict:
    store = get_store()
    inc = await store.get_incident(incident_id)
    if inc is None:
        raise HTTPException(404, f"no incident {incident_id}")

    runs = [r for r in await store.list_runs(limit=500) if r.incident_id == incident_id]
    audit = [a for a in await store.list_audit(limit=2000) if a.incident_id == incident_id]

    timeline = []
    for run in sorted(runs, key=lambda r: r.started_at):
        calls = [a for a in audit if a.run_id == run.id]
        timeline.append(
            {
                **run.model_dump(mode="json"),
                "costUsd": estimate_usd(run.model, run.prompt_tokens, run.candidates_tokens),
                "calls": [a.model_dump(mode="json") for a in sorted(calls, key=lambda a: a.ts)],
            }
        )

    return {"incident": inc.model_dump(mode="json"), "runs": timeline}


# --------------------------------------------------------------------------
# The live view. One poll, everything the operations screen needs.
#
# Deliberately one endpoint rather than four: the screen shows a single moment
# in an incident, and four independent polls can disagree about which moment
# that is — an agent shown mid-tool-call while the pipeline has already moved
# past it. Assembling it server-side from one read of the store makes the whole
# frame consistent by construction.
# --------------------------------------------------------------------------

# The stages a change passes through, and which of them an agent can act on.
# The last three exist to make the point that the agent's authority stops: the
# fleet cannot advance the pipeline past `review`.
PIPELINE = [
    ("alert", "Alert", "signal received"),
    ("triage", "Triage", "severity, deduplication"),
    ("diagnose", "Diagnose", "evidence and root cause"),
    ("remediate", "Patch", "propose a pull request"),
    ("review", "Human review", "approval required"),
    ("apply", "Apply", "CI rolls it out"),
    ("verify", "Verify", "SLOs recover"),
]

# Which pipeline stage each incident status is *currently working on*.
STATUS_STAGE = {
    IncidentStatus.open: "triage",
    IncidentStatus.diagnosing: "diagnose",
    IncidentStatus.remediating: "remediate",
    IncidentStatus.awaiting_merge: "review",
    IncidentStatus.verifying: "verify",
    IncidentStatus.resolved: "done",
    IncidentStatus.abandoned: "stopped",
}

AGENT_STAGE = {
    "triage": "triage",
    "diagnostician": "diagnose",
    "remediator": "remediate",
    "verifier": "verify",
}

RUN_STATE = {
    RunStatus.running: "working",
    RunStatus.ok: "done",
    RunStatus.failed: "failed",
    RunStatus.budget_exceeded: "failed",
    RunStatus.killed: "blocked",
}


@app.get("/api/live")
async def live() -> dict:
    store = get_store()
    fleet_state = await store.get_fleet_state()
    incidents = await store.list_incidents(limit=1)
    incident = incidents[0] if incidents else None

    runs = await store.list_runs(incident_id=incident.id if incident else None, limit=100)
    audit = await store.list_audit(limit=500)
    if incident:
        audit = [a for a in audit if a.incident_id == incident.id]

    stage = STATUS_STAGE.get(incident.status, "triage") if incident else "idle"
    order = [key for key, _, _ in PIPELINE]

    if stage == "stopped":
        # An abandoned incident stopped SOMEWHERE, and it matters where. The
        # first version marked stage zero, so a run that Triage deliberately
        # closed rendered as "Alert: stopped" — blaming the pager for a
        # decision an agent made. The last agent that actually ran is the one
        # that stopped it.
        ran = [AGENT_STAGE[r.agent] for r in runs if r.agent in AGENT_STAGE]
        reached = max((order.index(s) for s in ran), default=1)
    elif stage == "done":
        reached = len(order)
    elif stage in order:
        reached = order.index(stage)
    else:
        reached = 0

    agents = []
    for name, manifest in sorted(_state["fleet"].items()):
        mine = [r for r in runs if r.agent == name]
        run = mine[-1] if mine else None
        calls = [a for a in audit if run and a.run_id == run.id]
        denials = [a for a in calls if a.decision is Decision.deny]

        state = RUN_STATE.get(run.status, "idle") if run else "idle"
        if fleet_state.kill_switch and state == "working":
            state = "blocked"

        budget = manifest.spec.budget
        agents.append(
            {
                "name": name,
                "stage": AGENT_STAGE.get(name, ""),
                "description": manifest.metadata.description,
                "model": manifest.spec.model,
                "state": state,
                "runId": run.id if run else None,
                "startedAt": run.started_at.isoformat() if run else None,
                "endedAt": run.ended_at.isoformat() if run and run.ended_at else None,
                "outcome": run.outcome if run else "",
                "tokens": run.total_tokens if run else 0,
                # The audit count, not the budget ledger's. They agree in a
                # healthy run, and when they disagree the audit log is the one
                # that can be checked against what actually happened — which is
                # also the number the feed on the right is showing. Two
                # different counts for the same thing on one screen is how you
                # lose an audience mid-demo.
                "toolCalls": len(calls),
                "costUsd": estimate_usd(run.model, run.prompt_tokens, run.candidates_tokens)
                if run
                else 0.0,
                # The tool the agent is holding right now — what the character
                # is visibly *doing*, rather than a count of what it has done.
                "currentTool": calls[-1].tool if calls and state == "working" else None,
                "denials": len(denials),
                "lastDenial": denials[-1].reason if denials else None,
                "calls": [
                    {
                        "tool": a.tool,
                        "decision": a.decision.value,
                        "reason": a.reason,
                        "latencyMs": a.latency_ms,
                        "ts": a.ts.isoformat(),
                        "args": a.args_redacted,
                        # What the tool actually returned. This is what makes
                        # the audit trail checkable: a diagnosis can be read
                        # against the evidence it was built from, rather than
                        # taken on trust because a call is listed as allowed.
                        "result": a.result_preview,
                        "resultTruncated": a.result_truncated,
                    }
                    for a in calls
                ],
                "budget": {
                    "maxTokens": budget.max_tokens_per_run,
                    "maxToolCalls": budget.max_tool_calls,
                },
                "tokenPct": round(
                    100 * (run.total_tokens if run else 0) / max(budget.max_tokens_per_run, 1)
                ),
                "granted": manifest.spec.tools,
            }
        )

    pipeline = []
    for i, (key, label, detail) in enumerate(PIPELINE):
        if stage == "stopped":
            node = "stopped" if i == reached else ("done" if i < reached else "pending")
        elif i < reached or stage == "done":
            node = "done"
        elif i == reached:
            node = "active"
        else:
            node = "pending"
        pipeline.append({"key": key, "label": label, "detail": detail, "state": node})

    return {
        "killSwitch": fleet_state.kill_switch,
        # Is the incident ON SCREEN a fixture? Recomputed every poll, so the
        # banner clears the moment a real incident arrives.
        "demoData": bool(incident and incident.id in _state.get("seeded_ids", set())),
        "stage": stage,
        "pipeline": pipeline,
        "incident": (
            {
                **incident.model_dump(mode="json"),
                "toolCalls": len(audit),
                "denials": sum(1 for a in audit if a.decision is Decision.deny),
                "tokens": sum(r.total_tokens for r in runs),
                "costUsd": sum(
                    estimate_usd(r.model, r.prompt_tokens, r.candidates_tokens) for r in runs
                ),
            }
            if incident
            else None
        ),
        "agents": agents,
        "feed": [
            {
                "agent": a.agent,
                "tool": a.tool,
                "decision": a.decision.value,
                "reason": a.reason,
                "latencyMs": a.latency_ms,
                "ts": a.ts.isoformat(),
            }
            for a in audit[-40:]
        ],
    }


@app.get("/api/audit")
async def audit_log(limit: int = 200) -> dict:
    records = await get_store().list_audit(limit=limit)
    return {"audit": [a.model_dump(mode="json") for a in records]}


# --------------------------------------------------------------------------
# Kill switch — the demo beat. One call drains the fleet mid-incident, and the
# policy engine refuses every dispatch while it is set.
# --------------------------------------------------------------------------


class KillSwitchBody(BaseModel):
    engaged: bool
    note: str = ""


@app.post("/api/kill-switch")
async def kill_switch(body: KillSwitchBody) -> dict:
    from warden.models import utcnow

    store = get_store()
    state = FleetState(
        kill_switch=body.engaged,
        drained_at=utcnow() if body.engaged else None,
        note=body.note or ("drained from dashboard" if body.engaged else "released"),
    )
    await store.set_fleet_state(state)
    log.warning("kill switch %s", "ENGAGED" if body.engaged else "released")
    return {"killSwitch": state.kill_switch, "note": state.note}


@app.get("/api/policy-matrix")
async def policy_matrix() -> dict:
    """Every agent against every tool. The governance story, as data."""
    from warden.control_plane import policy
    from warden.probe import TOOL_PROBE_ARGS
    from warden.tools import catalog

    fleet_manifests = _state["fleet"]
    rows = []
    for tool_name in sorted(catalog.CATALOG):
        spec = catalog.get(tool_name)
        cells = {}
        for agent, manifest in sorted(fleet_manifests.items()):
            result = policy.evaluate(manifest, tool_name, TOOL_PROBE_ARGS.get(tool_name, {}))
            cells[agent] = {
                "allowed": result.allowed,
                "reason": result.reason,
            }
        rows.append({"tool": tool_name, "mutating": spec.mutating, "cells": cells})
    return {"agents": sorted(fleet_manifests), "rows": rows}


# --------------------------------------------------------------------------
# The SPA. Mounted last so it never shadows an /api route.
# --------------------------------------------------------------------------

if WEB_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        candidate = WEB_DIST / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")

else:  # pragma: no cover - only when the SPA has not been built

    @app.get("/")
    async def not_built() -> dict:
        return {
            "error": "dashboard SPA not built",
            "fix": "cd warden/dashboard/web && npm install && npm run build",
            "api": ["/healthz", "/api/fleet", "/api/incidents", "/api/policy-matrix"],
        }
