"""The Azure estate adapter.

Read-only by contract (EstateAdapter has no mutating method) and read-only by
credential: it authenticates with a ServiceAccount token bound to a Role that
grants get/list/watch and nothing else. Running `kubectl delete` with this token
returns Forbidden — see docs/SETUP.md §8, and keep that terminal output, it
belongs in the demo video.

The official Kubernetes client is synchronous, so every call is wrapped in
asyncio.to_thread rather than pulling in a second async client library.

On metrics: rather than require a Prometheus stack the estate does not have,
metrics are derived from workload state — restart counts, ready ratios, recent
event counts. That is honest about what it is, and it is enough for the
Diagnostician to reason about blast radius.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from warden.config import settings
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

log = logging.getLogger("warden.estate.aks")


@lru_cache
def _secret(name: str) -> str | None:
    """Read a secret from Google Secret Manager, falling back to the environment.

    Only sa-proxy holds roles/secretmanager.secretAccessor, so an agent process
    calling this will fail — by design.
    """
    env_key = name.upper().replace("-", "_")
    if os.environ.get(env_key):
        return os.environ[env_key]
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        path = f"projects/{settings().gcp_project}/secrets/{name}/versions/latest"
        return client.access_secret_version(name=path).payload.data.decode()
    except Exception as exc:
        log.warning("could not read secret %s: %s", name, exc)
        return None


class AksAdapter:
    def __init__(self, *, apiserver: str | None = None, token: str | None = None) -> None:
        s = settings()
        self._apiserver = apiserver or _secret(s.secret_aks_apiserver)
        self._token = token or _secret(s.secret_aks_token)
        self._core = None
        self._apps = None

    # -- client ------------------------------------------------------------

    def _clients(self):
        if self._core is None:
            from kubernetes import client

            if not self._apiserver or not self._token:
                raise RuntimeError(
                    "AksAdapter needs an API server and a read-only token. Set "
                    "AKS_APISERVER and AKS_READER_TOKEN, or grant this service "
                    "account access to the Secret Manager secrets. See docs/SETUP.md §8."
                )
            cfg = client.Configuration()
            cfg.host = self._apiserver
            cfg.api_key = {"authorization": f"Bearer {self._token}"}
            # The AKS API server presents a cert chain the client cannot verify
            # without the cluster CA. Mount the CA and set cfg.ssl_ca_cert for
            # production; this is acceptable for a hackathon estate reached with
            # a read-only token, but it is a real trade-off, not an oversight.
            cfg.verify_ssl = bool(os.environ.get("AKS_CA_CERT"))
            if cfg.verify_ssl:
                cfg.ssl_ca_cert = os.environ["AKS_CA_CERT"]
            api = client.ApiClient(cfg)
            self._core = client.CoreV1Api(api)
            self._apps = client.AppsV1Api(api)
        return self._core, self._apps

    # -- EstateAdapter -----------------------------------------------------

    async def list_workloads(self, namespace: str) -> list[Workload]:
        def _list():
            _, apps = self._clients()
            items = apps.list_namespaced_deployment(namespace).items
            return [
                Workload(
                    ref=WorkloadRef(namespace=namespace, name=d.metadata.name),
                    replicas_desired=d.spec.replicas or 0,
                    replicas_ready=d.status.ready_replicas or 0,
                    image=d.spec.template.spec.containers[0].image if d.spec.template.spec.containers else "",
                )
                for d in items
            ]

        return await asyncio.to_thread(_list)

    async def describe_workload(self, ref: WorkloadRef) -> WorkloadDetail:
        def _describe():
            core, apps = self._clients()
            dep = apps.read_namespaced_deployment(ref.name, ref.namespace)
            container = dep.spec.template.spec.containers[0]

            env = {e.name: (e.value or "") for e in (container.env or [])}
            resources = {}
            if container.resources:
                resources = {
                    "limits": dict(container.resources.limits or {}),
                    "requests": dict(container.resources.requests or {}),
                }

            pods = core.list_namespaced_pod(
                ref.namespace, label_selector=",".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items())
            ).items

            containers: list[ContainerState] = []
            for pod in pods:
                for cs in pod.status.container_statuses or []:
                    reason = None
                    exit_code = None
                    if cs.state and cs.state.waiting:
                        reason = cs.state.waiting.reason
                    elif cs.state and cs.state.terminated:
                        reason = cs.state.terminated.reason
                        exit_code = cs.state.terminated.exit_code
                    elif cs.last_state and cs.last_state.terminated:
                        reason = cs.last_state.terminated.reason
                        exit_code = cs.last_state.terminated.exit_code
                    containers.append(
                        ContainerState(
                            name=cs.name,
                            ready=bool(cs.ready),
                            restart_count=cs.restart_count or 0,
                            reason=reason,
                            exit_code=exit_code,
                        )
                    )

            conditions = [f"{c.type}={c.status}" for c in (dep.status.conditions or [])]

            return WorkloadDetail(
                ref=ref,
                replicas_desired=dep.spec.replicas or 0,
                replicas_ready=dep.status.ready_replicas or 0,
                image=container.image,
                containers=containers,
                env=env,
                resources=resources,
                conditions=conditions,
            )

        return await asyncio.to_thread(_describe)

    async def get_workload_logs(
        self, ref: WorkloadRef, *, since_seconds: int = 900, limit: int = 200
    ) -> list[LogLine]:
        def _logs():
            core, apps = self._clients()
            dep = apps.read_namespaced_deployment(ref.name, ref.namespace)
            selector = ",".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items())
            pods = core.list_namespaced_pod(ref.namespace, label_selector=selector).items

            lines: list[LogLine] = []
            for pod in pods[:3]:
                for container in pod.spec.containers:
                    for previous in (True, False):
                        # A crashlooping pod's useful output is in the PREVIOUS
                        # container, not the current one. Reading only the
                        # current container is why crashloop diagnosis usually
                        # comes back empty.
                        try:
                            raw = core.read_namespaced_pod_log(
                                name=pod.metadata.name,
                                namespace=ref.namespace,
                                container=container.name,
                                since_seconds=since_seconds,
                                tail_lines=limit,
                                previous=previous,
                                timestamps=True,
                            )
                        except Exception:
                            continue
                        for raw_line in raw.splitlines():
                            ts, _, message = raw_line.partition(" ")
                            try:
                                parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            except ValueError:
                                parsed, message = utcnow(), raw_line
                            lines.append(
                                LogLine(ts=parsed, container=container.name, message=message)
                            )
            lines.sort(key=lambda line: line.ts)
            return lines[-limit:]

        return await asyncio.to_thread(_logs)

    async def recent_deploys(self, ref: WorkloadRef, *, limit: int = 10) -> list[Deploy]:
        def _deploys():
            _, apps = self._clients()
            dep = apps.read_namespaced_deployment(ref.name, ref.namespace)
            selector = ",".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items())
            replicasets = apps.list_namespaced_replica_set(ref.namespace, label_selector=selector).items
            replicasets.sort(key=lambda r: r.metadata.creation_timestamp, reverse=True)

            out: list[Deploy] = []
            for rs in replicasets[:limit]:
                ann = rs.metadata.annotations or {}
                containers = rs.spec.template.spec.containers
                out.append(
                    Deploy(
                        ts=rs.metadata.creation_timestamp,
                        revision=ann.get("deployment.kubernetes.io/revision", rs.metadata.name),
                        image=containers[0].image if containers else "",
                        changed_by=ann.get("kubernetes.io/change-cause", "unknown"),
                        commit_sha=(rs.metadata.labels or {}).get("git-sha"),
                        summary=ann.get("kubernetes.io/change-cause", ""),
                    )
                )
            return out

        return await asyncio.to_thread(_deploys)

    async def query_metrics(
        self, ref: WorkloadRef, *, metric: str, window: str = "15m"
    ) -> MetricSeries:
        detail = await self.describe_workload(ref)
        desired = detail.replicas_desired or 1

        if metric in {"error_rate", "unavailability"}:
            value = 1.0 - (detail.replicas_ready / desired)
            unit = "ratio"
        elif metric == "restarts":
            value = float(sum(c.restart_count for c in detail.containers))
            unit = "count"
        else:
            value = float(detail.replicas_ready)
            unit = "replicas"

        now = datetime.now(UTC)
        return MetricSeries(
            metric=metric,
            unit=unit,
            points=[MetricPoint(ts=now - timedelta(minutes=m), value=value) for m in range(5, 0, -1)],
        )

    async def get_workload_status(self, ref: WorkloadRef) -> Status:
        detail = await self.describe_workload(ref)
        healthy = detail.replicas_ready == detail.replicas_desired and detail.replicas_desired > 0
        reasons = sorted({c.reason for c in detail.containers if c.reason})
        summary = f"{detail.replicas_ready}/{detail.replicas_desired} replicas ready"
        if reasons:
            summary += f" — {', '.join(reasons)}"
        return Status(ref=ref, healthy=healthy, summary=summary)
