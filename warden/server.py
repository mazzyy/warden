"""The standalone runtime, for running the ingest endpoints on their own.

    uvicorn warden.server:app --port 8081

An alert reaches Pub/Sub, Pub/Sub pushes here, and the fleet wakes up. Nobody
runs a script. That is the difference between a demo and a system, and it is
what the hackathon means by agents that "run in the background".

The routes themselves live in `warden/ingest.py`, because the deployed service
mounts the same router alongside the dashboard: one Cloud Run service, one
origin, one URL for the GitHub webhook to point at. This module exists for
local use, where running the runtime separately from the dashboard is often
what you want — two terminals, two logs.

ADK ships its own Pub/Sub trigger (`trigger_sources=["pubsub"]`, which registers
`/apps/{app}/trigger/pubsub`), and it is fine when the agent's side effects are
the point. It is not used here for two reasons: its response model is
`{status: success|error}` and never returns the agent's output, and it
auto-creates ephemeral sessions we cannot attach a policy plugin or an audit
trail to. Calling Runner ourselves keeps every run governed and inspectable.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from warden import ingest
from warden.config import settings
from warden.control_plane.jsonl_store import JsonlStore
from warden.control_plane.registry import load_all
from warden.control_plane.store import FirestoreStore, InMemoryStore

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("warden.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = settings()

    # Deployed this is Firestore. Locally it should be `jsonl`, so the server,
    # `make demo-live` and `make dashboard` all see the same incidents — a
    # webhook that verifies an incident the dashboard cannot see is only half
    # connected.
    kind = os.environ.get("WARDEN_STORE", "firestore")
    if kind == "memory":
        store = InMemoryStore()
    elif kind == "jsonl":
        store = JsonlStore()
    else:
        store = FirestoreStore(project=s.gcp_project)

    ingest.configure(store=store, fleet=load_all(s.manifest_dir))
    log.info("warden runtime ready — store=%s estate=%s", kind, s.estate_adapter)
    yield


app = FastAPI(title="Warden runtime", lifespan=lifespan)
app.include_router(ingest.router)


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "estate": settings().estate_adapter,
        **ingest.status(),
    }
