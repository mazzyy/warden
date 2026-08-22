"""The alert the demo hands the fleet must describe the cluster that exists.

A live run against the real AKS cluster showed `checkout-svc: 0/3 replicas
ready` on screen while three replicas were serving traffic perfectly well. The
real fault was a blocked rollout: the old ReplicaSet healthy, the new one
crashlooping behind it. The number came from a string literal written for the
fake estate.

That is a quiet, serious bug. Every downstream judgement — the triage severity,
the diagnosis, the confidence score — is reasoning about a symptom that did not
happen, and the run still looks like a success. These tests keep the alert
honest.
"""

from __future__ import annotations

import pytest

from warden.agents.demo import ALERT, observed_alert
from warden.estate.fake import FakeAdapter
from warden.models import Status, WorkloadRef


class _StubEstate:
    """Minimal stand-in: only the one call observed_alert makes."""

    def __init__(self, summary: str, healthy: bool, rollout: str = "complete", reasons=()):
        self.summary = summary
        self.healthy = healthy
        self.rollout = rollout
        self.reasons = list(reasons)

    async def get_workload_status(self, ref: WorkloadRef) -> Status:
        return Status(
            ref=ref,
            healthy=self.healthy,
            summary=self.summary,
            rollout=self.rollout,
            reasons=self.reasons,
        )


class _BrokenEstate:
    async def get_workload_status(self, ref: WorkloadRef) -> Status:
        raise ConnectionError("cluster unreachable")


@pytest.mark.asyncio
async def test_the_fake_estate_keeps_the_canned_alert():
    """Offline mode injects the fault it describes, so the literal is true."""
    alert, status = await observed_alert(FakeAdapter("bad_config"), ALERT)
    assert alert == ALERT
    assert status is None


@pytest.mark.asyncio
async def test_a_blocked_rollout_gets_its_own_signature():
    """The second live bug, and the more expensive one.

    A blocked rollout used to arrive as `checkout-svc/Error` — the em dash
    split of a summary. Triage, whose only inputs are the alert text and past
    incidents, read "3/3 replicas ready" and closed the incident as healthy
    with a noisy threshold. It reasoned correctly about a sentence that was
    wrong. The signature now names the condition instead of a container reason.
    """
    estate = _StubEstate(
        "rollout BLOCKED — 1/3 pods on the new revision, CrashLoopBackOff; "
        "the previous revision is still serving 3/3",
        healthy=False,
        rollout="blocked",
        reasons=["CrashLoopBackOff"],
    )

    alert, status = await observed_alert(estate, ALERT)

    assert alert["signature"] == "checkout-svc/RolloutBlocked"
    assert "BLOCKED" in alert["title"]
    assert "0/3" not in alert["title"], "still claiming an outage that did not happen"
    assert status is not None and not status.healthy


@pytest.mark.asyncio
async def test_a_workload_serving_fine_while_something_restarts_is_not_healthy():
    estate = _StubEstate(
        "3/3 replicas ready, but CrashLoopBackOff",
        healthy=False,
        rollout="progressing",
        reasons=["CrashLoopBackOff"],
    )
    alert, _ = await observed_alert(estate, ALERT)
    assert alert["signature"] == "checkout-svc/CrashLoopBackOff"


@pytest.mark.asyncio
async def test_a_real_outage_is_reported_as_one():
    estate = _StubEstate(
        "0/3 replicas ready — CrashLoopBackOff",
        healthy=False,
        rollout="blocked",
        reasons=["CrashLoopBackOff"],
    )

    alert, _ = await observed_alert(estate, ALERT)

    assert alert["title"] == "checkout-svc: 0/3 replicas ready — CrashLoopBackOff"
    assert alert["signature"] == "checkout-svc/RolloutBlocked"


@pytest.mark.asyncio
async def test_a_healthy_workload_gets_no_invented_fault_reason():
    """After a fix lands there is nothing wrong, and the alert must say so."""
    estate = _StubEstate("3/3 replicas ready", healthy=True)

    alert, status = await observed_alert(estate, ALERT)

    assert alert["title"] == "checkout-svc: 3/3 replicas ready"
    assert alert["signature"] == "checkout-svc/Healthy"
    assert status is not None and status.healthy


@pytest.mark.asyncio
async def test_an_unreachable_cluster_does_not_kill_the_demo():
    alert, status = await observed_alert(_BrokenEstate(), ALERT)
    assert alert == ALERT
    assert status is None


@pytest.mark.asyncio
async def test_the_alert_is_never_mutated_in_place():
    """ALERT is module-level; a live run must not poison the next one."""
    before = dict(ALERT)
    await observed_alert(
        _StubEstate("1/3 replicas ready — OOMKilled", healthy=False, reasons=["OOMKilled"]), ALERT
    )
    assert before == ALERT
