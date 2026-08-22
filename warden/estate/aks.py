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
import tempfile
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

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

# Container states that mean "this pod is not going to start on its own".
# `Error` is in here deliberately: a container that exited non-zero seconds ago
# has not reached CrashLoopBackOff yet, and a demo run right after a bad deploy
# catches exactly that window.
FAILING_REASONS = frozenset(
    {
        "CrashLoopBackOff",
        "Error",
        "ImagePullBackOff",
        "ErrImagePull",
        "InvalidImageName",
        "OOMKilled",
        "CreateContainerConfigError",
        "CreateContainerError",
        "RunContainerError",
    }
)


@lru_cache
def _secret(name: str) -> str | None:
    """Resolve a cluster credential: .env, then environment, then Secret Manager.

    The .env path exists so the real cluster can be used before any GCP
    infrastructure is provisioned. Note it must be read through Settings —
    pydantic-settings loads .env into the Settings object but NOT into
    os.environ, so checking os.environ alone silently misses it.

    In production these are empty and the value comes from Secret Manager,
    where only sa-proxy holds secretAccessor — so an agent process calling this
    fails, by design.
    """
    s = settings()
    direct = {
        s.secret_aks_apiserver: s.aks_apiserver,
        s.secret_aks_token: s.aks_reader_token,
    }.get(name)
    if direct:
        return direct

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

    @staticmethod
    def _ca_cert_path() -> str | None:
        """The cluster CA, from a file path or inline PEM.

        Accepts AKS_CA_CERT_PATH (a path) or AKS_CA_CERT (which may be either a
        path, as the old code assumed, or the PEM itself — people paste the
        certificate, and a certificate is not a secret, so the failure mode of
        silently ignoring it is worse than handling it).
        """
        s = settings()
        raw = (
            s.aks_ca_cert_path
            or os.environ.get("AKS_CA_CERT_PATH")
            or os.environ.get("AKS_CA_CERT")
        )
        if not raw:
            return None
        raw = raw.strip()
        if raw.startswith("-----BEGIN"):
            path = Path(tempfile.gettempdir()) / "warden-cluster-ca.crt"
            path.write_text(raw.replace("\\n", "\n"))
            return str(path)
        expanded = Path(raw).expanduser()
        if expanded.is_file():
            return str(expanded)
        log.warning("cluster CA configured as %s but that file does not exist", expanded)
        return None

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

            # Verify the API server's certificate.
            #
            # This used to default to OFF, on the reasoning that a read-only
            # token made it an acceptable trade-off. That reasoning was wrong
            # in a way worth spelling out, because it is a tempting mistake.
            #
            # Skipping verification does not weaken *writes* — it weakens
            # knowing WHO YOU ARE TALKING TO. An unverified connection can be
            # intercepted, and then two things follow. The bearer token is
            # handed to the interceptor on every call, and it is a token that
            # can read pod logs across the namespace. And the diagnosis the
            # agent produces is only as trustworthy as the logs it read: feed
            # it forged logs and it will faithfully open a pull request fixing
            # a bug that does not exist. Every downstream guarantee in this
            # project — the evidence chain, the blast radius, the reviewer's
            # ability to trust the PR body — sits on top of this connection.
            #
            # The CA is in the same ServiceAccount token secret the read token
            # comes from (`ca.crt`), so having one and not the other is an
            # oversight rather than a constraint. See docs/SETUP.md §8.
            ca_path = self._ca_cert_path()
            if ca_path:
                cfg.verify_ssl = True
                cfg.ssl_ca_cert = ca_path
            else:
                cfg.verify_ssl = False
                log.warning(
                    "AKS_CA_CERT_PATH is not set — the API server certificate is NOT being "
                    "verified. Logs and status could be forged in transit, and a forged log "
                    "produces a confident diagnosis of a bug that does not exist. "
                    "Run estate-gitops/scripts/extract-reader-credentials.sh to fix."
                )
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
                    replicas_updated=d.status.updated_replicas or 0,
                    replicas_available=d.status.available_replicas or 0,
                    image=d.spec.template.spec.containers[0].image
                    if d.spec.template.spec.containers
                    else "",
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
                ref.namespace,
                label_selector=",".join(
                    f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items()
                ),
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

            # The reason is the point: `Progressing=False` alone does not say
            # why, and `ProgressDeadlineExceeded` is Kubernetes telling you in
            # so many words that this rollout is never finishing.
            conditions = [
                f"{c.type}={c.status}" + (f" ({c.reason})" if c.reason else "")
                for c in (dep.status.conditions or [])
            ]

            return WorkloadDetail(
                ref=ref,
                replicas_desired=dep.spec.replicas or 0,
                replicas_ready=dep.status.ready_replicas or 0,
                replicas_updated=dep.status.updated_replicas or 0,
                replicas_available=dep.status.available_replicas or 0,
                image=container.image,
                command=list(container.command or []),
                args=list(container.args or []),
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
            selector = ",".join(
                f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items()
            )
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
            selector = ",".join(
                f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items()
            )
            replicasets = apps.list_namespaced_replica_set(
                ref.namespace, label_selector=selector
            ).items
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
            points=[
                MetricPoint(ts=now - timedelta(minutes=m), value=value) for m in range(5, 0, -1)
            ],
        )

    async def get_workload_status(self, ref: WorkloadRef) -> Status:
        """Health, and specifically: is the rollout stuck?

        A live run against the real cluster reported `3/3 replicas ready —
        Error` while a deployment was badly broken, and Triage — correctly,
        given that sentence — closed the incident as a healthy workload with a
        noisy threshold. It was right about the sentence and the sentence was
        wrong.

        The old test was `ready == desired`, which is true throughout a blocked
        rollout: the PREVIOUS ReplicaSet is still serving every replica quite
        happily while the new one crashloops behind it. Nothing is unavailable,
        and nothing is fine either — the estate is pinned to an old revision,
        every future deploy is stuck behind this one, and the moment a serving
        pod restarts it comes back on the broken spec.

        That is not a subtle edge case for this project. A blocked rollout is
        the exact shape of the incident the whole demo is built on, so a status
        call that cannot see one is the single most load-bearing read in the
        system being wrong.
        """
        detail = await self.describe_workload(ref)
        desired = detail.replicas_desired
        updated = detail.replicas_updated
        ready = detail.replicas_ready

        failing = sorted({c.reason for c in detail.containers if c.reason in FAILING_REASONS})
        stalled = any("ProgressDeadlineExceeded" in c for c in detail.conditions)

        if desired == 0:
            return Status(ref=ref, healthy=False, summary="no replicas desired", rollout="none")

        if failing and (updated < desired or stalled):
            summary = (
                f"rollout BLOCKED — {updated}/{desired} pods on the new revision, "
                f"{', '.join(failing)}; the previous revision is still serving {ready}/{desired}"
            )
            if stalled:
                summary += " (progress deadline exceeded)"
            return Status(
                ref=ref, healthy=False, summary=summary, rollout="blocked", reasons=failing
            )

        if ready < desired:
            summary = f"{ready}/{desired} replicas ready"
            if failing:
                summary += f" — {', '.join(failing)}"
            rollout = "blocked" if stalled else "progressing"
            return Status(ref=ref, healthy=False, summary=summary, rollout=rollout, reasons=failing)

        if failing:
            # Serving fine, but something is restarting behind the scenes.
            return Status(
                ref=ref,
                healthy=False,
                summary=f"{ready}/{desired} replicas ready, but {', '.join(failing)}",
                rollout="progressing" if updated < desired else "complete",
                reasons=failing,
            )

        if updated < desired:
            return Status(
                ref=ref,
                healthy=False,
                summary=f"rollout in progress — {updated}/{desired} pods updated",
                rollout="progressing",
            )

        return Status(
            ref=ref, healthy=True, summary=f"{ready}/{desired} replicas ready", rollout="complete"
        )
