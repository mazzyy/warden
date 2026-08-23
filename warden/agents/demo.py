"""One incident, end to end, printed as a readable trace.

    python -m warden.agents.demo              # offline: fake estate, scripted models, dry-run PR
    python -m warden.agents.demo --live       # real Gemini against the fake estate
    ESTATE_ADAPTER=aks python -m warden.agents.demo --live   # the real thing

Offline mode needs no credentials and spends nothing, so this is the first thing
to run after cloning.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import textwrap
import warnings

from warden.agents.fixtures import scripted_models
from warden.agents.orchestrator import handle_incident, verify_merged_incident
from warden.config import (
    CredentialError,
    GitHubCredential,
    configure_genai_env,
    credential_mode,
    resolve_github_credential,
    settings,
)
from warden.control_plane.budget import estimate_usd
from warden.control_plane.jsonl_store import JsonlStore
from warden.control_plane.registry import load_all
from warden.control_plane.store import InMemoryStore
from warden.estate.base import build_adapter
from warden.estate.fake import FakeAdapter
from warden.models import Decision, IncidentStatus, Status, WorkloadRef
from warden.tools.github_client import GitHubClient
from warden.tools.toolbox import ToolBox

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
RED, GREEN, YELLOW, BLUE = "\033[31m", "\033[32m", "\033[33m", "\033[34m"


def rule(title: str = "") -> None:
    print(f"\n{DIM}{'─' * 78}{RESET}")
    if title:
        print(f"{BOLD}{title}{RESET}")


ALERT = {
    "source": "cloud-monitoring",
    "signature": "checkout-svc/CrashLoopBackOff",
    "title": "checkout-svc: 0/3 replicas ready",
    "workload": "checkout-svc",
    "namespace": "demo",
    "fired_at": "just now",
}


async def observed_alert(estate, alert: dict[str, str]) -> tuple[dict[str, str], Status | None]:
    """Replace the canned alert text with what the estate actually reports.

    The literal above describes the fake estate's bad_config mode. Against a
    real cluster it can be plainly false: a blocked rollout leaves the old
    ReplicaSet serving all three replicas while the new one crashloops, which
    is "3/3 replicas ready — CrashLoopBackOff", not "0/3".

    Handing an agent a symptom that did not happen is the same failure as an
    empty pull request claiming to fix something — it looks like the system
    worked. It also quietly grades the demo on the wrong thing: a diagnosis
    that matches a fabricated alert proves nothing about the cluster.

    Returns the alert to use and the live Status, or None when the estate is
    the fake one and the canned text is already the truth.
    """
    if isinstance(estate, FakeAdapter):
        return alert, None

    ref = WorkloadRef(name=alert["workload"], namespace=alert["namespace"])
    try:
        status = await estate.get_workload_status(ref)
    except Exception as exc:  # a demo must not die because the cluster is unreachable
        print(
            f"  {YELLOW}could not read live status ({exc}) — falling back to the canned alert{RESET}"
        )
        return alert, None

    observed = dict(alert)
    observed["title"] = f"{ref.name}: {status.summary}"

    # The signature is what Triage deduplicates on, so it has to be derived from
    # the structured fields rather than the prose, and it has to name the CAUSE
    # in preference to the symptom. Two bugs are recorded here.
    #
    # The first: splitting `summary` on an em dash produced `checkout-svc/Error`
    # for a blocked rollout — vague enough that Triage, whose only tools are the
    # alert text and past incidents, had nothing to work with and closed a real
    # incident.
    #
    # The second is the reason `reasons` is checked before `rollout`. A blocked
    # rollout is not one failure mode. OOMKilled, ImagePullBackOff and a bad
    # configuration value all stall a rollout and all need entirely different
    # evidence. Collapsing them into `checkout-svc/RolloutBlocked` means the
    # second fault the fleet ever sees matches the first one's signature, and
    # Triage — correctly, given what it was told — reads it as a duplicate of an
    # incident already awaiting a merge and stops before anyone looks at the
    # cluster. `RolloutBlocked` is therefore what we fall back to when the
    # cluster gives us no container reason at all, not what we lead with. The
    # blocked-ness is not lost: it is the first thing `status.summary` says, and
    # the summary is the title Triage reads.
    if status.reasons:
        observed["signature"] = f"{ref.name}/{status.reasons[0]}"
    elif status.rollout == "blocked":
        observed["signature"] = f"{ref.name}/RolloutBlocked"
    elif status.healthy:
        observed["signature"] = f"{ref.name}/Healthy"
    else:
        observed["signature"] = f"{ref.name}/Degraded"
    return observed, status


async def verify_only(live: bool) -> int:
    """Run the Verifier against the last incident, after you have merged.

    Separate from the main flow on purpose: the merge is a human action that
    happens after the demo process exits, so the Verifier has to be startable
    on its own. This is the command that closes the loop on screen.
    """
    warnings.filterwarnings("ignore", category=UserWarning, module="google.*")
    with contextlib.suppress(Exception):
        import urllib3

        warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    s = settings()
    rule("WARDEN — verify a merged fix")

    if live:
        try:
            configure_genai_env()
        except CredentialError as exc:
            print(f"\n{RED}{exc}{RESET}\n")
            return 2

    store = JsonlStore()
    incidents = await store.list_incidents(limit=1)
    if not incidents:
        print(f"  {YELLOW}no incident to verify — run `make demo-live` first{RESET}\n")
        return 1

    estate = (
        FakeAdapter("healthy") if s.estate_adapter == "fake" else build_adapter(s.estate_adapter)
    )
    github = GitHubClient(
        repo_full_name=s.gitops_full_name,
        base_branch=s.gitops_base_branch,
        credential=resolve_github_credential(),
    )
    toolbox = ToolBox(estate=estate, store=store, github=github, alert_context={})
    fleet = load_all(s.manifest_dir)

    print(f"  incident   {incidents[0].id}  {incidents[0].title}")
    print(f"  estate     {s.estate_adapter}")

    incident, run = await verify_merged_incident(
        fleet=fleet,
        toolbox=toolbox,
        store=store,
        models=None if live else scripted_models(),
    )
    if run is None:
        print(f"  {YELLOW}nothing to verify{RESET}\n")
        return 1

    rule(f"VERIFIER  {DIM}{run.run.model}{RESET}")
    calls = [a for a in await store.list_audit() if a.run_id == run.run.id]
    for a in calls:
        mark = f"{GREEN}✓{RESET}" if a.decision is Decision.allow else f"{RED}✗ DENIED{RESET}"
        print(f"  {mark} {a.tool}{DIM} ({a.latency_ms}ms){RESET}")
    if run.structured:
        for k, v in run.structured.items():
            print(f"  {BLUE}{k}{RESET}: {str(v)[:300]}")

    if not calls:
        # A verifier that concludes "recovered" without reading anything is
        # worth nothing, and is the exact shape of bug this project keeps
        # finding: a confident claim with no evidence under it. The verdict
        # below does not come from the model — it comes from an independent
        # status read — but the transcript above would still mislead anyone
        # skimming it, so say so.
        print(f"  {YELLOW}The Verifier answered without calling a single tool. Its text is{RESET}")
        print(f"  {YELLOW}not evidence. The verdict below is an independent status read.{RESET}")

    rule("INCIDENT")
    ok = incident.status is IncidentStatus.resolved
    colour = GREEN if ok else YELLOW
    print(f"  {colour}{incident.id}  {incident.status}{RESET}")
    if ok:
        print(f"  {DIM}closed — the loop is complete. Check the dashboard.{RESET}")
    else:
        print(f"  {DIM}still open: the workload has not recovered. Did the merge apply?{RESET}")
    print()
    return 0


async def main(
    live: bool,
    mode: str,
    dry_run: bool,
    verify: bool,
    *,
    memory_only: bool = False,
    keep_history: bool = False,
) -> int:
    # The google-genai and ADK SDKs emit UserWarnings on every run about
    # experimental features and non-text response parts. Both are cosmetic and
    # both clutter a screen recording. Suppressed here, at the entry point
    # only — library code still warns.
    warnings.filterwarnings("ignore", category=UserWarning, module="google.*")
    # urllib3 warns once per request when TLS verification is off — 25 lines in
    # a single incident, which buries everything else. Suppressed here only
    # because the adapter now emits one explicit warning of its own and the
    # header below prints the TLS state on every run, so an unverified
    # connection is stated plainly instead of drowned in repetition.
    try:
        import urllib3

        warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    s = settings()

    rule("WARDEN — one incident, end to end")
    print(f"  estate     {s.estate_adapter}")
    if s.estate_adapter != "fake":
        from warden.estate.aks import AksAdapter

        ca = AksAdapter._ca_cert_path()
        if ca:
            print(f"  tls        {GREEN}API server certificate verified{RESET}")
        else:
            print(f"  tls        {RED}NOT VERIFIED — logs could be forged in transit{RESET}")
    print(
        f"  models     {'LIVE Gemini via Vertex/AI Studio' if live else 'scripted (offline, free)'}"
    )
    print(f"  github     {s.gitops_full_name}")

    if live:
        try:
            configure_genai_env()
        except CredentialError as exc:
            print(f"\n{RED}{exc}{RESET}\n")
            return 2
        print(f"  auth       {credential_mode()}")
    else:
        print(f"\n{YELLOW}  Scripted models are a development affordance. The submitted{RESET}")
        print(
            f"{YELLOW}  demo video must run --live: the rules require unedited live execution.{RESET}"
        )

    # --- wire up ---------------------------------------------------------
    estate = FakeAdapter(mode) if s.estate_adapter == "fake" else build_adapter(s.estate_adapter)
    if isinstance(estate, FakeAdapter):
        print(f"\n{RED}  ▸ injecting failure: {mode}{RESET}")

    # The dashboard is a separate process. Writing to a shared file-backed
    # store is what lets it show this run happening rather than seed data —
    # `make dashboard` in one terminal, this in another, and the agents move
    # on screen. --memory opts out for a run that should leave no trace.
    if memory_only:
        store = InMemoryStore()
        print(f"  store      {DIM}in-memory — the dashboard will not see this run{RESET}")
    else:
        store = JsonlStore()
        if not keep_history:
            # Rehearsing three times otherwise opens the fourth take on a wall
            # of stale incidents. Clean slate is the recording default.
            store.clear()
        print(f"  store      {store.dir} {DIM}— open the dashboard to watch{RESET}")
    # A token turns propose_patch into a REAL pull request on estate-gitops.
    # Without one it stays dry-run — the loop still runs, you just get a
    # described PR instead of a real one. --dry-run forces that either way.
    credential = (
        GitHubCredential(kind="none", label="dry run — forced by --dry-run", enforced=False)
        if dry_run
        else resolve_github_credential()
    )
    github = GitHubClient(
        repo_full_name=s.gitops_full_name,
        base_branch=s.gitops_base_branch,
        credential=credential,
    )
    print(f"  pull req   {'dry run' if github.dry_run else 'LIVE — will open a real PR'}")
    print(f"  identity   {github.identity}")
    if not github.dry_run:
        if github.review_boundary_enforced:
            print(
                f"  boundary   {GREEN}enforced by GitHub — the agent cannot merge its own work{RESET}"
            )
        else:
            print(f"  boundary   {YELLOW}NOT enforced — see the warning below{RESET}")
            for line in textwrap.wrap(credential.caveat, 74):
                print(f"  {YELLOW}{line}{RESET}")
    fleet = load_all(s.manifest_dir)

    # What actually fires the alert. Against a real cluster this is read from
    # the cluster, not asserted — see observed_alert.
    alert, status = await observed_alert(estate, ALERT)
    if status is not None:
        print(f"\n  {BOLD}observed{RESET}   {alert['title']}")
        if status.rollout == "blocked":
            print(f"  {RED}rollout    BLOCKED — the old revision is masking the failure{RESET}")
        elif status.healthy:
            print(f"  {YELLOW}Nothing is wrong with this workload right now. The fleet will{RESET}")
            print(f"  {YELLOW}investigate and find no fault — that is the correct outcome,{RESET}")
            print(
                f"  {YELLOW}not a broken demo. Re-inject with estate-gitops/scripts/inject.sh.{RESET}"
            )
        elif status.rollout == "progressing":
            print(
                f"  {YELLOW}A rollout is still in progress. Give it ~30s and re-run, or the{RESET}"
            )
            print(
                f"  {YELLOW}fleet will diagnose a cluster that is mid-deploy rather than broken.{RESET}"
            )

    toolbox = ToolBox(estate=estate, store=store, github=github, alert_context=alert)

    models = None if live else scripted_models()

    # --- run -------------------------------------------------------------
    result = await handle_incident(
        alert=alert, fleet=fleet, toolbox=toolbox, store=store, models=models, verify=verify
    )

    # --- what each agent did ---------------------------------------------
    for agent_run in result.runs:
        r = agent_run.run
        rule(f"{r.agent.upper()}  {DIM}{r.model}{RESET}")
        audit = [a for a in await store.list_audit() if a.run_id == r.id]
        for a in audit:
            mark = f"{GREEN}✓{RESET}" if a.decision is Decision.allow else f"{RED}✗ DENIED{RESET}"
            print(f"  {mark} {a.tool}{DIM} ({a.latency_ms}ms){RESET}")
            if a.decision is Decision.deny:
                print(f"      {RED}{a.reason}{RESET}")
        if agent_run.structured:
            for k, v in agent_run.structured.items():
                text = str(v)
                print(f"  {BLUE}{k}{RESET}: {text[:300]}{'…' if len(text) > 300 else ''}")
        print(
            f"  {DIM}{r.total_tokens} tokens · {r.tool_calls} tool calls · "
            f"~${estimate_usd(r.model, r.prompt_tokens, r.candidates_tokens):.4f}{RESET}"
        )

    # --- the pull request -------------------------------------------------
    rule("PULL REQUEST")
    if result.incident.pr_url:
        print(f"  {GREEN}{result.incident.pr_url}{RESET}")
        print(f"  {DIM}opened on {s.gitops_full_name} — go look at it{RESET}")
    elif github.dry_run_prs:
        pr = github.dry_run_prs[0]
        print(f"  {BOLD}{pr.title}{RESET}")
        print(f"  {DIM}branch {pr.branch} · files: {', '.join(pr.changes)}{RESET}\n")
        for line in pr.body.splitlines():
            print(f"  {DIM}│{RESET} {line}")
    else:
        print(
            f"  {YELLOW}none opened — the fleet stopped at {result.stopped_at or 'remediation'}{RESET}"
        )

    # --- summary ----------------------------------------------------------
    rule("INCIDENT")
    inc = result.incident
    print(f"  {inc.id}  {inc.status}  severity={inc.severity}")
    audit = await store.list_audit()
    denials = [a for a in audit if a.decision is Decision.deny]
    print(f"  {len(audit)} audited tool calls, {len(denials)} denied")
    print(f"  {result.total_tokens} tokens across {len(result.runs)} agents")
    if not live:
        print(f"\n  {DIM}Run with --live to use real Gemini. Run `python -m warden.probe`{RESET}")
        print(f"  {DIM}to see the policy matrix — that is where the denials live.{RESET}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live", action="store_true", help="use real Gemini instead of scripted models")
    p.add_argument("--dry-run", action="store_true", help="never open a real PR, even with a token")
    p.add_argument("--verify", action="store_true", help="also run the Verifier after remediation")
    p.add_argument(
        "--verify-only",
        action="store_true",
        help="run ONLY the Verifier, against the last incident, after you have merged and applied",
    )
    p.add_argument(
        "--memory", action="store_true", help="in-memory store; the dashboard will not see this run"
    )
    p.add_argument(
        "--keep-history",
        action="store_true",
        help="append to previous runs instead of a clean slate",
    )
    p.add_argument(
        "--mode",
        default="bad_config",
        choices=["healthy", "bad_config", "oom"],
        help="which failure to inject into the fake estate",
    )
    args = p.parse_args()
    if args.verify_only:
        sys.exit(asyncio.run(verify_only(args.live)))
    sys.exit(
        asyncio.run(
            main(
                args.live,
                args.mode,
                args.dry_run,
                args.verify,
                memory_only=args.memory,
                keep_history=args.keep_history,
            )
        )
    )
