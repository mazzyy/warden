"""The agent registry: YAML manifests in git, loaded and validated into the fleet.

Validation is deliberately strict and deliberately loud. A manifest that grants a
tool the catalog has never heard of is a typo that would otherwise surface as a
mysterious runtime denial three days later, so it fails at load with the offending
name in the message.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from warden.models import AgentManifest
from warden.tools import catalog

log = logging.getLogger("warden.registry")


class ManifestError(ValueError):
    pass


def load_manifest(path: str | Path) -> AgentManifest:
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: expected a YAML mapping")

    try:
        manifest = AgentManifest.model_validate(raw)
    except Exception as exc:  # pragma: no cover - pydantic message is the useful part
        raise ManifestError(f"{path}: {exc}") from exc

    unknown = catalog.unknown_tools(manifest.spec.tools)
    if unknown:
        raise ManifestError(
            f"{path}: agent {manifest.name!r} grants unknown tool(s): "
            f"{', '.join(unknown)}. Add them to warden/tools/catalog.py or fix the typo."
        )

    # A manifest that grants a tool but withholds the scope that tool needs is
    # not an error — it is a policy choice — but it is almost always a mistake,
    # so it warns.
    granted = set(manifest.spec.scopes)
    for tool_name in manifest.spec.tools:
        spec = catalog.get(tool_name)
        assert spec is not None  # guaranteed by the unknown check above
        missing = spec.scopes - granted
        if missing:
            log.warning(
                "manifest %s grants tool %r but not scope(s) %s — every call will be denied",
                manifest.name,
                tool_name,
                ", ".join(sorted(missing)),
            )

    return manifest


def load_all(manifest_dir: str | Path) -> dict[str, AgentManifest]:
    directory = Path(manifest_dir)
    if not directory.is_dir():
        raise ManifestError(f"{directory} is not a directory")

    manifests: dict[str, AgentManifest] = {}
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        manifest = load_manifest(path)
        if manifest.name in manifests:
            raise ManifestError(f"duplicate agent name {manifest.name!r} at {path}")
        manifests[manifest.name] = manifest

    if not manifests:
        raise ManifestError(f"no manifests found in {directory}")
    return manifests


async def sync_to_store(manifests: dict[str, AgentManifest], project: str) -> None:
    """Push the loaded manifests into Firestore so the dashboard can read them.

    Git remains the source of truth; Firestore is a projection of it.
    """
    from google.cloud import firestore

    db = firestore.AsyncClient(project=project)
    for name, manifest in manifests.items():
        await db.collection("agents").document(name).set(manifest.model_dump(mode="json", by_alias=True))
        log.info("registry: synced agent %s", name)
