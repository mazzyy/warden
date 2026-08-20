"""The policy engine.

Pure functions over (manifest, tool, args). No I/O, no clients, no clock — which
is what makes it trivially testable and what lets the denial test in
tests/test_policy.py be the load-bearing test of the whole project.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from warden.models import AgentManifest, Decision
from warden.tools import catalog


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.allow


ALLOW = PolicyResult(Decision.allow)


def _deny(reason: str) -> PolicyResult:
    return PolicyResult(Decision.deny, reason)


def evaluate(
    manifest: AgentManifest,
    tool_name: str,
    args: dict[str, Any],
    *,
    kill_switch: bool = False,
) -> PolicyResult:
    """Decide whether `manifest`'s agent may call `tool_name` with `args`.

    Checks run cheapest-first and short-circuit. Every deny reason names the
    specific thing that was missing — these strings appear on screen in the
    demo and in the audit log, so they are user-facing copy.
    """
    if kill_switch:
        return _deny("fleet kill switch is engaged; all tool dispatch is halted")

    spec = catalog.get(tool_name)
    if spec is None:
        return _deny(f"tool {tool_name!r} is not in the tool catalog")

    if tool_name not in manifest.spec.tools:
        return _deny(
            f"tool {tool_name!r} is not in the manifest allow-list for agent "
            f"{manifest.name!r} (allowed: {', '.join(sorted(manifest.spec.tools)) or 'none'})"
        )

    granted = set(manifest.spec.scopes)
    missing = spec.scopes - granted
    if missing:
        return _deny(
            f"agent {manifest.name!r} lacks scope(s) {', '.join(sorted(missing))} "
            f"required by tool {tool_name!r}"
        )

    return _check_blast_radius(manifest, tool_name, args)


def _check_blast_radius(
    manifest: AgentManifest, tool_name: str, args: dict[str, Any]
) -> PolicyResult:
    radius = manifest.spec.blast_radius

    ns = args.get("namespace")
    if ns is not None and ns != radius.namespace:
        return _deny(
            f"namespace {ns!r} is outside the blast radius for agent "
            f"{manifest.name!r} (permitted: {radius.namespace!r})"
        )

    files = args.get("files") or args.get("paths")
    if isinstance(files, (list, tuple)) and len(files) > radius.max_files_per_patch:
        return _deny(
            f"patch touches {len(files)} files, exceeding maxFilesPerPatch="
            f"{radius.max_files_per_patch} for agent {manifest.name!r}"
        )

    return ALLOW


def redact(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Strip anything that must not be persisted to the audit log verbatim."""
    spec = catalog.get(tool_name)
    if spec is None:
        return dict(args)
    return {
        k: ("<redacted>" if k in spec.redact_args else v)
        for k, v in args.items()
        # ToolContext is injected by ADK and is not serialisable.
        if k != "tool_context"
    }
