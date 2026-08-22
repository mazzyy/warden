"""The merge event, and what it is allowed to start.

Two bugs live in this file's history, and both are the same shape: an endpoint
that looked wired up and did the wrong thing quietly.

The first is that `/webhook/github` called `handle_incident(..., verify=True)`,
which begins at Triage. On a merge that is actively wrong. The merge is the END
of an incident, not the start of one — running the full pipeline against a
workload that was just fixed would open a fresh incident, re-diagnose a healthy
service, and could propose a second pull request for a fault that no longer
exists.

The second is that the endpoint had no signature check. It starts agent runs.
An unauthenticated one is a remote trigger for the fleet available to anyone
who learns the URL, which for a service whose subject is bounded authority is
not a tidiness issue.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from warden.agents.orchestrator import verify_merged_incident
from warden.control_plane.registry import load_all
from warden.control_plane.store import InMemoryStore
from warden.estate.fake import FakeAdapter
from warden.models import Incident, IncidentStatus, WorkloadRef
from warden.server import _signature_ok, app
from warden.tools.github_client import GitHubClient
from warden.tools.toolbox import ToolBox

SECRET = "a-shared-secret"
PR_URL = "https://github.com/mazzyy/estate-gitops/pull/6"


def _merged_body(branch="warden/fix-payment-endpoint", merged=True, url=PR_URL):
    return {
        "action": "closed",
        "pull_request": {
            "number": 6,
            "merged": merged,
            "html_url": url,
            "head": {"ref": branch},
        },
    }


def _post(client, body, *, secret=SECRET, event="pull_request", sign=True):
    raw = json.dumps(body).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if sign:
        digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"
    return client.post("/webhook/github", content=raw, headers=headers)


# --------------------------------------------------------------------------
# The signature
# --------------------------------------------------------------------------


def test_a_correct_signature_passes():
    body = b'{"hello": "world"}'
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert _signature_ok(SECRET, body, f"sha256={digest}") is True


def test_a_tampered_body_fails():
    body = b'{"hello": "world"}'
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert _signature_ok(SECRET, b'{"hello": "evil"}', f"sha256={digest}") is False


def test_the_wrong_secret_fails():
    body = b'{"hello": "world"}'
    digest = hmac.new(b"not-the-secret", body, hashlib.sha256).hexdigest()
    assert _signature_ok(SECRET, body, f"sha256={digest}") is False


def test_a_missing_header_fails():
    assert _signature_ok(SECRET, b"{}", None) is False


def test_an_unprefixed_digest_fails():
    """A bare hex digest is not a valid header, however correct the hash is."""
    body = b"{}"
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert _signature_ok(SECRET, body, digest) is False


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("WARDEN_STORE", "memory")
    monkeypatch.setenv("WARDEN_USE_FIXTURES", "1")
    monkeypatch.setenv("ESTATE_ADAPTER", "fake")
    # The server's lifespan calls configure_genai_env, which refuses to start
    # on the Vertex path without application default credentials. Point it at
    # the key path with a dummy key: the fixtures mean no model is ever called.
    monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "0")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-not-used")
    from warden.config import settings

    settings.cache_clear()
    with TestClient(app) as c:
        yield c
    settings.cache_clear()


def test_an_unsigned_delivery_is_refused(client):
    assert _post(client, _merged_body(), sign=False).status_code == 401


def test_a_delivery_signed_with_the_wrong_secret_is_refused(client):
    assert _post(client, _merged_body(), secret="wrong").status_code == 401


def test_a_ping_is_answered(client):
    r = _post(client, {"zen": "Non-blocking is better than blocking."}, event="ping")
    assert r.status_code == 200
    assert r.json()["status"] == "pong"


def test_a_merged_warden_branch_is_accepted(client):
    r = _post(client, _merged_body())
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"


def test_a_pull_request_closed_without_merging_is_ignored(client):
    """The reviewer rejected the fix. Verifying it would revert nothing."""
    r = _post(client, _merged_body(merged=False))
    assert r.json()["status"] == "ignored"


def test_a_branch_the_fleet_did_not_open_is_ignored(client):
    """A human's own pull request is not an incident of ours to close."""
    r = _post(client, _merged_body(branch="feature/new-checkout-ui"))
    assert r.json()["status"] == "ignored"


def test_an_unrelated_event_is_ignored(client):
    r = _post(client, {"ref": "refs/heads/main"}, event="push")
    assert r.json()["status"] == "ignored"


def test_healthz_says_whether_the_webhook_is_armed(client):
    assert client.get("/healthz").json()["webhook"] == "armed"


# --------------------------------------------------------------------------
# Matching the merge back to the right incident
# --------------------------------------------------------------------------


def _toolbox(store):
    return ToolBox(
        estate=FakeAdapter("healthy"),
        store=store,
        github=GitHubClient(repo_full_name="mazzyy/estate-gitops", token=None),
        alert_context={},
    )


async def _incident(store, ident, url, opened_offset=0):
    from datetime import timedelta

    from warden.models import utcnow

    inc = Incident(
        id=ident,
        source="cloud-monitoring",
        signature="checkout-svc/RolloutBlocked",
        title=f"incident {ident}",
        status=IncidentStatus.awaiting_merge,
        workload=WorkloadRef(namespace="demo", name="checkout-svc"),
        pr_url=url,
        opened_at=utcnow() - timedelta(minutes=opened_offset),
    )
    await store.put_incident(inc)
    return inc


@pytest.mark.asyncio
async def test_the_merge_closes_the_incident_that_opened_that_pull_request():
    """With two incidents in flight, "the most recent" closes the wrong one.

    That would mark a real, unfixed fault as resolved — a silent failure of
    exactly the kind this project keeps finding.
    """
    from warden.agents.fixtures import scripted_models

    store = InMemoryStore()
    await _incident(store, "INC-OLD", "https://github.com/mazzyy/estate-gitops/pull/5", 30)
    await _incident(store, "INC-NEW", PR_URL, 0)

    incident, _ = await verify_merged_incident(
        fleet=load_all("manifests/agents"),
        toolbox=_toolbox(store),
        store=store,
        pr_url="https://github.com/mazzyy/estate-gitops/pull/5",
        models=scripted_models(),
    )

    assert incident is not None
    assert incident.id == "INC-OLD", "the merge closed the wrong incident"
    assert (await store.get_incident("INC-NEW")).status is IncidentStatus.awaiting_merge


@pytest.mark.asyncio
async def test_a_pull_request_we_do_not_recognise_verifies_nothing():
    """Better to do nothing than to close an arbitrary incident."""
    from warden.agents.fixtures import scripted_models

    store = InMemoryStore()
    await _incident(store, "INC-ONE", PR_URL)

    incident, run = await verify_merged_incident(
        fleet=load_all("manifests/agents"),
        toolbox=_toolbox(store),
        store=store,
        pr_url="https://github.com/mazzyy/estate-gitops/pull/999",
        models=scripted_models(),
    )

    assert incident is None
    assert run is None
    assert (await store.get_incident("INC-ONE")).status is IncidentStatus.awaiting_merge
