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

import json
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

    # Long enough to read a diagnosis against, short enough that a run's audit
    # log stays a log rather than a copy of the cluster.
    MAX_RESULT_CHARS = 4000

    # Result keys whose values never belong in an audit trail. Tool results
    # here are cluster reads, and a pod spec carries its container's entire
    # environment — which in a real estate is where credentials live. The
    # policy proxy already redacts arguments; results were the other half, and
    # storing them unredacted would have turned the audit log into the most
    # readable secret store in the system.
    # Matched against the key with every non-alphanumeric character stripped,
    # so one hint covers API_KEY, api-key, apiKey and x-api-key at once. Listing
    # spellings instead of normalising is how `x-api-key` slipped through the
    # first version of this — caught by its own test, which is the only reason
    # to write tests for a redactor.
    SECRET_HINTS = (
        "password",
        "passwd",
        "secret",
        "token",
        "credential",
        "apikey",
        "privatekey",
        "authorization",
        "bearer",
    )

    @staticmethod
    def _normalise(key: Any) -> str:
        return "".join(ch for ch in str(key).lower() if ch.isalnum())

    @classmethod
    def _scrub(cls, value: Any) -> Any:
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                flat = cls._normalise(k)
                if any(hint in flat for hint in cls.SECRET_HINTS):
                    out[k] = "«redacted»"
                else:
                    out[k] = cls._scrub(v)
            return out
        if isinstance(value, list):
            return [cls._scrub(v) for v in value]
        return value

    @classmethod
    def _preview(cls, result: Any) -> tuple[str, bool]:
        """Render a tool result for the audit log. Never raises."""
        if result is None:
            return "", False
        try:
            text = json.dumps(cls._scrub(result), indent=2, default=str, ensure_ascii=False)
        except Exception:
            text = str(result)
        if len(text) > cls.MAX_RESULT_CHARS:
            return text[: cls.MAX_RESULT_CHARS] + "\n… truncated", True
        return text, False

    async def _audit(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        decision: Decision,
        reason: str,
        latency_ms: int = 0,
        result: Any = None,
    ) -> str:
        preview, truncated = self._preview(result)
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
            result_preview=preview,
            result_truncated=truncated,
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
            result=result,
        )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        """A tool that raised still happened, and must still be audited.

        `after_tool_callback` does not fire on an exception, so without this
        hook a call that blew up — a cluster timeout, a TLS failure — would
        leave no trace at all. An audit log that silently drops the failures is
        worse than no audit log, because it reads as a clean run.
        """
        started = self._started.pop(self._call_key(tool_context, tool), None)
        latency_ms = int((time.perf_counter() - started) * 1000) if started else 0

        self._ledger.add_tool_call()
        await self._audit(
            tool=tool,
            tool_args=tool_args,
            decision=Decision.allow,
            reason=f"tool raised {type(error).__name__}",
            latency_ms=latency_ms,
            result={"error": type(error).__name__, "detail": str(error)[:500]},
        )
        log.warning("%s.%s raised %s", self._manifest.name, tool.name, error)
        return None
