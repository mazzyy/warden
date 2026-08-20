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
import logging
import sys

from warden.agents.fixtures import scripted_models
from warden.agents.orchestrator import handle_incident
from warden.config import CredentialError, configure_genai_env, credential_mode, settings
from warden.control_plane.budget import estimate_usd
from warden.control_plane.registry import load_all
from warden.control_plane.store import InMemoryStore
from warden.estate.base import build_adapter
from warden.estate.fake import FakeAdapter
from warden.models import Decision
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


async def main(live: bool, mode: str) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    s = settings()

    rule("WARDEN — one incident, end to end")
    print(f"  estate     {s.estate_adapter}")
    print(f"  models     {'LIVE Gemini via Vertex/AI Studio' if live else 'scripted (offline, free)'}")
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
        print(f"{YELLOW}  demo video must run --live: the rules require unedited live execution.{RESET}")

    # --- wire up ---------------------------------------------------------
    estate = FakeAdapter(mode) if s.estate_adapter == "fake" else build_adapter(s.estate_adapter)
    if isinstance(estate, FakeAdapter):
        print(f"\n{RED}  ▸ injecting failure: {mode}{RESET}")

    store = InMemoryStore()
    github = GitHubClient(repo_full_name=s.gitops_full_name, token=None)  # dry-run
    fleet = load_all(s.manifest_dir)
    toolbox = ToolBox(estate=estate, store=store, github=github, alert_context=ALERT)

    models = None if live else scripted_models()

    # --- run -------------------------------------------------------------
    result = await handle_incident(
        alert=ALERT, fleet=fleet, toolbox=toolbox, store=store, models=models
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
    if github.dry_run_prs:
        pr = github.dry_run_prs[0]
        print(f"  {BOLD}{pr.title}{RESET}")
        print(f"  {DIM}branch {pr.branch} · files: {', '.join(pr.changes)}{RESET}\n")
        for line in pr.body.splitlines():
            print(f"  {DIM}│{RESET} {line}")
    else:
        print(f"  {YELLOW}none opened — the fleet stopped at {result.stopped_at or 'remediation'}{RESET}")

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
    p.add_argument(
        "--mode",
        default="bad_config",
        choices=["healthy", "bad_config", "oom"],
        help="which failure to inject into the fake estate",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.live, args.mode)))
