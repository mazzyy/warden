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
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel

from warden.agents.orchestrator import handle_incident, verify_merged_incident
from warden.config import configure_genai_env, resolve_github_token, settings
from warden.control_plane.jsonl_store import JsonlStore
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
    # Deployed this is Firestore. Locally it should be `jsonl`, so the server,
    # `make demo-live` and `make dashboard` all see the same incidents — a
    # webhook that verifies an incident the dashboard cannot see is only half
    # connected.
    kind = os.environ.get("WARDEN_STORE", "firestore")
    if kind == "memory":
        _ctx["store"] = InMemoryStore()
    elif kind == "jsonl":
        _ctx["store"] = JsonlStore()
    else:
        _ctx["store"] = FirestoreStore(project=s.gcp_project)
    log.info("store: %s", kind)
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


def _signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of GitHub's X-Hub-Signature-256.

    This endpoint starts agent runs. Without a signature check it is a remote
    trigger for the fleet available to anyone who learns the URL — which, for a
    service whose whole subject is bounded authority, is not a tidiness issue.
    """
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256=") :])


async def _verify_after_merge(pr_url: str) -> None:
    """Run the Verifier alone. Deliberately NOT the whole pipeline.

    This used to call `handle_incident(..., verify=True)`, which starts at
    Triage. On a merge that is actively wrong: the fleet would open a fresh
    incident for a workload that was just fixed, re-diagnose it, and could
    propose a second pull request for a fault that no longer exists. The merge
    is the END of an incident, not the start of one.
    """
    models = None
    if settings().use_fixtures:
        from warden.agents.fixtures import scripted_models

        models = scripted_models()

    try:
        incident, run = await verify_merged_incident(
            fleet=_ctx["fleet"],
            toolbox=_toolbox({}),
            store=_ctx["store"],
            pr_url=pr_url,
            models=models,
        )
        if incident is None:
            log.warning("nothing to verify for %s", pr_url)
            return
        log.info(
            "%s -> %s after merge (%d tokens)",
            incident.id,
            incident.status,
            run.run.total_tokens if run else 0,
        )
    except Exception:
        log.exception("post-merge verification failed for %s", pr_url)


@app.post("/webhook/github")
async def github_webhook(request: Request, background: BackgroundTasks) -> dict:
    """The other half of the loop: a human merged the pull request.

    Only a merged pull request on a warden branch triggers verification. A
    closed-without-merge pull request means the reviewer rejected the fix, and
    verifying a change that was never applied would have the Verifier open a
    revert for nothing.
    """
    raw = await request.body()

    secret = settings().github_webhook_secret
    if not secret:
        # Fail closed. An unauthenticated endpoint that runs agents is worse
        # than one that does not work, because the second failure is loud.
        log.error("GITHUB_WEBHOOK_SECRET is not set — refusing webhook deliveries")
        raise HTTPException(503, "webhook secret not configured")

    if not _signature_ok(secret, raw, request.headers.get("X-Hub-Signature-256")):
        log.warning("rejected a webhook delivery with a bad or missing signature")
        raise HTTPException(401, "bad signature")

    event = request.headers.get("X-GitHub-Event", "")
    body = json.loads(raw)

    if event == "ping":
        return {"status": "pong"}
    if event != "pull_request" or body.get("action") != "closed":
        return {"status": "ignored", "event": event}

    pr = body.get("pull_request", {})
    if not pr.get("merged"):
        return {"status": "ignored", "reason": "closed without merging"}

    branch = pr.get("head", {}).get("ref", "")
    if not branch.startswith("warden/"):
        return {"status": "ignored", "reason": f"not a warden branch: {branch}"}

    pr_url = pr.get("html_url", "")
    log.info("PR #%s merged (%s) — verifying", pr.get("number"), branch)
    background.add_task(_verify_after_merge, pr_url)
    return {"status": "accepted", "pr": pr.get("number")}


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "estate": settings().estate_adapter,
        "github": "live" if not _ctx["github"].dry_run else "dry-run",
        "webhook": "armed" if settings().github_webhook_secret else "NOT CONFIGURED",
        "agents": sorted(_ctx["fleet"]),
    }
