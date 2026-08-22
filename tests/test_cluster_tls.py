"""The connection the whole evidence chain rests on.

Warden ran for days against a real cluster with TLS verification disabled. The
comment justifying it argued that a read-only token made it an acceptable
trade-off. That argument is wrong in a way worth keeping a test around for.

Skipping verification does not weaken writes — the agent has no cluster write
scope to weaken. It weakens knowing who you are talking to, and two things
follow. The bearer token is handed to whoever intercepts the connection, and it
can read pod logs across the namespace. And the Diagnostician's output is
derived entirely from those logs: forge them and it will faithfully open a pull
request fixing a bug that never existed, with a confident evidence chain
pointing straight at the forgery.

Every guarantee downstream of this connection — the evidence chain, the blast
radius, a reviewer's ability to trust the pull request body — assumes the logs
are real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from warden.estate.aks import AksAdapter

PEM = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AKS_CA_CERT_PATH", "AKS_CA_CERT"):
        monkeypatch.delenv(var, raising=False)
    from warden.config import settings

    settings.cache_clear()
    yield
    settings.cache_clear()


def test_no_ca_configured_returns_none():
    """And the adapter logs a warning and the demo header prints NOT VERIFIED."""
    assert AksAdapter._ca_cert_path() is None


def test_a_path_is_used_as_a_path(monkeypatch, tmp_path):
    ca = tmp_path / "cluster-ca.crt"
    ca.write_text(PEM)
    monkeypatch.setenv("AKS_CA_CERT_PATH", str(ca))
    assert AksAdapter._ca_cert_path() == str(ca)


def test_an_inline_pem_is_written_to_a_file(monkeypatch):
    """People paste the certificate. A cert is not a secret; accept it.

    Silently ignoring a pasted PEM would leave verification off while the
    operator believed they had turned it on — the worst of both outcomes.
    """
    monkeypatch.setenv("AKS_CA_CERT_PATH", PEM)
    path = AksAdapter._ca_cert_path()
    assert path is not None
    assert Path(path).read_text().startswith("-----BEGIN CERTIFICATE-----")


def test_escaped_newlines_in_an_inline_pem_are_restored(monkeypatch):
    """A PEM pasted into .env arrives as one line with literal backslash-n."""
    monkeypatch.setenv("AKS_CA_CERT_PATH", PEM.replace("\n", "\\n"))
    path = AksAdapter._ca_cert_path()
    assert path is not None
    body = Path(path).read_text()
    assert "\\n" not in body
    assert body.count("\n") >= 2


def test_the_legacy_variable_still_works(monkeypatch, tmp_path):
    """AKS_CA_CERT was the old name; an existing deployment must not break."""
    ca = tmp_path / "ca.crt"
    ca.write_text(PEM)
    monkeypatch.setenv("AKS_CA_CERT", str(ca))
    assert AksAdapter._ca_cert_path() == str(ca)


def test_a_missing_file_fails_closed_rather_than_pretending(monkeypatch):
    """Returning the bad path would make verify_ssl True and every call explode.

    Returning None keeps the run alive and produces the NOT VERIFIED banner,
    which is the honest state: the operator meant to verify and is not.
    """
    monkeypatch.setenv("AKS_CA_CERT_PATH", "/definitely/not/here.crt")
    assert AksAdapter._ca_cert_path() is None


def test_the_path_setting_is_not_a_secret_field():
    """It must never be caught by the credential-in-a-name-field validator."""
    from warden.config import Settings

    s = Settings(AKS_CA_CERT_PATH="~/.warden/cluster-ca.crt")
    assert s.aks_ca_cert_path == "~/.warden/cluster-ca.crt"
