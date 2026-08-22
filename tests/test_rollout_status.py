"""Every replica ready, and the workload badly broken.

A live run against the real cluster reported `checkout-svc: 3/3 replicas ready
— Error`, and Triage closed the incident:

    severity: low
    escalate: False
    reasoning: All 3 out of 3 replicas are currently ready, indicating the
    workload is healthy. This is likely a transient error or a noisy threshold.

Triage was right about the sentence it was given. The sentence was wrong.

`kubectl` had just rolled out a bad config. Kubernetes did exactly what it
should: it started one pod on the new revision, that pod failed, and the
rollout stopped there — leaving the PREVIOUS ReplicaSet serving all three
replicas. Nothing was unavailable. Nothing was fine either. The estate was
pinned to an old revision, every future deploy was stuck behind this one, and
any serving pod that restarted would come back on the broken spec.

The old health test was `ready == desired`, which is true for the entire
duration of that state. So the single most load-bearing read in the system
could not see the exact failure shape the whole project is built around.

These tests are the fix's teeth.
"""

from __future__ import annotations

import pytest

from warden.estate.aks import FAILING_REASONS, AksAdapter
from warden.estate.fake import FakeAdapter
from warden.models import ContainerState, WorkloadDetail, WorkloadRef

REF = WorkloadRef(namespace="demo", name="checkout-svc")


def _detail(*, desired=3, ready=3, updated=3, available=3, reasons=(), conditions=()):
    return WorkloadDetail(
        ref=REF,
        replicas_desired=desired,
        replicas_ready=ready,
        replicas_updated=updated,
        replicas_available=available,
        containers=[
            ContainerState(name="checkout", ready=False, reason=r, restart_count=4) for r in reasons
        ],
        conditions=list(conditions),
    )


async def _status(monkeypatch, detail):
    adapter = AksAdapter(apiserver="https://x", token="t")

    async def fake_describe(ref):
        return detail

    monkeypatch.setattr(adapter, "describe_workload", fake_describe)
    return await adapter.get_workload_status(REF)


# --------------------------------------------------------------------------
# The bug
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_blocked_rollout_is_not_healthy(monkeypatch):
    """The exact production state. 3/3 ready, 1/3 updated, new pod failing."""
    status = await _status(monkeypatch, _detail(ready=3, updated=1, reasons=["CrashLoopBackOff"]))
    assert status.healthy is False, "a blocked rollout reported itself as healthy"
    assert status.rollout == "blocked"
    assert "BLOCKED" in status.summary


@pytest.mark.asyncio
async def test_the_summary_says_why_all_replicas_look_fine(monkeypatch):
    """A reader must not have to infer that the ready pods are the old ones."""
    status = await _status(monkeypatch, _detail(ready=3, updated=1, reasons=["CrashLoopBackOff"]))
    assert "previous revision is still serving" in status.summary
    assert "1/3" in status.summary


@pytest.mark.asyncio
async def test_error_counts_as_failing_not_just_crashloopbackoff(monkeypatch):
    """The live run caught the window before CrashLoopBackOff was reached.

    A container that exited non-zero four seconds ago reports `Error`. Waiting
    for the kubelet to escalate it to CrashLoopBackOff means the first minute
    after a bad deploy is invisible — which is precisely when a demo runs.
    """
    assert "Error" in FAILING_REASONS
    status = await _status(monkeypatch, _detail(ready=3, updated=1, reasons=["Error"]))
    assert status.rollout == "blocked"


@pytest.mark.asyncio
async def test_progress_deadline_exceeded_is_treated_as_blocked(monkeypatch):
    """Kubernetes' own verdict, in as many words. Believe it."""
    status = await _status(
        monkeypatch,
        _detail(
            ready=3,
            updated=3,
            reasons=["CrashLoopBackOff"],
            conditions=["Progressing=False (ProgressDeadlineExceeded)"],
        ),
    )
    assert status.rollout == "blocked"
    assert "progress deadline exceeded" in status.summary


# --------------------------------------------------------------------------
# The states that must NOT be mistaken for it
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_genuinely_healthy_workload_is_healthy(monkeypatch):
    """A denial engine that denies everything proves nothing; same idea here."""
    status = await _status(monkeypatch, _detail())
    assert status.healthy is True
    assert status.rollout == "complete"
    assert status.summary == "3/3 replicas ready"


@pytest.mark.asyncio
async def test_a_normal_rollout_in_flight_is_not_called_broken(monkeypatch):
    """Mid-deploy, nothing failing. Reporting this as an incident is a false alarm."""
    status = await _status(monkeypatch, _detail(ready=2, updated=2, reasons=[]))
    assert status.rollout == "progressing"
    assert "BLOCKED" not in status.summary


@pytest.mark.asyncio
async def test_a_full_outage_still_reads_as_an_outage(monkeypatch):
    status = await _status(
        monkeypatch, _detail(ready=0, updated=3, available=0, reasons=["CrashLoopBackOff"])
    )
    assert status.healthy is False
    assert status.summary.startswith("0/3 replicas ready")


@pytest.mark.asyncio
async def test_a_scaled_to_zero_workload_is_not_reported_as_ready(monkeypatch):
    """0 == 0 satisfied the old `ready == desired` test at zero replicas too."""
    status = await _status(monkeypatch, _detail(desired=0, ready=0, updated=0, available=0))
    assert status.healthy is False
    assert status.rollout == "none"


@pytest.mark.asyncio
async def test_restarts_behind_a_healthy_front_are_still_surfaced(monkeypatch):
    """Serving fine now, but something is looping. Not an outage, not silence."""
    status = await _status(monkeypatch, _detail(ready=3, updated=3, reasons=["OOMKilled"]))
    assert status.healthy is False
    assert "but OOMKilled" in status.summary


# --------------------------------------------------------------------------
# The fake estate has to be able to reach the same state
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_fake_estate_can_produce_a_blocked_rollout():
    """Otherwise this state is only reachable by breaking a real cluster."""
    status = await FakeAdapter("blocked_rollout").get_workload_status(REF)
    assert status.healthy is False
    assert status.rollout == "blocked"
    assert "previous revision is still serving" in status.summary


@pytest.mark.asyncio
async def test_the_fake_healthy_mode_reports_a_complete_rollout():
    status = await FakeAdapter("healthy").get_workload_status(REF)
    assert status.healthy is True
    assert status.rollout == "complete"
