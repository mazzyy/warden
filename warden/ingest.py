"""The two ways the world wakes the fleet, as a mountable router.

    POST /pubsub          an alert fired    -> triage, diagnose, propose a PR
    POST /webhook/github  a human merged it -> verify, or open a revert

These used to live directly on `warden/server.py`, a second FastAPI app beside
the dashboard's. Two apps meant two Cloud Run services, two URLs and two deploys
— and the webhook URL was the one that had to be stable and public, which is the
one a laptop tunnel is worst at providing.

So they are a router now. `warden/server.py` still mounts it for a standalone
runtime, and the dashboard mounts it too, which is what lets a single deployed
service answer the webhook, receive alerts, and serve the operations screen from
one origin.

WHY THE CONTEXT IS LAZY

`make dashboard` has to work on a clean clone with no cloud project, no model
credential and no cluster. But this router needs an estate adapter, a GitHub
client and model credentials to do anything. Building those eagerly at import
or at startup would make the dashboard refuse to boot for someone who only
wanted to look at it.

So the context is built on the first request that actually needs it, and a
failure to build it returns 503 with the reason rather than taking the whole
service down. The store and fleet are injected by whichever app mounts the
router, so the dashboard and the webhook are always looking at the same
incidents — a webhook that closes an incident the dashboard cannot see is only
half connected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from warden.agents.orchestrator import handle_incident, verify_merged_incident
from warden.config import configure_genai_env, resolve_github_credential, settings
from warden.control_plane.registry import load_all
from warden.control_plane.store import Store
from warden.estate.base import build_adapter
from warden.tools.github_client import GitHubClient
from warden.tools.toolbox import ToolBox

log = logging.getLogger("warden.ingest")

router = APIRouter()

_ctx: dict[str, Any] = {}


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------


def configure(*, store: Store | None = None, fleet: dict | None = None) -> None:
    """Share the mounting app's store and fleet.

    Called from a lifespan. Anything not provided is built on first use.
    """
    if store is not None:
        _ctx["store"] = store
    if fleet is not None:
        _ctx["fleet"] = fleet


def _context() -> dict[str, Any]:
    """Build what is missing, once. Raises on a genuinely unusable config."""
    s = settings()

    if "fleet" not in _ctx:
        _ctx["fleet"] = load_all(s.manifest_dir)

    if "store" not in _ctx:
        from warden.control_plane.jsonl_store import JsonlStore

        _ctx["store"] = JsonlStore()
        log.warning("ingest built its own store — the dashboard will not see these incidents")

    if "estate" not in _ctx:
        _ctx["estate"] = build_adapter(s.estate_adapter)

    if "github" not in _ctx:
        credential = resolve_github_credential()
        _ctx["github"] = GitHubClient(
            repo_full_name=s.gitops_full_name,
            base_branch=s.gitops_base_branch,
            credential=credential,
        )
        log.info("ingest github identity: %s", _ctx["github"].identity)

    if not _ctx.get("genai"):
        configure_genai_env()
        _ctx["genai"] = True

    return _ctx


def _ready() -> dict[str, Any]:
    try:
        return _context()
    except Exception as exc:
        # 503, not 500: the service is up and the dashboard still works. This
        # endpoint specifically cannot run agents, and the reason belongs in
        # the response rather than only in a log nobody is tailing.
        log.exception("ingest context unavailable")
        raise HTTPException(503, f"agent runtime unavailable: {type(exc).__name__}: {exc}") from exc


def _toolbox(alert: dict) -> ToolBox:
    ctx = _ctx
    return ToolBox(
        estate=ctx["estate"],
        store=ctx["store"],
        github=ctx["github"],
        namespace=settings().estate_namespace,
        alert_context=alert,
    )


def _models() -> dict | None:
    if not settings().use_fixtures:
        return None
    from warden.agents.fixtures import scripted_models

    log.warning("FIXTURES ACTIVE — scripted models, not real Gemini")
    return scripted_models()


# --------------------------------------------------------------------------
# An alert fired
# --------------------------------------------------------------------------


class PubSubEnvelope(BaseModel):
    message: dict
    subscription: str | None = None


@router.post("/pubsub")
async def pubsub(envelope: PubSubEnvelope, background: BackgroundTasks) -> dict:
    """Pub/Sub push. Acks immediately and works in the background.

    An incident takes tens of seconds; Pub/Sub's ack deadline is shorter than
    that, and a late ack means redelivery — which would run the fleet twice on
    the same alert and open two pull requests. Ack first, work after.
    """
    _ready()

    raw = envelope.message.get("data")
    payload = base64.b64decode(raw).decode() if raw else "{}"
    try:
        alert = json.loads(payload)
    except json.JSONDecodeError:
        alert = {"source": "pubsub", "title": payload[:200], "signature": "unparsed"}

    alert.setdefault("source", "pubsub")
    log.info("alert received: %s", alert.get("title", "<untitled>"))

    background.add_task(_run_incident, alert)
    return {"status": "accepted", "title": alert.get("title")}


async def _run_incident(alert: dict) -> None:
    try:
        result = await handle_incident(
            alert=alert,
            fleet=_ctx["fleet"],
            toolbox=_toolbox(alert),
            store=_ctx["store"],
            models=_models(),
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
        # the event loop's handler and the incident would look like it simply
        # stopped. Log it where the dashboard and Cloud Logging can see it.
        log.exception("incident handling failed for %r", alert.get("title"))


# --------------------------------------------------------------------------
# A human merged it
# --------------------------------------------------------------------------


def signature_ok(secret: str, body: bytes, header: str | None) -> bool:
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
    try:
        incident, run = await verify_merged_incident(
            fleet=_ctx["fleet"],
            toolbox=_toolbox({}),
            store=_ctx["store"],
            pr_url=pr_url,
            models=_models(),
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


@router.post("/webhook/github")
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

    if not signature_ok(secret, raw, request.headers.get("X-Hub-Signature-256")):
        log.warning("rejected a webhook delivery with a bad or missing signature")
        raise HTTPException(401, "bad signature")

    event = request.headers.get("X-GitHub-Event", "")
    body = json.loads(raw)

    # Answer the ping before building the agent context. A ping arrives the
    # moment the hook is created, often before any credential is in place, and
    # failing it makes a correctly configured webhook look broken.
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

    _ready()
    pr_url = pr.get("html_url", "")
    log.info("PR #%s merged (%s) — verifying", pr.get("number"), branch)
    background.add_task(_verify_after_merge, pr_url)
    return {"status": "accepted", "pr": pr.get("number")}


def status() -> dict[str, str]:
    """What /healthz reports about this router."""
    return {
        "webhook": "armed" if settings().github_webhook_secret else "NOT CONFIGURED",
        "runtime": "ready" if "github" in _ctx else "lazy",
    }
