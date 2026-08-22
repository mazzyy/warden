"""The event-driven entry point.

    uvicorn warden.server:app --port 8081

An alert reaches Pub/Sub, Pub/Sub pushes here, and the fleet wakes up. Nobody
runs a script. That is the difference between a demo and a system, and it is
what the hackathon means by agents that "run in the background".

ADK ships its own Pub/Sub trigger (`trigger_sources=["pubsub"]`, which registers
`/apps/{app}/trigger/pubsub`), and it is fine when the agent's side effects are
the point. It is not used here for two reasons: its response model is
`{status: success|error}` and never returns the agent's output, and it
auto-creates ephemeral sessions we cannot attach a policy plugin or an audit
trail to. Calling Runner ourselves keeps every run governed and inspectable.

Two endpoints, because the loop has two halves that are hours apart:

    POST /pubsub          an alert fired    -> triage, diagnose, propose a PR
    POST /webhook/github  a human merged it -> verify, or open a revert
"""

from __future__ import annotations

import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from pydantic import BaseModel

from warden.agents.orchestrator import handle_incident
from warden.config import configure_genai_env, resolve_github_token, settings
from warden.control_plane.registry import load_all
from warden.control_plane.store import FirestoreStore, InMemoryStore
from warden.estate.base import build_adapter
from warden.tools.github_client import GitHubClient
from warden.tools.toolbox import ToolBox

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("warden.server")

_ctx: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = settings()
    configure_genai_env()

    _ctx["fleet"] = load_all(s.manifest_dir)
    _ctx["store"] = (
        InMemoryStore()
        if os.environ.get("WARDEN_STORE", "firestore") == "memory"
        else FirestoreStore(project=s.gcp_project)
    )
    _ctx["estate"] = build_adapter(s.estate_adapter)

    token = resolve_github_token()
    _ctx["github"] = GitHubClient(
        repo_full_name=s.gitops_full_name,
        token=token,
        base_branch=s.gitops_base_branch,
    )
    log.info(
        "warden ready — estate=%s github=%s",
        s.estate_adapter,
        "live" if token else "DRY RUN (no token)",
    )
    yield


app = FastAPI(title="Warden runtime", lifespan=lifespan)


def _toolbox(alert: dict) -> ToolBox:
    return ToolBox(
        estate=_ctx["estate"],
        store=_ctx["store"],
        github=_ctx["github"],
        namespace=settings().estate_namespace,
        alert_context=alert,
    )


class PubSubEnvelope(BaseModel):
    message: dict
    subscription: str | None = None


@app.post("/pubsub")
async def pubsub(envelope: PubSubEnvelope, background: BackgroundTasks) -> dict:
    """Pub/Sub push. Acks immediately and works in the background.

    An incident takes tens of seconds; Pub/Sub's ack deadline is shorter than
    that, and a late ack means redelivery — which would run the fleet twice on
    the same alert and open two pull requests. Ack first, work after.
    """
    raw = envelope.message.get("data")
    payload = base64.b64decode(raw).decode() if raw else "{}"
    try:
        alert = json.loads(payload)
    except json.JSONDecodeError:
        alert = {"source": "pubsub", "title": payload[:200], "signature": "unparsed"}

    alert.setdefault("source", "pubsub")
    log.info("alert received: %s", alert.get("title", "<untitled>"))

    background.add_task(_handle, alert)
    return {"status": "accepted", "title": alert.get("title")}


async def _handle(alert: dict, verify: bool = False) -> None:
    # WARDEN_USE_FIXTURES lets the whole event-driven path be exercised with no
    # model credential and no spend — which is how the Pub/Sub wiring gets
    # tested without waiting on Gemini. Never set in the deployed service.
    models = None
    if settings().use_fixtures:
        from warden.agents.fixtures import scripted_models

        models = scripted_models()
        log.warning("FIXTURES ACTIVE — scripted models, not real Gemini")

    try:
        result = await handle_incident(
            alert=alert,
            fleet=_ctx["fleet"],
            toolbox=_toolbox(alert),
            store=_ctx["store"],
            models=models,
            verify=verify,
        )
        log.info(
            "%s -> %s (%d tokens, %d tool calls)",
            result.incident.id,
            result.incident.status,
            result.total_tokens,
            result.total_tool_calls,
        )
    except Exception:
        # Never re-raise into a background task: the exception would vanish into
        # the event loop's exception handler and the incident would look like it
        # simply stopped. Log it where the dashboard and Cloud Logging can see it.
        log.exception("incident handling failed for %r", alert.get("title"))


@app.post("/webhook/github")
async def github_webhook(request: Request, background: BackgroundTasks) -> dict:
    """The other half of the loop: a human merged the pull request.

    Only a merged PR on the base branch triggers verification. A closed-without-
    merge PR means the reviewer rejected the fix, and verifying a change that
    was never applied would have the Verifier open a revert for nothing.
    """
    event = request.headers.get("X-GitHub-Event", "")
    body = await request.json()

    if event != "pull_request" or body.get("action") != "closed":
        return {"status": "ignored", "event": event}

    pr = body.get("pull_request", {})
    if not pr.get("merged"):
        return {"status": "ignored", "reason": "closed without merging"}

    branch = pr.get("head", {}).get("ref", "")
    if not branch.startswith("warden/"):
        return {"status": "ignored", "reason": f"not a warden branch: {branch}"}

    log.info("PR #%s merged (%s) — verifying", pr.get("number"), branch)
    alert = {
        "source": "github",
        "signature": "post-merge-verification",
        "title": f"verify after merge of PR #{pr.get('number')}",
        "workload": "checkout-svc",
        "namespace": settings().estate_namespace,
        "pr_url": pr.get("html_url"),
    }
    background.add_task(_handle, alert, True)
    return {"status": "accepted", "pr": pr.get("number")}


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "estate": settings().estate_adapter,
        "github": "live" if not _ctx["github"].dry_run else "dry-run",
        "agents": sorted(_ctx["fleet"]),
    }
