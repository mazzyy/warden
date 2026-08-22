"""A file-backed store, so two processes can watch the same incident.

WHY THIS EXISTS

`make demo-live` and `make dashboard` are separate processes. Both were using
InMemoryStore, which meant the dashboard could never show a real run — it showed
`seed.py`, three invented incidents including one linking to a pull request that
does not exist. Everything else in this project is real; the screen was not.

Firestore fixes it properly and is where deployed runs go. But it needs a cloud
project, credentials and a Terraform apply, and none of that should stand
between someone cloning the repo and watching agents work. So: append-only JSONL
on disk, no cloud, no daemon, both processes pointed at the same directory.

DESIGN NOTES

Append-only, one file per record type. A run is written many times as it
progresses, so reads collapse by id keeping the last write — the file is a log,
the store is the fold over it. That makes writes atomic-ish for free (a single
short `write` on a line-buffered append handle) and makes the whole history
recoverable, which matters when you are debugging what an agent did.

The dashboard polls, so it re-reads on every request. At demo scale — hundreds
of records — that is microseconds and not worth caching. It caches on mtime
anyway, because a poll every second for four minutes of recording is not
nothing, and the check is three lines.

Not durable against concurrent writers on a network filesystem. It is a local
development and demo store, and says so.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from warden.models import AuditRecord, FleetState, Incident, Run

log = logging.getLogger("warden.store.jsonl")

DEFAULT_DIR = Path(os.environ.get("WARDEN_STORE_DIR", ".warden-state"))


class JsonlStore:
    """Append-only JSONL on local disk. Shared by the demo and the dashboard."""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.dir = Path(directory or DEFAULT_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    # -- files -------------------------------------------------------------

    def _path(self, kind: str) -> Path:
        return self.dir / f"{kind}.jsonl"

    async def _append(self, kind: str, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, default=str, separators=(",", ":")) + "\n"

        def _write() -> None:
            with open(self._path(kind), "a", encoding="utf-8") as fh:
                fh.write(line)

        async with self._lock:
            await asyncio.to_thread(_write)
        self._cache.pop(kind, None)

    def _read(self, kind: str) -> list[dict[str, Any]]:
        path = self._path(kind)
        if not path.exists():
            return []
        mtime = path.stat().st_mtime
        cached = self._cache.get(kind)
        if cached and cached[0] == mtime:
            return cached[1]

        rows: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    # A half-written final line means the writer is mid-append.
                    # Skipping it is right: it will be complete on the next poll,
                    # and raising here would take down the dashboard mid-demo.
                    log.debug("skipping partial line in %s", path.name)
        self._cache[kind] = (mtime, rows)
        return rows

    @staticmethod
    def _collapse(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Last write wins, insertion order preserved."""
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            out[row.get("id", "")] = row
        return list(out.values())

    # -- incidents ---------------------------------------------------------

    async def put_incident(self, incident: Incident) -> None:
        await self._append("incidents", incident.model_dump(mode="json"))

    async def get_incident(self, incident_id: str) -> Incident | None:
        for row in reversed(self._read("incidents")):
            if row.get("id") == incident_id:
                return Incident.model_validate(row)
        return None

    async def list_incidents(self, limit: int = 50) -> list[Incident]:
        items = [Incident.model_validate(r) for r in self._collapse(self._read("incidents"))]
        items.sort(key=lambda i: i.opened_at, reverse=True)
        return items[:limit]

    # -- runs --------------------------------------------------------------

    async def put_run(self, run: Run) -> None:
        await self._append("runs", run.model_dump(mode="json"))

    async def get_run(self, run_id: str) -> Run | None:
        for row in reversed(self._read("runs")):
            if row.get("id") == run_id:
                return Run.model_validate(row)
        return None

    async def list_runs(self, incident_id: str | None = None, limit: int = 100) -> list[Run]:
        items = [Run.model_validate(r) for r in self._collapse(self._read("runs"))]
        if incident_id:
            items = [r for r in items if r.incident_id == incident_id]
        items.sort(key=lambda r: r.started_at)
        return items[-limit:]

    # -- audit -------------------------------------------------------------

    async def append_audit(self, record: AuditRecord) -> None:
        await self._append("audit", record.model_dump(mode="json"))

    async def list_audit(self, run_id: str | None = None, limit: int = 200) -> list[AuditRecord]:
        # Audit is genuinely append-only — never collapsed. Two identical calls
        # are two events, and losing one would understate what an agent did.
        items = [AuditRecord.model_validate(r) for r in self._read("audit")]
        if run_id:
            items = [a for a in items if a.run_id == run_id]
        items.sort(key=lambda a: a.ts)
        return items[-limit:]

    # -- fleet state -------------------------------------------------------

    async def get_fleet_state(self) -> FleetState:
        rows = self._read("fleet")
        return FleetState.model_validate(rows[-1]) if rows else FleetState()

    async def set_fleet_state(self, state: FleetState) -> None:
        await self._append("fleet", state.model_dump(mode="json"))

    # -- housekeeping ------------------------------------------------------

    def clear(self) -> None:
        """Start a clean take. Called by `make demo-live` unless told otherwise.

        Rehearsing three times leaves three incidents on screen, and the fourth
        take then opens with a wall of stale runs. Recording is the use case;
        make the clean slate the default and let --keep-history opt out.
        """
        for kind in ("incidents", "runs", "audit", "fleet"):
            self._path(kind).unlink(missing_ok=True)
        self._cache.clear()
