"""The tool catalog: every tool the fleet can possibly call, and what it costs in scope.

Two rules make the whole governance story work:

1. A tool that is not in this catalog cannot be granted by a manifest. The
   registry loader rejects unknown tool names loudly at load time rather than
   failing mysteriously at run time.

2. `delete_workload` is deliberately registered here and deliberately granted to
   nobody. It exists so the denial is real: the agent can genuinely attempt the
   call, and policy genuinely stops it. A denial demo against a tool that does
   not exist would prove nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolSpec:
    name: str
    scopes: frozenset[str]
    mutating: bool = False
    description: str = ""
    # Args that must never reach the audit log verbatim.
    redact_args: frozenset[str] = field(default_factory=frozenset)


READ_CLUSTER = "cluster:demo:read"
WRITE_CLUSTER = "cluster:demo:write"
WRITE_PR = "repo:estate-gitops:write-pr"
READ_REPO = "repo:estate-gitops:read"
READ_MEMORY = "memory:incidents:read"


CATALOG: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in [
        ToolSpec(
            name="get_alert_context",
            scopes=frozenset(),
            description="Return the raw signal that opened this incident.",
        ),
        ToolSpec(
            name="recall_similar_incidents",
            scopes=frozenset({READ_MEMORY}),
            description="Search past incidents for a matching signature.",
        ),
        ToolSpec(
            name="describe_workload",
            scopes=frozenset({READ_CLUSTER}),
            description="Current spec and status of a workload.",
        ),
        ToolSpec(
            name="get_workload_logs",
            scopes=frozenset({READ_CLUSTER}),
            description="Recent container logs.",
        ),
        ToolSpec(
            name="recent_deploys",
            scopes=frozenset({READ_CLUSTER}),
            description="Deployment history, newest first.",
        ),
        ToolSpec(
            name="query_metrics",
            scopes=frozenset({READ_CLUSTER}),
            description="A metric series over a window.",
        ),
        ToolSpec(
            name="get_workload_status",
            scopes=frozenset({READ_CLUSTER}),
            description="Health rollup for a workload.",
        ),
        ToolSpec(
            name="read_repo_file",
            scopes=frozenset({READ_REPO}),
            description="Read a file from the GitOps repository.",
        ),
        ToolSpec(
            name="propose_patch",
            scopes=frozenset({WRITE_PR}),
            mutating=True,
            description="Open a pull request against the GitOps repository.",
        ),
        ToolSpec(
            name="request_revert",
            scopes=frozenset({WRITE_PR}),
            mutating=True,
            description="Open a pull request reverting a previous change.",
        ),
        # --- granted to nobody. See module docstring. ---
        ToolSpec(
            name="delete_workload",
            scopes=frozenset({WRITE_CLUSTER}),
            mutating=True,
            description="Delete a workload from the cluster. No manifest grants this.",
        ),
        ToolSpec(
            name="scale_workload",
            scopes=frozenset({WRITE_CLUSTER}),
            mutating=True,
            description="Scale a workload directly. No manifest grants this.",
        ),
    ]
}


def get(tool_name: str) -> ToolSpec | None:
    return CATALOG.get(tool_name)


def known(tool_name: str) -> bool:
    return tool_name in CATALOG


def unknown_tools(names: list[str]) -> list[str]:
    return [n for n in names if n not in CATALOG]
