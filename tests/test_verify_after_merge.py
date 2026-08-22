"""Closing the loop, after a human has merged.

The Verifier cannot be the last step of `handle_incident`. The merge is a human
action that happens minutes or hours after that function returns — the process
that would have run the Verifier is long gone by the time verifying means
anything. So it needs to be startable on its own, against an incident that is
already in the store.

That is what `verify_merged_incident` is, and it is the command that turns the
last two columns of the operations screen from `waiting` into a closed incident.
"""

from __future__ import annotations

import pytest

from warden.agents.fixtures import scripted_models
from warden.agents.orchestrator import verify_merged_incident
from warden.control_plane.registry import load_all
from warden.control_plane.store import InMemoryStore
from warden.estate.fake import FakeAdapter
from warden.models import Incident, IncidentStatus, WorkloadRef
from warden.tools.github_client import GitHubClient
from warden.tools.toolbox import ToolBox

MANIFEST_DIR = "manifests/agents"


def _toolbox(store, mode):
    return ToolBox(
        estate=FakeAdapter(mode),
        store=store,
        github=GitHubClient(repo_full_name="mazzyy/estate-gitops", token=None),
        alert_context={},
    )


async def _run(mode: str, incident: Incident | None = None):
    store = InMemoryStore()
    if incident is None:
        incident = Incident(
            id="INC-MERGED",
            source="cloud-monitoring",
            signature="checkout-svc/RolloutBlocked",
            title="checkout-svc: rollout BLOCKED",
            status=IncidentStatus.awaiting_merge,
            workload=WorkloadRef(namespace="demo", name="checkout-svc"),
            pr_url="https://github.com/mazzyy/estate-gitops/pull/6",
        )
    await store.put_incident(incident)
    result = await verify_merged_incident(
        fleet=load_all(MANIFEST_DIR),
        toolbox=_toolbox(store, mode),
        store=store,
        models=scripted_models(),
    )
    return result, store


@pytest.mark.asyncio
async def test_a_recovered_workload_closes_the_incident():
    (incident, run), _ = await _run("healthy")
    assert incident is not None and run is not None
    assert incident.status is IncidentStatus.resolved
    assert incident.closed_at is not None


@pytest.mark.asyncio
async def test_a_workload_that_did_not_recover_stays_open():
    """The important direction. Closing on a still-broken service is the
    failure this whole project is about — a run that looks like success."""
    (incident, _), _ = await _run("bad_config")
    assert incident.status is not IncidentStatus.resolved
    assert incident.closed_at is None


@pytest.mark.asyncio
async def test_a_blocked_rollout_is_not_mistaken_for_recovery():
    """3/3 replicas ready, and still broken. The state that fooled Triage."""
    (incident, _), _ = await _run("blocked_rollout")
    assert incident.status is not IncidentStatus.resolved


@pytest.mark.asyncio
async def test_the_verdict_does_not_come_from_the_model():
    """An independent status read decides, not the Verifier's prose.

    The scripted Verifier returns "recovered" in every mode. If the model's
    text were what closed incidents, `bad_config` above would close too.
    """
    (incident, run), _ = await _run("bad_config")
    assert "recover" in str(run.run.outcome).lower()
    assert incident.status is not IncidentStatus.resolved


@pytest.mark.asyncio
async def test_nothing_to_verify_is_not_an_error():
    store = InMemoryStore()
    incident, run = await verify_merged_incident(
        fleet=load_all(MANIFEST_DIR),
        toolbox=_toolbox(store, "healthy"),
        store=store,
        models=scripted_models(),
    )
    assert incident is None
    assert run is None


@pytest.mark.asyncio
async def test_the_verifier_is_bound_to_the_incident_it_is_verifying():
    """Otherwise recall hands it its own incident as a prior one — the same
    self-reference bug that once had Triage close an incident as a duplicate
    of itself."""
    store = InMemoryStore()
    box = _toolbox(store, "healthy")
    await store.put_incident(Incident(id="INC-BIND", source="cm", signature="s", title="t"))
    await verify_merged_incident(
        fleet=load_all(MANIFEST_DIR), toolbox=box, store=store, models=scripted_models()
    )
    assert box._current_incident_id == "INC-BIND"
