"""What the agent writes as, and what it is allowed to claim about that.

This file exists because of a measured result, not a worry.
`estate-gitops/scripts/verify-github-token.sh` attempted a direct commit to a
branch-protected `main` using the agent's own fine-grained PAT and got **HTTP
201**. The commit landed. Branch protection was enabled at the time.

The reason is that a PAT inherits the repository role of the human who minted
it, and a repository admin bypasses protection when `enforce_admins` is false.
Narrow scopes decide which repositories and which APIs a token may touch; they
do not demote the actor behind it.

So the sentence Warden had been printing on every pull request — "this agent
cannot merge this pull request" — was false on the path we were actually
running. These tests make it impossible for that sentence to reappear on a
credential that cannot back it up.
"""

from __future__ import annotations

import pytest

from warden.config import GitHubCredential
from warden.tools.github_client import GitHubClient

APP = GitHubCredential(
    kind="app",
    label="GitHub App 1234 · installation 5678",
    enforced=True,
    app_id="1234",
    installation_id="5678",
    private_key="-----BEGIN RSA PRIVATE KEY-----\nnot-a-real-key\n-----END RSA PRIVATE KEY-----",
)
PAT = GitHubCredential(kind="pat", label="personal access token", enforced=False, token="ghp_x")
NONE = GitHubCredential(kind="none", label="dry run — no GitHub credential", enforced=False)


def _client(credential):
    return GitHubClient(repo_full_name="mazzyy/estate-gitops", credential=credential)


# --------------------------------------------------------------------------
# The claim on the artefact
# --------------------------------------------------------------------------


def test_a_pat_may_not_claim_it_cannot_merge():
    """The exact false sentence. It must never appear on the PAT path again."""
    body = _client(PAT)._signature()
    assert "cannot merge this" not in body
    assert "the platform will refuse" not in body


def test_a_pat_says_plainly_that_the_boundary_is_only_our_code():
    body = _client(PAT)._signature()
    assert "personal access token" in body
    assert "inherits the repository role" in body
    assert "promise, not a control" in body


def test_only_an_app_may_claim_the_platform_enforces_it():
    body = _client(APP)._signature()
    assert "cannot merge this" in body
    assert "GitHub App" in body
    assert "⚠️" not in body


def test_every_credential_still_claims_the_thing_that_is_always_true():
    """No cluster write scope holds on every path — that one is real."""
    for cred in (APP, PAT, NONE):
        body = _client(cred)._signature()
        assert "no cluster credentials" in body
        assert "only write primitive is this pull request" in body


# --------------------------------------------------------------------------
# The credential itself
# --------------------------------------------------------------------------


def test_enforced_is_true_for_the_app_and_false_for_everything_else():
    assert _client(APP).review_boundary_enforced is True
    assert _client(PAT).review_boundary_enforced is False
    assert _client(NONE).review_boundary_enforced is False


def test_dry_run_follows_the_credential_kind():
    assert _client(NONE).dry_run is True
    assert _client(PAT).dry_run is False
    assert _client(APP).dry_run is False


def test_the_legacy_token_argument_still_works():
    """Existing callers pass token=; they must keep working, as the PAT path."""
    assert GitHubClient(repo_full_name="x/y", token="ghp_x").dry_run is False
    assert GitHubClient(repo_full_name="x/y", token=None).dry_run is True
    assert GitHubClient(repo_full_name="x/y", token="ghp_x").review_boundary_enforced is False


def test_the_pat_caveat_names_the_variables_that_fix_it():
    """A warning that does not say what to do instead is just noise."""
    caveat = PAT.caveat
    assert "GITHUB_APP_ID" in caveat
    assert "GITHUB_APP_PRIVATE_KEY_PATH" in caveat


def test_an_app_carries_no_caveat():
    assert APP.caveat == ""


# --------------------------------------------------------------------------
# Least privilege at the moment of use
# --------------------------------------------------------------------------


def test_the_installation_token_cannot_read_or_edit_branch_protection():
    """`administration` would let the agent relax the rule that governs it."""
    perms = GitHubClient.TOKEN_PERMISSIONS
    assert "administration" not in perms
    assert perms["contents"] == "write"
    assert perms["pull_requests"] == "write"
    assert perms["metadata"] == "read"


def test_the_installation_token_is_not_granted_anything_else():
    """A new permission should be a deliberate edit here, with a reason."""
    assert set(GitHubClient.TOKEN_PERMISSIONS) == {"contents", "pull_requests", "metadata"}


# --------------------------------------------------------------------------
# How the installation token is minted.
#
# PyGithub's `Auth.AppAuth(...).get_installation_auth(...)` returns an auth
# object that is NOT attached to a Requester. Reading `.token` on it raises
#
#     AssertionError: Method withRequester(Requester) must be called first
#
# before any HTTP request happens. `estate-gitops/scripts/verify-github-token.sh`
# hit exactly this, silently fell back to the PAT, and reported the write path
# as unenforced — on a correctly configured GitHub App. A verification script
# that quietly downgrades what it is verifying is the worst possible bug in a
# verification script.
#
# GitHubClient does not have that problem, because it hands the auth to
# `Github(auth=...)`, and PyGithub's Requester calls `withRequester` on it
# during construction. These tests pin that difference so a future refactor
# cannot quietly drop the wrapper.
# --------------------------------------------------------------------------


def _throwaway_key() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


def test_a_bare_installation_auth_cannot_produce_a_token():
    """Characterises the library trap. No network: the assert fires first."""
    from github import Auth

    auth = Auth.AppAuth("123", _throwaway_key()).get_installation_auth(456)
    with pytest.raises(AssertionError, match="withRequester"):
        _ = auth.token


def test_the_client_wires_the_auth_through_a_requester(monkeypatch):
    """GitHubClient must construct Github(auth=...) — that is what wires it."""
    import github

    seen = {}

    class FakeGithub:
        def __init__(self, auth=None, **kwargs):
            seen["auth"] = auth

        def get_repo(self, name):
            seen["repo"] = name
            return "repo-handle"

    monkeypatch.setattr(github, "Github", FakeGithub)

    cred = GitHubCredential(
        kind="app",
        label="GitHub App 123 · installation 456",
        enforced=True,
        app_id="123",
        installation_id="456",
        private_key=_throwaway_key(),
    )
    client = GitHubClient(repo_full_name="mazzyy/estate-gitops", credential=cred)

    assert client._repo_handle() == "repo-handle"
    assert seen["repo"] == "mazzyy/estate-gitops"
    assert type(seen["auth"]).__name__ == "AppInstallationAuth"
