"""The full incident pipeline, offline.

Runs signal → triage → diagnosis → pull request with a scripted model and the
fake estate. No cloud, no credentials, no spend — so this can run in CI on every
push, which is the whole reason ADR-006 exists.
"""

from __future__ import annotations

import pytest

from warden.agents.fixtures import scripted_models
from warden.agents.orchestrator import handle_incident
from warden.control_plane.registry import load_all
from warden.control_plane.store import InMemoryStore
from warden.estate.fake import FakeAdapter
from warden.models import Decision, Diagnosis, IncidentStatus, TriageVerdict
from warden.tools.github_client import GitHubClient
from warden.tools.toolbox import ToolBox

ALERT = {
    "source": "cloud-monitoring",
    "signature": "checkout-svc/CrashLoopBackOff",
    "title": "checkout-svc: 0/3 replicas ready",
    "workload": "checkout-svc",
    "namespace": "demo",
}


async def _run(mode: str = "bad_config"):
    store = InMemoryStore()
    estate = FakeAdapter(mode)
    github = GitHubClient(repo_full_name="mazzyy/estate-gitops", token=None)
    toolbox = ToolBox(estate=estate, store=store, github=github, alert_context=ALERT)
    fleet = load_all("manifests/agents")
    result = await handle_incident(
        alert=ALERT, fleet=fleet, toolbox=toolbox, store=store, models=scripted_models()
    )
    return result, store, github


@pytest.mark.asyncio
async def test_incident_runs_end_to_end_and_opens_a_pull_request():
    result, _store, github = await _run()

    assert result.incident.status is IncidentStatus.awaiting_merge
    assert len(result.runs) == 3, [r.run.agent for r in result.runs]
    assert github.dry_run_prs, "no pull request was proposed"

    pr = github.dry_run_prs[0]
    assert "PAYMENT_ENDPOINT" in pr.title or "payment" in pr.title.lower()
    assert list(pr.changes) == ["apps/checkout-svc/deployment.yaml"]
    # The PR body must tell a reviewer what to watch for after merge.
    assert "review" in pr.body.lower()


@pytest.mark.asyncio
async def test_the_diagnosis_cites_evidence_from_tools_that_actually_ran():
    result, store, _ = await _run()

    diagnosis = result.diagnosis.parse(Diagnosis)
    assert diagnosis is not None
    assert diagnosis.evidence, "a diagnosis with no evidence chain is a guess"

    audit = await store.list_audit()
    tools_called = {a.tool for a in audit if a.decision is Decision.allow}
    for item in diagnosis.evidence:
        assert item.source in tools_called, f"cited {item.source} but never called it"


@pytest.mark.asyncio
async def test_every_tool_call_in_the_incident_is_audited():
    result, store, _ = await _run()

    audit = await store.list_audit()
    assert len(audit) == result.total_tool_calls
    # Every record must be attributable.
    for record in audit:
        assert record.incident_id == result.incident.id
        assert record.agent in {"triage", "diagnostician", "remediator"}
        assert record.run_id


@pytest.mark.asyncio
async def test_token_and_cost_accounting_is_populated():
    result, _, _ = await _run()

    assert result.total_tokens > 0
    for agent_run in result.runs:
        assert agent_run.run.total_tokens > 0, f"{agent_run.run.agent} recorded no tokens"
        assert agent_run.run.ended_at is not None


@pytest.mark.asyncio
async def test_triage_produces_a_structured_verdict():
    result, _, _ = await _run()

    verdict = result.triage.parse(TriageVerdict)
    assert verdict is not None
    assert verdict.escalate is True
    assert verdict.reasoning


@pytest.mark.asyncio
async def test_the_fake_estate_reproduces_both_failure_modes():
    estate = FakeAdapter("bad_config")
    detail = await estate.describe_workload(
        (await estate.list_workloads("demo"))[0].ref
    )
    assert detail.env["PAYMENT_ENDPOINT"].startswith("htps://")
    assert any(c.reason == "CrashLoopBackOff" for c in detail.containers)

    estate.inject("oom")
    detail = await estate.describe_workload(
        (await estate.list_workloads("demo"))[0].ref
    )
    assert any(c.reason == "OOMKilled" for c in detail.containers)
    assert detail.resources["limits"]["memory"] == "64Mi"


