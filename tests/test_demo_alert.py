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
async def test_a_blocked_rollout_says_so_in_the_title_and_names_the_cause():
    """The second live bug, and the more expensive one.

    A blocked rollout used to arrive as `checkout-svc/Error` — the em dash
    split of a summary. Triage, whose only inputs are the alert text and past
    incidents, read "3/3 replicas ready" and closed the incident as healthy
    with a noisy threshold. It reasoned correctly about a sentence that was
    wrong.

    The blocked-ness now lives in the title, where Triage reads it, and the
    signature names the container reason — see the collision test below for
    why that split matters.
    """
    estate = _StubEstate(
        "rollout BLOCKED — 1/3 pods on the new revision, CrashLoopBackOff; "
        "the previous revision is still serving 3/3",
        healthy=False,
        rollout="blocked",
        reasons=["CrashLoopBackOff"],
    )

    alert, status = await observed_alert(estate, ALERT)

    assert alert["signature"] == "checkout-svc/CrashLoopBackOff"
    assert "BLOCKED" in alert["title"]
    assert "0/3" not in alert["title"], "still claiming an outage that did not happen"
    assert status is not None and not status.healthy


@pytest.mark.asyncio
async def test_a_rollout_stalled_with_no_container_reason_falls_back_to_the_symptom():
    """Sometimes the cluster genuinely offers no cause — pods never scheduled."""
    estate = _StubEstate(
        "rollout BLOCKED — 0/3 pods on the new revision",
        healthy=False,
        rollout="blocked",
        reasons=[],
    )
    alert, _ = await observed_alert(estate, ALERT)
    assert alert["signature"] == "checkout-svc/RolloutBlocked"


@pytest.mark.asyncio
async def test_two_different_faults_do_not_share_one_signature():
    """The dedup collision, which would have cost a live demo.

    An OOMKilled workload and a workload crashlooping on a bad configuration
    value both stall a rollout. When the signature led with `RolloutBlocked`,
    both arrived as `checkout-svc/RolloutBlocked` — so the second incident the
    fleet ever saw matched the first one's signature, and Triage would read a
    brand-new, unrelated fault as a duplicate of an incident already awaiting a
    merge and stop before anyone looked at the cluster.

    `recall_similar_incidents` matches a query signature as a substring of a
    stored one, so it is not enough for the two to differ: neither may be a
    substring of the other.
    """
    oom = _StubEstate(
        "rollout BLOCKED — 0/3 pods on the new revision, OOMKilled",
        healthy=False,
        rollout="blocked",
        reasons=["OOMKilled"],
    )
    bad_config = _StubEstate(
        "rollout BLOCKED — 0/3 pods on the new revision, CrashLoopBackOff",
        healthy=False,
        rollout="blocked",
        reasons=["CrashLoopBackOff"],
    )

    oom_alert, _ = await observed_alert(oom, ALERT)
    config_alert, _ = await observed_alert(bad_config, ALERT)

    a, b = oom_alert["signature"].lower(), config_alert["signature"].lower()
    assert a != b
    assert a not in b and b not in a, "one signature would match the other by substring"


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
    assert alert["signature"] == "checkout-svc/CrashLoopBackOff"


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
