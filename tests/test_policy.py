"""The load-bearing tests.

If the rest of the project burns down, these are what prove the claim the whole
submission rests on: an agent cannot call a tool it was not granted, and the
denial is recorded with a reason.
"""

from __future__ import annotations

import pytest

from warden.control_plane import policy
from warden.control_plane.registry import ManifestError, load_all, load_manifest
from warden.models import Decision

MANIFEST_DIR = "manifests/agents"


@pytest.fixture(scope="module")
def fleet():
    return load_all(MANIFEST_DIR)


# --------------------------------------------------------------------------
# The denial. This is the scene in the demo video.
# --------------------------------------------------------------------------


def test_remediator_cannot_delete_a_workload(fleet):
    """The Remediator may open a pull request. It may not touch the cluster."""
    result = policy.evaluate(
        fleet["remediator"], "delete_workload", {"namespace": "demo", "name": "checkout-svc"}
    )
    assert result.decision is Decision.deny
    assert "delete_workload" in result.reason
    assert "allow-list" in result.reason


def test_no_agent_in_the_fleet_can_write_to_the_cluster(fleet):
    """Not a spot check — an assertion over the entire fleet.

    This is the test that would catch a future manifest edit quietly granting
    cluster:demo:write to somebody.
    """
    for name, manifest in fleet.items():
        for tool in ("delete_workload", "scale_workload"):
            result = policy.evaluate(manifest, tool, {"namespace": "demo"})
            assert result.decision is Decision.deny, f"{name} was allowed to call {tool}"


def test_diagnostician_holds_no_write_scope_at_all(fleet):
    scopes = set(fleet["diagnostician"].spec.scopes)
    assert not any("write" in s for s in scopes), scopes


# --------------------------------------------------------------------------
# The allow path — a denial engine that denies everything proves nothing.
# --------------------------------------------------------------------------


def test_diagnostician_may_read_logs(fleet):
    result = policy.evaluate(
        fleet["diagnostician"], "get_workload_logs", {"namespace": "demo", "name": "checkout-svc"}
    )
    assert result.allowed, result.reason


def test_remediator_may_open_a_pull_request(fleet):
    result = policy.evaluate(
        fleet["remediator"],
        "propose_patch",
        {"namespace": "demo", "files": ["apps/checkout-svc/deployment.yaml"]},
    )
    assert result.allowed, result.reason


# --------------------------------------------------------------------------
# Blast radius
# --------------------------------------------------------------------------


def test_patch_outside_the_namespace_is_denied(fleet):
    result = policy.evaluate(
        fleet["remediator"], "propose_patch", {"namespace": "kube-system", "files": ["x.yaml"]}
    )
    assert result.decision is Decision.deny
    assert "blast radius" in result.reason


def test_patch_touching_too_many_files_is_denied(fleet):
    files = [f"apps/checkout-svc/{i}.yaml" for i in range(5)]
    result = policy.evaluate(fleet["remediator"], "propose_patch", {"namespace": "demo", "files": files})
    assert result.decision is Decision.deny
    assert "maxFilesPerPatch" in result.reason


def test_patch_at_exactly_the_limit_is_allowed(fleet):
    files = [f"apps/checkout-svc/{i}.yaml" for i in range(3)]
    result = policy.evaluate(fleet["remediator"], "propose_patch", {"namespace": "demo", "files": files})
    assert result.allowed, result.reason


# --------------------------------------------------------------------------
# Kill switch and unknown tools
# --------------------------------------------------------------------------


def test_kill_switch_denies_everything(fleet):
    for name, manifest in fleet.items():
        for tool in manifest.spec.tools:
            result = policy.evaluate(manifest, tool, {"namespace": "demo"}, kill_switch=True)
            assert result.decision is Decision.deny, f"{name}.{tool} survived the kill switch"
            assert "kill switch" in result.reason


def test_a_tool_outside_the_catalog_is_denied(fleet):
    result = policy.evaluate(fleet["remediator"], "exfiltrate_secrets", {})
    assert result.decision is Decision.deny
    assert "tool catalog" in result.reason


# --------------------------------------------------------------------------
# Manifest validation
# --------------------------------------------------------------------------


def test_manifests_load_and_name_themselves(fleet):
    assert set(fleet) == {"triage", "diagnostician", "remediator", "verifier"}
    for name, manifest in fleet.items():
        assert manifest.name == name


def test_a_manifest_granting_an_unknown_tool_fails_loudly(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "apiVersion: warden.dev/v1\n"
        "kind: Agent\n"
        "metadata: {name: typo}\n"
        "spec:\n"
        "  model: gemini-3.5-flash\n"
        "  tools: [get_workload_logz]\n"
    )
    with pytest.raises(ManifestError, match="get_workload_logz"):
        load_manifest(bad)


def test_redaction_drops_the_tool_context(fleet):
    cleaned = policy.redact("propose_patch", {"namespace": "demo", "tool_context": object()})
    assert "tool_context" not in cleaned
    assert cleaned["namespace"] == "demo"


# --------------------------------------------------------------------------
# Blast radius by line count.
#
# A live run against the real cluster produced a pull request that fixed the
# actual bug AND replaced the container image with one that does not exist,
# deleting 39 lines of working entrypoint on the way. Merging it would have
# broken the cluster worse than the incident did. maxFilesPerPatch did not
# catch it — the damage was inside a single permitted file.
# --------------------------------------------------------------------------


def test_every_writing_agent_has_a_line_level_blast_radius(fleet):
    for name, manifest in fleet.items():
        limit = manifest.spec.blast_radius.max_changed_lines
        can_write = any("write" in scope for scope in manifest.spec.scopes)
        if can_write:
            assert limit > 0, f"{name} can open pull requests but may rewrite a file freely"
            assert limit <= 40, f"{name} may change {limit} lines; that is a rewrite, not a fix"
        else:
            assert limit == 0, f"{name} holds no write scope but carries a line budget"


def test_the_remediator_is_capped_tightly(fleet):
    """The remediator is the only agent that writes. Its cap is the real one."""
    assert fleet["remediator"].spec.blast_radius.max_changed_lines <= 20
