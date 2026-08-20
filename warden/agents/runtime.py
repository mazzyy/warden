"""Running one agent, once, under governance.

Every ADK gotcha that cost time to find is handled here and commented, so it has
to be got right exactly once:

* `Runner.__init__` and `run_async` are keyword-only.
* `auto_create_session` defaults to False — the session must exist first.
* `usage_metadata` is emitted per MODEL CALL, not per turn. Sum it.
* `output_key` stores a plain dict, not the pydantic instance. Re-hydrate it.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, TypeVar

from google.adk.apps import App
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

from warden.agents.definitions import build_agent
from warden.control_plane.budget import BudgetLedger
from warden.control_plane.store import Store
from warden.models import AgentManifest, Run, RunStatus, utcnow
from warden.proxy.plugin import WardenPolicyPlugin
from warden.tools.toolbox import ToolBox

log = logging.getLogger("warden.runtime")

T = TypeVar("T", bound=BaseModel)

APP_NAME = "warden"


class AgentRun:
    """The result of one governed agent run."""

    def __init__(self, run: Run, text: str | None, structured: dict[str, Any] | None) -> None:
        self.run = run
        self.text = text
        self.structured = structured

    def parse(self, schema: type[T]) -> T | None:
        """Re-hydrate the structured output. ADK stores it as a plain dict."""
        if self.structured is None:
            return None
        return schema.model_validate(self.structured)

    def __repr__(self) -> str:  # pragma: no cover - display only
        return (
            f"<AgentRun {self.run.agent} {self.run.status} "
            f"tokens={self.run.total_tokens} tools={self.run.tool_calls}>"
        )


async def run_agent(
    *,
    manifest: AgentManifest,
    toolbox: ToolBox,
    store: Store,
    incident_id: str,
    prompt: str,
    model_override: BaseLlm | str | None = None,
) -> AgentRun:
    run = Run(
        id=str(uuid.uuid4()),
        incident_id=incident_id,
        agent=manifest.name,
        model=manifest.spec.model,
    )
    ledger = BudgetLedger(run, manifest)
    await store.put_run(run)

    tools = toolbox.build(manifest.spec.tools)
    agent = build_agent(manifest, tools, model_override=model_override)

    plugin = WardenPolicyPlugin(
        manifest=manifest,
        store=store,
        ledger=ledger,
        incident_id=incident_id,
        run_id=run.id,
    )
    app = App(name=APP_NAME, root_agent=agent, plugins=[plugin])

    sessions = InMemorySessionService()
    runner = Runner(app=app, session_service=sessions)

    session_id = f"{incident_id}-{manifest.name}-{run.id[:8]}"
    # auto_create_session defaults to False in ADK 2.x; without this, run_async
    # raises rather than creating one for you.
    await sessions.create_session(app_name=APP_NAME, user_id="warden", session_id=session_id)

    final_text: str | None = None
    try:
        async for event in runner.run_async(
            user_id="warden",
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            if event.usage_metadata:
                um = event.usage_metadata
                ledger.add_usage(
                    um.prompt_token_count or 0,
                    um.candidates_token_count or 0,
                    um.total_token_count or 0,
                )
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(p.text or "" for p in event.content.parts)
    except Exception as exc:
        run.status = RunStatus.failed
        run.outcome = f"{type(exc).__name__}: {exc}"
        run.ended_at = utcnow()
        await store.put_run(run)
        log.exception("agent %s failed", manifest.name)
        raise

    session = await sessions.get_session(
        app_name=APP_NAME, user_id="warden", session_id=session_id
    )
    structured = (session.state or {}).get("result") if session else None

    if run.status is RunStatus.running:
        run.status = RunStatus.ok
    run.ended_at = utcnow()
    run.outcome = (final_text or "")[:500]
    await store.put_run(run)

    log.info(
        "%s finished: %s tokens, %s tool calls, ~$%.4f",
        manifest.name,
        run.total_tokens,
        run.tool_calls,
        ledger.cost_usd(),
    )
    return AgentRun(run, final_text, structured)