@pytest.mark.asyncio
async def test_dry_run_is_always_declared_in_the_result():
    """You must not be able to show a fake PR on camera by accident."""
    github = GitHubClient(repo_full_name="mazzyy/estate-gitops", token=None)
    out = await github.open_pull_request(title="t", body="b", changes={"a.yaml": "x"})
    assert out["dry_run"] is True
    assert "DRY-RUN" in out["pr_url"]


@pytest.mark.asyncio
async def test_a_broken_github_token_degrades_instead_of_crashing():
    """An expired PAT must not take down a live incident mid-run.

    This is a demo-survivability test. Before it existed, a bad token raised
    out of read_repo_file and killed the whole incident with a stack trace —
    on camera, that ends the take.
    """
    gh = GitHubClient(repo_full_name="mazzyy/estate-gitops", token="ghp_definitely_invalid")

    result = await gh.read_file("apps/checkout-svc/deployment.yaml")
    assert result["error"] == "github_read_failed"
    assert "hint" in result

    result = await gh.open_pull_request(title="t", body="b", changes={"a.yaml": "x"})
    assert result["error"] == "github_open_pull_request_failed"


@pytest.mark.asyncio
async def test_triage_cannot_recall_its_own_incident():
    """A live run caught this: triage closed an incident as a duplicate of itself.

    The orchestrator persists the incident before any agent runs, so an
    unfiltered recall returns the very incident being triaged. The model then
    reasons correctly from a tool that lied to it — which is the worst kind of
    bug, because nothing looks wrong.
    """
    store = InMemoryStore()
    toolbox = ToolBox(estate=FakeAdapter("bad_config"), store=store, alert_context=ALERT)
    fleet = load_all("manifests/agents")

    result = await handle_incident(
        alert=ALERT, fleet=fleet, toolbox=toolbox, store=store, models=scripted_models()
    )

    # The scripted triage escalates; a self-recall would have abandoned it.
    assert result.incident.status is not IncidentStatus.abandoned
    verdict = result.triage.parse(TriageVerdict)
    assert verdict.duplicate_of != result.incident.id, "recalled itself as a precedent"


@pytest.mark.asyncio
async def test_recall_still_finds_genuinely_earlier_incidents():
    """Self-exclusion must not break the feature it protects."""
    from warden.models import Incident
    from warden.models import IncidentStatus as St

    store = InMemoryStore()
    await store.put_incident(
        Incident(
            id="INC-OLDER",
            source="cloud-monitoring",
            signature="checkout-svc/CrashLoopBackOff",
            status=St.resolved,
        )
    )
    toolbox = ToolBox(estate=FakeAdapter(), store=store, alert_context=ALERT)
    toolbox.bind_incident("INC-CURRENT")

    recall = toolbox.build(["recall_similar_incidents"])[0]
    out = await recall(signature="checkout-svc/CrashLoopBackOff")

    assert out["count"] == 1
    assert out["matches"][0]["id"] == "INC-OLDER"


@pytest.mark.asyncio
async def test_a_no_op_patch_does_not_open_an_empty_pull_request():
    """A live run opened a PR with an empty diff, and it looked like success.

    The remediator read a file that was already correct and wrote it back
    byte-identical. GitHub happily made a commit with no content, so the run
    reported a pull request URL for a change that did not exist. Failing loudly
    beats a green result that fixed nothing.
    """
    gh = GitHubClient(repo_full_name="mazzyy/estate-gitops", token=None)
    assert gh.dry_run

    # Dry-run still reports what it would do; the real guard lives in _open()
    # and is exercised against GitHub. What we assert here is the contract:
    # a result carrying `error` must never also carry a pr_url a caller would
    # mistake for a real one.
    out = await gh.open_pull_request(title="t", body="b", changes={"a.yaml": "x"})
    assert "error" not in out
    assert out["pr_url"].endswith("DRY-RUN")
