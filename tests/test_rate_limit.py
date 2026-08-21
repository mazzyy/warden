"""Rate-limit retry.

This is a demo-reliability test, not a correctness one. The Gemini API free tier
allows 5 requests per minute per model and one incident makes 7-9 model calls
back to back, so an unretried run fails almost every time. The hackathon forbids
editing around a failure in the demo video — a 429 mid-recording ends the take.
"""

from __future__ import annotations

import pytest

from warden.agents import runtime


class FakeResourceExhausted(Exception):
    """Shaped like ADK's private _ResourceExhaustedError."""


RATE_LIMIT_TEXT = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
    "current quota... Please retry in 59.328362286s.', 'status': 'RESOURCE_EXHAUSTED'}}"
)


def test_recognises_a_rate_limit_from_the_message():
    # Matched on text, not by importing ADK's private class — that would break
    # on any release where the class is renamed or moved.
    assert runtime._is_rate_limited(FakeResourceExhausted(RATE_LIMIT_TEXT))
    assert runtime._is_rate_limited(Exception("429 Too Many Requests"))


def test_does_not_retry_unrelated_failures():
    assert not runtime._is_rate_limited(ValueError("tool not found"))
    assert not runtime._is_rate_limited(KeyError("missing manifest"))


def test_prefers_the_servers_own_retry_delay():
    delay = runtime._retry_after(FakeResourceExhausted(RATE_LIMIT_TEXT), attempt=0)
    # 59.3s + 1s of margin. Backing off less than the server asked for just
    # earns another 429.
    assert 60.0 <= delay <= 61.0


def test_falls_back_to_exponential_backoff():
    plain = Exception("429 RESOURCE_EXHAUSTED with no delay hint")
    assert runtime._retry_after(plain, attempt=0) == 5.0
    assert runtime._retry_after(plain, attempt=1) == 10.0
    assert runtime._retry_after(plain, attempt=2) == 20.0


def test_backoff_is_capped():
    plain = Exception("429 RESOURCE_EXHAUSTED")
    assert runtime._retry_after(plain, attempt=10) == 60.0
    long_wait = Exception("429 RESOURCE_EXHAUSTED. Please retry in 3600s.")
    assert runtime._retry_after(long_wait, attempt=0) == 90.0


@pytest.mark.asyncio
async def test_run_agent_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise FakeResourceExhausted(RATE_LIMIT_TEXT)
        return "ok"

    async def no_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(runtime, "_execute", flaky)
    monkeypatch.setattr(runtime.asyncio, "sleep", no_sleep)

    result = await runtime.run_agent(
        manifest=_stub_manifest(), toolbox=None, store=None, incident_id="INC", prompt="go"
    )
    assert result == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2


@pytest.mark.asyncio
async def test_run_agent_gives_up_and_reraises(monkeypatch):
    async def always_limited(**kwargs):
        raise FakeResourceExhausted(RATE_LIMIT_TEXT)

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(runtime, "_execute", always_limited)
    monkeypatch.setattr(runtime.asyncio, "sleep", no_sleep)

    with pytest.raises(FakeResourceExhausted):
        await runtime.run_agent(
            manifest=_stub_manifest(),
            toolbox=None,
            store=None,
            incident_id="INC",
            prompt="go",
            max_retries=2,
        )


@pytest.mark.asyncio
async def test_a_real_bug_is_not_swallowed_by_the_retry(monkeypatch):
    """A retry loop that hides genuine errors is worse than no retry loop."""
    calls = {"n": 0}

    async def broken(**kwargs):
        calls["n"] += 1
        raise ValueError("propose_patch got 2 files and 1 content")

    monkeypatch.setattr(runtime, "_execute", broken)

    with pytest.raises(ValueError, match="propose_patch"):
        await runtime.run_agent(
            manifest=_stub_manifest(), toolbox=None, store=None, incident_id="INC", prompt="go"
        )
    assert calls["n"] == 1, "a non-rate-limit failure must fail immediately"


def _stub_manifest():
    from warden.control_plane.registry import load_all

    return load_all("manifests/agents")["triage"]
