"""The operations screen, and the store that lets it show anything real.

The dashboard and `make demo-live` are separate processes. Both used
InMemoryStore, so the dashboard could only ever render seed.py — three invented
incidents, one of them linking to a pull request that does not exist. Every
other part of this project runs against real infrastructure; the screen was the
exception, and it was the part most likely to end up in a video.

These tests cover the two things that fix required: a store two processes can
share, and one endpoint that describes a single consistent moment of an
incident.
"""

from __future__ import annotations

import pytest

from warden.control_plane.jsonl_store import JsonlStore
from warden.control_plane.store import Store
from warden.models import (
    AuditRecord,
    Decision,
    FleetState,
    Incident,
    IncidentStatus,
    Run,
    RunStatus,
    Severity,
)


@pytest.fixture
def store(tmp_path):
    return JsonlStore(tmp_path / "state")


# --------------------------------------------------------------------------
# The shared store
# --------------------------------------------------------------------------


def test_it_is_a_store(store):
    assert isinstance(store, Store)


@pytest.mark.asyncio
async def test_a_second_process_sees_the_first_ones_writes(store, tmp_path):
    """The entire point. Two JsonlStore objects, one directory."""
    await store.put_incident(Incident(id="INC-1", source="cm", signature="s", title="t"))
    other = JsonlStore(tmp_path / "state")
    assert [i.id for i in await other.list_incidents()] == ["INC-1"]


@pytest.mark.asyncio
async def test_a_run_is_folded_to_its_latest_state(store):
    """A run is written repeatedly as it progresses; reads must not duplicate it."""
    run = Run(id="r1", incident_id="INC-1", agent="triage", model="m")
    await store.put_run(run)
    run.status = RunStatus.ok
    run.total_tokens = 900
    await store.put_run(run)

    runs = await store.list_runs()
    assert len(runs) == 1, "the same run appeared twice"
    assert runs[0].status is RunStatus.ok
    assert runs[0].total_tokens == 900


@pytest.mark.asyncio
async def test_audit_records_are_never_folded(store):
    """Two identical calls are two events. Collapsing them would hide one.

    This is the opposite rule to runs, deliberately: a run is a thing with a
    current state, an audit record is a thing that happened.
    """
    for i in range(2):
        await store.append_audit(
            AuditRecord(
                id=f"a{i}",
                run_id="r1",
                incident_id="INC-1",
                agent="diagnostician",
                tool="get_workload_logs",
                decision=Decision.allow,
            )
        )
    assert len(await store.list_audit()) == 2


@pytest.mark.asyncio
async def test_a_half_written_line_does_not_take_down_the_reader(store):
    """The dashboard polls while the demo is mid-append. It must survive that."""
    await store.put_incident(Incident(id="INC-1", source="cm", signature="s", title="t"))
    with open(store._path("incidents"), "a", encoding="utf-8") as fh:
        fh.write('{"id": "INC-2", "sourc')  # torn write, no newline

    incidents = await store.list_incidents()
    assert [i.id for i in incidents] == ["INC-1"]


@pytest.mark.asyncio
async def test_clear_starts_a_fresh_take(store):
    await store.put_incident(Incident(id="INC-1", source="cm", signature="s", title="t"))
    await store.set_fleet_state(FleetState(kill_switch=True))
    store.clear()
    assert await store.list_incidents() == []
    assert (await store.get_fleet_state()).kill_switch is False


# --------------------------------------------------------------------------
# The live endpoint
# --------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WARDEN_STORE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WARDEN_STORE", "jsonl")
    from fastapi.testclient import TestClient

    import warden.control_plane.jsonl_store as js

    monkeypatch.setattr(js, "DEFAULT_DIR", tmp_path / "state")
    from warden.dashboard.api import app

    with TestClient(app) as c:
        yield c


def test_an_empty_store_is_labelled_as_sample_data(client):
    """An unlabelled fixture on screen is the same lie as an empty pull request."""
    body = client.get("/api/live").json()
    assert body["demoData"] is True


def test_the_pipeline_always_has_seven_stages_in_order(client):
    body = client.get("/api/live").json()
    assert [p["key"] for p in body["pipeline"]] == [
        "alert",
        "triage",
        "diagnose",
        "remediate",
        "review",
        "apply",
        "verify",
    ]


def test_exactly_one_stage_is_active_at_a_time(client):
    body = client.get("/api/live").json()
    active = [p for p in body["pipeline"] if p["state"] == "active"]
    assert len(active) <= 1, [p["key"] for p in active]


def test_three_stages_have_no_agent_in_them(client):
    """Alert, human review and CI apply. The picture makes the point; so does this.

    If a future manifest granted an agent something that put it in the review
    or apply column, this fails — which is the right place to find out.
    """
    body = client.get("/api/live").json()
    staged = {a["stage"] for a in body["agents"]}
    assert staged == {"triage", "diagnose", "remediate", "verify"}
    assert "review" not in staged
    assert "apply" not in staged


def test_every_agent_reports_a_state_the_ui_can_draw(client):
    drawable = {"idle", "working", "done", "blocked", "failed"}
    body = client.get("/api/live").json()
    for agent in body["agents"]:
        assert agent["state"] in drawable, agent


def test_the_tool_count_matches_the_feed(client):
    """The column and the feed show the same number or the demo looks broken."""
    body = client.get("/api/live").json()
    for agent in body["agents"]:
        assert agent["toolCalls"] == len(agent["calls"])


def test_the_kill_switch_shows_as_blocked_not_merely_flagged(client, tmp_path):
    """Draining the fleet has to be visible on the agents, not just in a header."""
    import asyncio

    from warden.dashboard.api import _state

    store = _state["store"]
    inc = Incident(
        id="INC-K",
        source="cm",
        signature="s",
        title="t",
        severity=Severity.critical,
        status=IncidentStatus.diagnosing,
    )
    run = Run(
        id="rk",
        incident_id="INC-K",
        agent="diagnostician",
        model="m",
        status=RunStatus.running,
    )
    asyncio.run(_seed_running(store, inc, run))

    client.post("/api/kill-switch", json={"engaged": True})
    body = client.get("/api/live").json()
    assert body["killSwitch"] is True
    diagnostician = next(a for a in body["agents"] if a["name"] == "diagnostician")
    assert diagnostician["state"] == "blocked"


async def _seed_running(store, incident, run):
    await store.put_incident(incident)
    await store.put_run(run)


def test_a_denial_is_carried_with_its_reason(client):
    """The reason is the whole value of the denial. A count alone proves nothing."""
    body = client.get("/api/live").json()
    for agent in body["agents"]:
        for call in agent["calls"]:
            if call["decision"] == "deny":
                assert call["reason"], f"{agent['name']} denied {call['tool']} with no reason"
