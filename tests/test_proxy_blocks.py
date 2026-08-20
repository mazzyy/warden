"""Integration test: the policy plugin actually stops the tool from running.

The unit tests in test_policy.py prove the *decision* is right. This proves the
*enforcement* is real — that a denied tool function is never entered, inside a
genuine ADK agent run, through ADK's own callback machinery.

It uses a stub model rather than a live one, so it costs nothing and runs in CI.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from warden.control_plane.budget import BudgetLedger
from warden.control_plane.registry import load_all
from warden.control_plane.store import InMemoryStore
from warden.models import Decision, Run
from warden.proxy.plugin import WardenPolicyPlugin

# Set by the tool if it is ever entered. It must never be.
TOOL_RAN: list[str] = []


def delete_workload(namespace: str, name: str) -> dict:
    """Delete a workload from the cluster.

    Args:
        namespace: The namespace.
        name: The workload name.
    """
    TOOL_RAN.append(f"{namespace}/{name}")  # pragma: no cover - the point is this never runs
    return {"status": "deleted"}


def get_workload_logs(namespace: str, name: str) -> dict:
    """Return recent logs for a workload.

    Args:
        namespace: The namespace.
        name: The workload name.
    """
    return {"lines": ["FATAL: unsupported URL scheme"]}


# Stub-model state. Module-level because BaseLlm is a pydantic model and will not
# take arbitrary instance attributes — `tool` is what it attempts on its first
# turn, `turn` is the per-test call counter.
SCRIPT: dict[str, object] = {"tool": "delete_workload", "turn": 0}


def _reset_script(tool: str) -> None:
    SCRIPT["tool"] = tool
    SCRIPT["turn"] = 0


class ScriptedLlm(BaseLlm):
    """Emits a fixed sequence of turns so the test is deterministic and free.

    Note the subtlety that makes these tests meaningful: the tool being attempted
    must be genuinely wired into `agent.tools`, or ADK raises "Tool not found"
    before any callback fires — which would prove nothing about policy. The
    denial we care about happens when the tool IS reachable by the framework and
    the manifest still refuses it.
    """

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        turn = int(SCRIPT["turn"])
        SCRIPT["turn"] = turn + 1
        if turn == 0:
            part = types.Part(
                function_call=types.FunctionCall(
                    name=SCRIPT["tool"], args={"namespace": "demo", "name": "checkout-svc"}
                )
            )
        else:
            part = types.Part(text="I could not remove the workload; policy denied it.")
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=120, candidates_token_count=40, total_token_count=160
            ),
        )


async def _run_once(
    agent_name: str, tools: list, *, tool_to_attempt: str = "delete_workload"
) -> tuple[InMemoryStore, BudgetLedger]:
    _reset_script(tool_to_attempt)
    fleet = load_all("manifests/agents")
    manifest = fleet[agent_name]
    store = InMemoryStore()
    run = Run(
        id=str(uuid.uuid4()),
        incident_id="INC-TEST",
        agent=manifest.name,
        model=manifest.spec.model,
    )
    ledger = BudgetLedger(run, manifest)

    model = ScriptedLlm(model="stub-model")

    agent = LlmAgent(
        name=agent_name,
        model=model,
        instruction="Resolve the incident.",
        tools=tools,
    )
    plugin = WardenPolicyPlugin(
        manifest=manifest, store=store, ledger=ledger, incident_id="INC-TEST", run_id=run.id
    )
    app = App(name="warden-test", root_agent=agent, plugins=[plugin])

    sessions = InMemorySessionService()
    runner = Runner(app=app, session_service=sessions)
    # auto_create_session defaults to False in ADK 2.x — the session must exist.
    await sessions.create_session(app_name="warden-test", user_id="test", session_id="s1")

    async for event in runner.run_async(
        user_id="test",
        session_id="s1",
        new_message=types.Content(role="user", parts=[types.Part(text="checkout-svc is down")]),
    ):
        # usage_metadata is emitted per model call — sum it, never last-value it.
        if event.usage_metadata:
            um = event.usage_metadata
            ledger.add_usage(
                um.prompt_token_count or 0,
                um.candidates_token_count or 0,
                um.total_token_count or 0,
            )
    return store, ledger


@pytest.mark.asyncio
async def test_denied_tool_never_executes():
    TOOL_RAN.clear()
    store, _ = await _run_once("remediator", [delete_workload])

    assert TOOL_RAN == [], "the denied tool function was entered — enforcement is not real"

    audit = await store.list_audit()
    denials = [a for a in audit if a.decision is Decision.deny]
    assert denials, "no denial was recorded"
    assert denials[0].tool == "delete_workload"
    assert "allow-list" in denials[0].reason


@pytest.mark.asyncio
async def test_the_denial_is_audited_with_a_readable_reason():
    TOOL_RAN.clear()
    store, _ = await _run_once("remediator", [delete_workload])
    record = (await store.list_audit())[0]

    assert record.agent == "remediator"
    assert record.incident_id == "INC-TEST"
    # This string appears on screen in the demo — keep it human.
    assert "remediator" in record.reason
    assert "tool_context" not in record.args_redacted


@pytest.mark.asyncio
async def test_token_usage_is_summed_across_model_calls():
    TOOL_RAN.clear()
    _, ledger = await _run_once("remediator", [delete_workload])
    # Two model turns at 160 total tokens each; a last-value read would give 160.
    assert ledger.run.total_tokens == 320, ledger.run.total_tokens
    assert ledger.cost_usd() > 0


@pytest.mark.asyncio
async def test_kill_switch_halts_a_tool_the_agent_is_otherwise_allowed():
    """The diagnostician may read logs — until the kill switch is thrown."""
    TOOL_RAN.clear()
    _reset_script("get_workload_logs")
    fleet = load_all("manifests/agents")
    manifest = fleet["diagnostician"]
    store = InMemoryStore()
    state = await store.get_fleet_state()
    state.kill_switch = True
    await store.set_fleet_state(state)

    run = Run(id=str(uuid.uuid4()), incident_id="INC-TEST", agent="diagnostician", model="stub")
    ledger = BudgetLedger(run, manifest)
    model = ScriptedLlm(model="stub-model")

    agent = LlmAgent(name="diagnostician", model=model, instruction="Diagnose.", tools=[get_workload_logs])
    plugin = WardenPolicyPlugin(
        manifest=manifest, store=store, ledger=ledger, incident_id="INC-TEST", run_id=run.id
    )
    app = App(name="warden-test", root_agent=agent, plugins=[plugin])
    sessions = InMemorySessionService()
    runner = Runner(app=app, session_service=sessions)
    await sessions.create_session(app_name="warden-test", user_id="test", session_id="s2")

    async for _ in runner.run_async(
        user_id="test",
        session_id="s2",
        new_message=types.Content(role="user", parts=[types.Part(text="go")]),
    ):
        pass

    audit = await store.list_audit()
    assert any("kill switch" in a.reason for a in audit if a.decision is Decision.deny)
