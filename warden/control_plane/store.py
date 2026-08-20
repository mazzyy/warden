"""Persistence. One interface, a Firestore implementation and an in-memory one.

Nothing outside this module talks to Firestore directly — that rule is what lets
the entire control plane be tested without a cloud project, and what makes the
fixture-driven development in ADR-006 possible.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

from warden.models import AuditRecord, FleetState, Incident, Run


@runtime_checkable
class Store(Protocol):
    async def put_incident(self, incident: Incident) -> None: ...
    async def get_incident(self, incident_id: str) -> Incident | None: ...
    async def list_incidents(self, limit: int = 50) -> list[Incident]: ...
    async def put_run(self, run: Run) -> None: ...
    async def get_run(self, run_id: str) -> Run | None: ...
    async def list_runs(self, incident_id: str | None = None, limit: int = 100) -> list[Run]: ...
    async def append_audit(self, record: AuditRecord) -> None: ...
    async def list_audit(self, run_id: str | None = None, limit: int = 200) -> list[AuditRecord]: ...
    async def get_fleet_state(self) -> FleetState: ...
    async def set_fleet_state(self, state: FleetState) -> None: ...


class InMemoryStore:
    """Used by tests and by `ESTATE_ADAPTER=fake` local runs."""

    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}
        self._runs: dict[str, Run] = {}
        self._audit: list[AuditRecord] = []
        self._fleet = FleetState()
        self._lock = asyncio.Lock()

    async def put_incident(self, incident: Incident) -> None:
        self._incidents[incident.id] = incident

    async def get_incident(self, incident_id: str) -> Incident | None:
        return self._incidents.get(incident_id)

    async def list_incidents(self, limit: int = 50) -> list[Incident]:
        items = sorted(self._incidents.values(), key=lambda i: i.opened_at, reverse=True)
        return items[:limit]

    async def put_run(self, run: Run) -> None:
        self._runs[run.id] = run

    async def get_run(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    async def list_runs(self, incident_id: str | None = None, limit: int = 100) -> list[Run]:
        items = [r for r in self._runs.values() if incident_id is None or r.incident_id == incident_id]
        items.sort(key=lambda r: r.started_at, reverse=True)
        return items[:limit]

    async def append_audit(self, record: AuditRecord) -> None:
        async with self._lock:
            self._audit.append(record)

    async def list_audit(self, run_id: str | None = None, limit: int = 200) -> list[AuditRecord]:
        items = [a for a in self._audit if run_id is None or a.run_id == run_id]
        items.sort(key=lambda a: a.ts)
        return items[-limit:]

    async def get_fleet_state(self) -> FleetState:
        return self._fleet

    async def set_fleet_state(self, state: FleetState) -> None:
        self._fleet = state


class FirestoreStore:
    """Collections per Appendix B of the delivery plan.

    The audit collection is append-only by convention here and by IAM in
    production: only sa-proxy holds roles/datastore.user, so an agent cannot
    erase its own trail even if it wanted to.
    """

    def __init__(self, project: str, database: str = "(default)") -> None:
        from google.cloud import firestore  # imported lazily so tests need no GCP

        self._db = firestore.AsyncClient(project=project, database=database)

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _dump(model: Any) -> dict[str, Any]:
        return model.model_dump(mode="json")

    async def put_incident(self, incident: Incident) -> None:
        await self._db.collection("incidents").document(incident.id).set(self._dump(incident))

    async def get_incident(self, incident_id: str) -> Incident | None:
        snap = await self._db.collection("incidents").document(incident_id).get()
        return Incident.model_validate(snap.to_dict()) if snap.exists else None

    async def list_incidents(self, limit: int = 50) -> list[Incident]:
        from google.cloud.firestore import Query

        q = self._db.collection("incidents").order_by("opened_at", direction=Query.DESCENDING).limit(limit)
        return [Incident.model_validate(d.to_dict()) async for d in q.stream()]

    async def put_run(self, run: Run) -> None:
        await self._db.collection("runs").document(run.id).set(self._dump(run))

    async def get_run(self, run_id: str) -> Run | None:
        snap = await self._db.collection("runs").document(run_id).get()
        return Run.model_validate(snap.to_dict()) if snap.exists else None

    async def list_runs(self, incident_id: str | None = None, limit: int = 100) -> list[Run]:
        from google.cloud.firestore import Query

        q = self._db.collection("runs")
        if incident_id:
            q = q.where("incident_id", "==", incident_id)
        q = q.order_by("started_at", direction=Query.DESCENDING).limit(limit)
        return [Run.model_validate(d.to_dict()) async for d in q.stream()]

    async def append_audit(self, record: AuditRecord) -> None:
        await self._db.collection("audit").document(record.id).set(self._dump(record))

    async def list_audit(self, run_id: str | None = None, limit: int = 200) -> list[AuditRecord]:
        q = self._db.collection("audit")
        if run_id:
            q = q.where("run_id", "==", run_id)
        q = q.order_by("ts").limit(limit)
        return [AuditRecord.model_validate(d.to_dict()) async for d in q.stream()]

    async def get_fleet_state(self) -> FleetState:
        snap = await self._db.collection("fleet").document("state").get()
        return FleetState.model_validate(snap.to_dict()) if snap.exists else FleetState()

    async def set_fleet_state(self, state: FleetState) -> None:
        await self._db.collection("fleet").document("state").set(self._dump(state))
