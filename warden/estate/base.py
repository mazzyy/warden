"""The estate boundary (ADR-005).

Everything above this line is cloud-agnostic. The agents, their prompts, the tool
schemas and the policy engine never learn whether they are looking at AKS, GKE or
Cloud Run — which is the entire reason the Azure-now / Google-later migration is
a config change rather than a rewrite.

Implementations: AksAdapter (today, on your Azure credits), GkeAdapter (when the
$150 lands), FakeAdapter (tests and offline development).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from warden.models import (
    Deploy,
    LogLine,
    MetricSeries,
    Status,
    Workload,
    WorkloadDetail,
    WorkloadRef,
)


@runtime_checkable
class EstateAdapter(Protocol):
    """Read-only by contract, and read-only by credential.

    There is deliberately no mutating method on this interface. The fleet's only
    write primitive lives in the GitHub tools, and it opens a pull request.
    """

    async def list_workloads(self, namespace: str) -> list[Workload]: ...

    async def describe_workload(self, ref: WorkloadRef) -> WorkloadDetail: ...

    async def get_workload_logs(
        self, ref: WorkloadRef, *, since_seconds: int = 900, limit: int = 200
    ) -> list[LogLine]: ...

    async def recent_deploys(self, ref: WorkloadRef, *, limit: int = 10) -> list[Deploy]: ...

    async def query_metrics(
        self, ref: WorkloadRef, *, metric: str, window: str = "15m"
    ) -> MetricSeries: ...

    async def get_workload_status(self, ref: WorkloadRef) -> Status: ...


def build_adapter(kind: str) -> EstateAdapter:
    """Factory. `ESTATE_ADAPTER` selects the implementation at boot."""
    if kind == "fake":
        from warden.estate.fake import FakeAdapter

        return FakeAdapter()
    if kind == "aks":
        from warden.estate.aks import AksAdapter

        return AksAdapter()
    if kind == "gke":  # pragma: no cover - lands when the Google credits do
        from warden.estate.gke import GkeAdapter

        return GkeAdapter()
    raise ValueError(f"unknown ESTATE_ADAPTER {kind!r} (expected: aks, gke, fake)")
