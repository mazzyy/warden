"""Running one agent, once, under governance.

Every ADK gotcha that cost time to find is handled here and commented, so it has
to be got right exactly once:

* `Runner.__init__` and `run_async` are keyword-only.
* `auto_create_session` defaults to False — the session must exist first.
* `usage_metadata` is emitted per MODEL CALL, not per turn. Sum it.
* `output_key` stores a plain dict, not the pydantic instance. Re-hydrate it.
"""

from __future__ import annotations

import asyncio
import logging
import re
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


RATE_LIMIT_MARKERS = ("RESOURCE_EXHAUSTED", "429", "ResourceExhausted")


def _is_rate_limited(exc: Exception) -> bool:
    """ADK raises a private _ResourceExhaustedError, so match on the message.

    Catching a private class by import would break on any ADK release; the
    error text is the stable surface here.
    """
    text = f"{type(exc).__name__} {exc}"
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


def _retry_after(exc: Exception, attempt: int) -> float:
    """Prefer the server's own retryDelay; fall back to exponential backoff."""
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", str(exc))
    if match:
        return min(float(match.group(1)) + 1.0, 90.0)
    return min(5.0 * (2**attempt), 60.0)


async def run_agent(
    *,
    manifest: AgentManifest,
    toolbox: ToolBox,
    store: Store,
    incident_id: str,
    prompt: str,
    model_override: BaseLlm | str | None = None,
    max_retries: int = 3,
) -> AgentRun:
    """Run one agent under governance, retrying through rate limits.

    The retry is not politeness — it is a demo-reliability requirement. The
    Gemini API free tier allows 5 requests per minute per model, and a single
    incident makes 7-9 model calls back to back, so an unretried run fails
    roughly every time. The hackathon forbids editing around a failure in the
    demo video, which means a 429 mid-recording ends the take.

    Vertex has proper quotas and is the right answer for the deployed service.
    This exists so that a transient limit anywhere degrades into a pause rather
    than a dead run.
    """
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await _execute(
                manifest=manifest,
                toolbox=toolbox,
                store=store,
                incident_id=incident_id,
                prompt=prompt,
                model_override=model_override,
            )
        except Exception as exc:
            last = exc
            if not _is_rate_limited(exc) or attempt == max_retries:
                raise
            delay = _retry_after(exc, attempt)
            log.warning(
                "%s rate limited (attempt %d/%d) — waiting %.0fs. "
                "Set GOOGLE_GENAI_USE_ENTERPRISE=1 to use Vertex and avoid the free-tier cap.",
                manifest.name,
                attempt + 1,
                max_retries,
                delay,
            )
            await asyncio.sleep(delay)
    raise last  # unreachable; keeps type checkers happy


async def _execute(
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

    tools = toolbox.build(
        manifest.spec.tools, max_changed_lines=manifest.spec.blast_radius.max_changed_lines
    )
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
