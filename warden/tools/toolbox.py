"""Tool implementations, built per-run with their dependencies captured.

ADK derives a tool's schema from the function signature and docstring, and its
name from `__name__`. So each tool here is a closure produced by a factory: the
estate adapter, store and GitHub client are captured at build time rather than
reached for through a global, and the function ADK sees is still a plain,
introspectable Python function whose name matches the catalog entry.

Nothing in this module checks permissions. That is deliberate — authorisation
lives in exactly one place (warden/proxy/plugin.py), and a tool that also
half-enforced policy would be a second place to get it wrong.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from warden.control_plane.store import Store
from warden.estate.base import EstateAdapter
from warden.models import WorkloadRef

log = logging.getLogger("warden.tools")


class ToolBox:
    def __init__(
        self,
        *,
        estate: EstateAdapter,
        store: Store,
        github: Any | None = None,
        namespace: str = "demo",
        alert_context: dict[str, Any] | None = None,
    ) -> None:
        self._estate = estate
        self._store = store
        self._github = github
        self._namespace = namespace
        self._alert = alert_context or {}
        # Set by the orchestrator once the incident exists. Without it,
        # recall_similar_incidents returns the incident currently being
        # triaged and the agent correctly concludes it is a duplicate of
        # itself — closing the very incident it was asked to open.
        self._current_incident_id: str | None = None

    def bind_incident(self, incident_id: str) -> None:
        self._current_incident_id = incident_id

    # -- assembly ----------------------------------------------------------

    def build(self, tool_names: list[str], *, max_changed_lines: int = 0) -> list[Callable]:
        """Return the callables for the tools a manifest grants."""
        self._max_changed_lines = max_changed_lines
        factories = {
            "get_alert_context": self._get_alert_context,
            "recall_similar_incidents": self._recall_similar_incidents,
            "describe_workload": self._describe_workload,
            "get_workload_logs": self._get_workload_logs,
            "recent_deploys": self._recent_deploys,
            "query_metrics": self._query_metrics,
            "get_workload_status": self._get_workload_status,
            "list_repo_files": self._list_repo_files,
            "read_repo_file": self._read_repo_file,
            "propose_patch": self._propose_patch,
            "request_revert": self._request_revert,
            # Registered so an agent can genuinely attempt it and policy can
            # genuinely refuse. Granted by no manifest. See tools/catalog.py.
            "delete_workload": self._delete_workload,
        }
        missing = [n for n in tool_names if n not in factories]
        if missing:
            raise ValueError(f"no implementation for tool(s): {', '.join(missing)}")
        return [factories[n]() for n in tool_names]

    def _ref(self, name: str, namespace: str | None = None) -> WorkloadRef:
        return WorkloadRef(namespace=namespace or self._namespace, name=name)

    async def estate_status(self, name: str, namespace: str | None = None):
        """Ground truth for the orchestrator, outside the agent loop.

        Whether an incident is resolved is a fact about the cluster, not an
        opinion the Verifier holds — so the orchestrator reads it directly
        rather than trusting the model's summary.
        """
        try:
            return await self._estate.get_workload_status(self._ref(name, namespace))
        except Exception:
            return None

    # -- context and memory ------------------------------------------------

    def _get_alert_context(self) -> Callable:
        alert = self._alert

        def get_alert_context() -> dict:
            """Return the raw signal that opened this incident.

            Includes the source, the alert text and the workload it refers to.
            """
            return {"alert": alert}

        return get_alert_context

    def _recall_similar_incidents(self) -> Callable:
        store = self._store
        box = self

        async def recall_similar_incidents(signature: str) -> dict:
            """Find PAST incidents with a matching signature.

            Excludes the incident currently being triaged. Returns an empty
            list when this failure has not been seen before.

            Args:
                signature: A short stable string identifying the failure mode,
                    for example "checkout-svc/CrashLoopBackOff".
            """
            incidents = await store.list_incidents(limit=50)
            matches = [
                {
                    "id": i.id,
                    "signature": i.signature,
                    "status": str(i.status),
                    "opened_at": i.opened_at.isoformat(),
                    "pr_url": i.pr_url,
                }
                for i in incidents
                # The self-exclusion is the whole point. See bind_incident.
                if i.id != box._current_incident_id
                and signature.lower() in i.signature.lower()
            ]
            return {"matches": matches, "count": len(matches)}

        return recall_similar_incidents

    # -- estate reads ------------------------------------------------------

    def _describe_workload(self) -> Callable:
        estate, ref = self._estate, self._ref

        async def describe_workload(name: str, namespace: str = "demo") -> dict:
            """Return the current spec and status of a workload.

            Args:
                name: The workload name, for example "checkout-svc".
                namespace: The namespace it lives in.
            """
            detail = await estate.describe_workload(ref(name, namespace))
            return detail.model_dump(mode="json")

        return describe_workload

    def _get_workload_logs(self) -> Callable:
        estate, ref = self._estate, self._ref

        async def get_workload_logs(
            name: str, namespace: str = "demo", since_seconds: int = 900, limit: int = 100
        ) -> dict:
            """Return recent container logs for a workload.

            Args:
                name: The workload name.
                namespace: The namespace it lives in.
                since_seconds: How far back to look.
                limit: Maximum lines to return.
            """
            lines = await estate.get_workload_logs(
                ref(name, namespace), since_seconds=since_seconds, limit=limit
            )
            return {"lines": [f"{ln.ts.isoformat()} [{ln.container}] {ln.message}" for ln in lines]}

        return get_workload_logs

    def _recent_deploys(self) -> Callable:
        estate, ref = self._estate, self._ref

        async def recent_deploys(name: str, namespace: str = "demo", limit: int = 10) -> dict:
            """Return deployment history for a workload, newest first.

            Args:
                name: The workload name.
                namespace: The namespace it lives in.
                limit: Maximum entries to return.
            """
            deploys = await estate.recent_deploys(ref(name, namespace), limit=limit)
            return {"deploys": [d.model_dump(mode="json") for d in deploys]}

        return recent_deploys

    def _query_metrics(self) -> Callable:
        estate, ref = self._estate, self._ref

        async def query_metrics(
            name: str, metric: str, namespace: str = "demo", window: str = "15m"
        ) -> dict:
            """Return a metric series for a workload over a time window.

            Args:
                name: The workload name.
                metric: Metric name, for example "error_rate" or "restarts".
                namespace: The namespace it lives in.
                window: Lookback window, for example "15m" or "1h".
            """
            series = await estate.query_metrics(ref(name, namespace), metric=metric, window=window)
            values = [p.value for p in series.points]
            return {
                "metric": series.metric,
                "unit": series.unit,
                "latest": values[-1] if values else None,
                "mean": sum(values) / len(values) if values else None,
                "points": len(values),
            }

        return query_metrics

    def _get_workload_status(self) -> Callable:
        estate, ref = self._estate, self._ref

        async def get_workload_status(name: str, namespace: str = "demo") -> dict:
            """Return a health rollup for a workload.

            Args:
                name: The workload name.
                namespace: The namespace it lives in.
            """
            status = await estate.get_workload_status(ref(name, namespace))
            return status.model_dump(mode="json")

        return get_workload_status

    # -- the write path: pull requests only --------------------------------

    def _list_repo_files(self) -> Callable:
        gh = self._github

        async def list_repo_files(prefix: str = "") -> dict:
            """List the files in the GitOps repository.

            Call this BEFORE read_repo_file rather than guessing a path. A live
            run showed the remediator probing four candidate paths and 404ing on
            two of them, which cost more tokens than the rest of the incident.

            Args:
                prefix: Optional path prefix to filter by, e.g. "apps/".
            """
            if gh is None:
                return {"error": "no GitHub client configured"}
            return await gh.list_files(prefix)

        return list_repo_files

    def _read_repo_file(self) -> Callable:
        gh = self._github

        async def read_repo_file(path: str) -> dict:
            """Read a file from the GitOps repository.

            Use list_repo_files first to find the exact path. Guessing wastes a
            round trip and returns a 404 error dict, not the file.

            Args:
                path: Repository-relative path, e.g. "apps/checkout-svc/deployment.yaml".
            """
            if gh is None:
                return {"error": "no GitHub client configured", "path": path}
            return await gh.read_file(path)

        return read_repo_file

    def _propose_patch(self) -> Callable:
        gh = self._github
        max_lines = getattr(self, "_max_changed_lines", 0)

        async def propose_patch(
            title: str, rationale: str, files: list[str], contents: list[str]
        ) -> dict:
            """Open a pull request against the GitOps repository.

            This is the only way anything in this system changes the estate. The
            pull request is opened on a new branch; it is not merged, and this
            agent cannot merge it.

            Args:
                title: The pull request title.
                rationale: Why this change fixes the incident. Becomes the PR body.
                files: Repository-relative paths to change.
                contents: New full contents for each path, in the same order as files.
            """
            if gh is None:
                return {"error": "no GitHub client configured"}
            if len(files) != len(contents):
                return {"error": f"files ({len(files)}) and contents ({len(contents)}) differ in length"}
            return await gh.open_pull_request(
                title=title,
                body=rationale,
                changes=dict(zip(files, contents, strict=True)),
                max_changed_lines=max_lines,
            )

        return propose_patch

    def _request_revert(self) -> Callable:
        gh = self._github

        async def request_revert(pr_number: int, reason: str) -> dict:
            """Open a pull request reverting a previously merged change.

            Args:
                pr_number: The pull request number to revert.
                reason: Why the revert is needed. Becomes the PR body.
            """
            if gh is None:
                return {"error": "no GitHub client configured"}
            return await gh.open_revert(pr_number=pr_number, reason=reason)

        return request_revert

    # -- the tool nobody is granted ----------------------------------------

    def _delete_workload(self) -> Callable:
        def delete_workload(name: str, namespace: str = "demo") -> dict:
            """Delete a workload from the cluster.

            Args:
                name: The workload name.
                namespace: The namespace it lives in.
            """
            # Unreachable. The policy proxy denies this for every agent in the
            # fleet, and tests/test_policy.py asserts that over the whole fleet.
            raise AssertionError(
                "delete_workload executed — the policy proxy failed to block it. "
                "This is a security regression, not a bug."
            )

        return delete_workload
