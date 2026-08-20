"""An in-memory estate that can be broken on demand.

This exists so the entire incident loop — triage, diagnosis, patch proposal —
can be exercised without a cluster, without Azure, and without spending a token.
It reproduces the same two failure modes as the real injection scripts, so a test
that passes here is testing the real reasoning path.
"""

from __future__ import annotations

from datetime import timedelta

from warden.models import (
    ContainerState,
    Deploy,
    LogLine,
    MetricPoint,
    MetricSeries,
    Status,
    Workload,
    WorkloadDetail,
    WorkloadRef,
    utcnow,
)

CHECKOUT = WorkloadRef(namespace="demo", name="checkout-svc", kind="Deployment")

_GOOD_ENV = {
    "PAYMENT_ENDPOINT": "https://payments.internal/v2",
    "LOG_LEVEL": "info",
    "TIMEOUT_MS": "3000",
}


class FakeAdapter:
    """Failure modes: `healthy`, `bad_config`, `oom`."""

    def __init__(self, mode: str = "healthy") -> None:
        self.mode = mode

    # -- injection ---------------------------------------------------------

    def inject(self, mode: str) -> None:
        if mode not in {"healthy", "bad_config", "oom"}:
            raise ValueError(f"unknown failure mode {mode!r}")
        self.mode = mode

    # -- EstateAdapter -----------------------------------------------------

    async def list_workloads(self, namespace: str) -> list[Workload]:
        ready = 3 if self.mode == "healthy" else 0
        return [
            Workload(
                ref=CHECKOUT,
                replicas_desired=3,
                replicas_ready=ready,
                image="ghcr.io/mazzyy/checkout-svc:1.4.2",
            )
        ]

    async def describe_workload(self, ref: WorkloadRef) -> WorkloadDetail:
        env = dict(_GOOD_ENV)
        containers = [ContainerState(name="checkout", ready=True, restart_count=0)]
        resources = {"limits": {"memory": "512Mi", "cpu": "500m"}}
        conditions = ["Available=True", "Progressing=True"]

        if self.mode == "bad_config":
            env["PAYMENT_ENDPOINT"] = "htps://payments.internal/v2"  # the typo
            containers = [
                ContainerState(
                    name="checkout",
                    ready=False,
                    restart_count=7,
                    reason="CrashLoopBackOff",
                    exit_code=1,
                )
            ]
            conditions = ["Available=False", "Progressing=False"]
        elif self.mode == "oom":
            resources = {"limits": {"memory": "64Mi", "cpu": "500m"}}
            containers = [
                ContainerState(
                    name="checkout", ready=False, restart_count=4, reason="OOMKilled", exit_code=137
                )
            ]
            conditions = ["Available=False"]

        return WorkloadDetail(
            ref=ref,
            replicas_desired=3,
            replicas_ready=3 if self.mode == "healthy" else 0,
            image="ghcr.io/mazzyy/checkout-svc:1.4.2",
            containers=containers,
            env=env,
            resources=resources,
            conditions=conditions,
        )

    async def get_workload_logs(
        self, ref: WorkloadRef, *, since_seconds: int = 900, limit: int = 200
    ) -> list[LogLine]:
        now = utcnow()
        if self.mode == "bad_config":
            lines = [
                "starting checkout-svc 1.4.2",
                "config: PAYMENT_ENDPOINT=htps://payments.internal/v2",
                'FATAL: unsupported URL scheme "htps" in PAYMENT_ENDPOINT',
                "shutting down after 0.4s",
            ]
        elif self.mode == "oom":
            lines = [
                "starting checkout-svc 1.4.2",
                "warming cache (512MB target)",
                "memory limit 64Mi reached",
                "Killed",
            ]
        else:
            lines = ["starting checkout-svc 1.4.2", "listening on :8080", "healthz ok"]

        return [
            LogLine(ts=now - timedelta(seconds=(len(lines) - i) * 5), container="checkout", message=m)
            for i, m in enumerate(lines)
        ][:limit]

    async def recent_deploys(self, ref: WorkloadRef, *, limit: int = 10) -> list[Deploy]:
        now = utcnow()
        deploys = [
            Deploy(
                ts=now - timedelta(minutes=4),
                revision="r42",
                image="ghcr.io/mazzyy/checkout-svc:1.4.2",
                changed_by="mazzyy",
                commit_sha="9f2c1ab",
                summary="chore: tune payment endpoint and timeouts",
            ),
            Deploy(
                ts=now - timedelta(days=2),
                revision="r41",
                image="ghcr.io/mazzyy/checkout-svc:1.4.1",
                changed_by="mazzyy",
                commit_sha="41ddb07",
                summary="feat: add retry budget",
            ),
        ]
        return deploys[:limit]

    async def query_metrics(
        self, ref: WorkloadRef, *, metric: str, window: str = "15m"
    ) -> MetricSeries:
        now = utcnow()
        healthy = self.mode == "healthy"
        base = 0.01 if healthy else 0.94
        return MetricSeries(
            metric=metric,
            unit="ratio" if "error" in metric else "count",
            points=[
                MetricPoint(ts=now - timedelta(minutes=m), value=base)
                for m in range(10, 0, -1)
            ],
        )

    async def get_workload_status(self, ref: WorkloadRef) -> Status:
        healthy = self.mode == "healthy"
        summary = {
            "healthy": "3/3 replicas ready",
            "bad_config": "0/3 replicas ready — CrashLoopBackOff",
            "oom": "0/3 replicas ready — OOMKilled",
        }[self.mode]
        return Status(ref=ref, healthy=healthy, summary=summary)
