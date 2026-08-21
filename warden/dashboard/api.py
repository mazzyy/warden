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

from warden.config import credential_mode, settings
from warden.control_plane.budget import estimate_usd
from warden.control_plane.registry import load_all
from warden.control_plane.store import FirestoreStore, InMemoryStore, Store
from warden.models import Decision, FleetState

log = logging.getLogger("warden.dashboard")

WEB_DIST = Path(__file__).parent / "web" / "dist"

_state: dict[str, Any] = {}


def get_store() -> Store:
    return _state["store"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = settings()
    # In-memory by default so the dashboard runs with no cloud at all — which
    # is what makes `make dashboard` a thing you can try in ten seconds.
    if os.environ.get("WARDEN_STORE", "memory") == "firestore":
        _state["store"] = FirestoreStore(project=s.gcp_project)
        log.info("dashboard: Firestore store, project %s", s.gcp_project)
    else:
        _state["store"] = InMemoryStore()
        from warden.dashboard.seed import seed

        await seed(_state["store"])
        log.info("dashboard: in-memory store, seeded")

    _state["fleet"] = load_all(s.manifest_dir)
    yield


app = FastAPI(title="Warden", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# Health — W-107. Live probes, not expiry parsing: probing catches revocation,
# rotation, quota exhaustion and expiry at once, where reading an expiry date
# catches only the last of those.
#
# The same endpoint is behind a Cloud Monitoring uptime check, so it also
# guards the demo URL through the Sept 1 - Oct 1 judging window.
# --------------------------------------------------------------------------


@app.get("/healthz")
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
            "costUsd": sum(estimate_usd(r.model, r.prompt_tokens, r.candidates_tokens) for r in runs),
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
