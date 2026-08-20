"""The Policy Tool Proxy, implemented as an ADK Plugin.

This is the choke point. Every tool call by every agent in the app passes through
`before_tool_callback` before the tool function is entered, and returning a
non-empty dict from it prevents the tool from running at all.

Why a Plugin rather than a separate HTTP service: ADK's plugin hooks fire for
every tool of every agent in the tree and run *before* per-agent callbacks, so
there is no path around them from inside the process. That gives enforcement
that is genuinely unbypassable in-process, with no extra network hop on the hot
path.

That is tier one. Tier two is IAM, and it is the reason this is defence in
depth rather than a single point of failure: the agent service accounts do not
hold roles/secretmanager.secretAccessor, so even a hypothetically bypassed
policy check leaves an agent unable to obtain an estate or GitHub credential.
Policy denies it in-process; IAM makes it impossible out-of-process.

Two sharp edges in ADK's callback contract, both load-bearing here:

* Plugin callbacks are KEYWORD-ONLY and use `tool_args` / `result` — the
  per-agent callbacks use `args` / `tool_response`. Getting the names wrong
  means the hook silently never fires.
* The tool is skipped whenever the return value `is not None`, but the callback
  chain only breaks on a TRUTHY return. Returning `{}` blocks the tool while
  still iterating remaining callbacks. Always deny with a non-empty dict.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from google.adk.plugins import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from warden.control_plane import policy
from warden.control_plane.budget import BudgetLedger
from warden.control_plane.store import Store
from warden.models import AgentManifest, AuditRecord, Decision

log = logging.getLogger("warden.proxy")


class WardenPolicyPlugin(BasePlugin):
    def __init__(
        self,
        *,
        manifest: AgentManifest,
        store: Store,
        ledger: BudgetLedger,
        incident_id: str,
        run_id: str,
    ) -> None:
        super().__init__(name="warden_policy")
        self._manifest = manifest
        self._store = store
        self._ledger = ledger
        self._incident_id = incident_id
        self._run_id = run_id
        self._started: dict[str, float] = {}

    # -- helpers ----------------------------------------------------------

    def _call_key(self, tool_context: ToolContext, tool: BaseTool) -> str:
        return getattr(tool_context, "function_call_id", None) or f"{tool.name}:{id(tool_context)}"

    async def _audit(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        decision: Decision,
        reason: str,
        latency_ms: int = 0,
    ) -> str:
        record = AuditRecord(
            id=str(uuid.uuid4()),
            run_id=self._run_id,
            incident_id=self._incident_id,
            agent=self._manifest.name,
            tool=tool.name,
            args_redacted=policy.redact(tool.name, tool_args),
            decision=decision,
            reason=reason,
            latency_ms=latency_ms,
        )
        await self._store.append_audit(record)
        return record.id

    @staticmethod
    def _denial(reason: str, audit_id: str) -> dict[str, Any]:
        # Non-empty by construction — see module docstring.
        return {
            "error": "denied_by_policy",
            "denied_by": "warden-policy-proxy",
            "reason": reason,
            "audit_id": audit_id,
        }

    # -- ADK hooks --------------------------------------------------------

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        """Return None to allow. Return a non-empty dict to block the call."""
        fleet = await self._store.get_fleet_state()

        verdict = policy.evaluate(
            self._manifest, tool.name, tool_args, kill_switch=fleet.kill_switch
        )
        if not verdict.allowed:
            audit_id = await self._audit(
                tool=tool, tool_args=tool_args, decision=Decision.deny, reason=verdict.reason
            )
            log.warning("DENY %s.%s — %s", self._manifest.name, tool.name, verdict.reason)
            return self._denial(verdict.reason, audit_id)

        budget = self._ledger.check()
        if not budget.ok:
            self._ledger.exceeded()
            audit_id = await self._audit(
                tool=tool, tool_args=tool_args, decision=Decision.deny, reason=budget.reason
            )
            log.warning("DENY %s.%s — %s", self._manifest.name, tool.name, budget.reason)
            return self._denial(budget.reason, audit_id)

        self._started[self._call_key(tool_context, tool)] = time.perf_counter()
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return None to keep the tool's own result."""
        started = self._started.pop(self._call_key(tool_context, tool), None)
        latency_ms = int((time.perf_counter() - started) * 1000) if started else 0

        self._ledger.add_tool_call()
        await self._audit(
            tool=tool,
            tool_args=tool_args,
            decision=Decision.allow,
            reason="",
            latency_ms=latency_ms,
        )
        return None
