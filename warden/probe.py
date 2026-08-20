"""The policy matrix: every agent × every tool, allowed or denied.

    python -m warden.probe
    python -m warden.probe --explain          # show the reason for each denial
    python -m warden.probe --assert-no-cluster-writes    # exit 1 if anything can write

This is the honest way to demonstrate the denial. You cannot reliably get a
well-prompted model to attempt a tool it has been told it does not have, so
trying to stage that on camera is a losing game. Instead, ask the policy engine
directly and show the whole matrix at once — which is both more convincing and
what an operator would actually want after editing a manifest.

The `--assert-no-cluster-writes` mode belongs in CI. It turns "no agent can write
to the cluster" from a claim in the README into a build failure if it ever stops
being true.
"""

from __future__ import annotations

import argparse
import sys

from warden.control_plane import policy
from warden.control_plane.registry import load_all
from warden.models import Decision
from warden.tools import catalog

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

# Realistic arguments per tool. These must match what the tool actually takes:
# passing a `files` list to a read-only tool trips the blast-radius check on any
# agent whose maxFilesPerPatch is 0, and every cell in the matrix goes red for
# the wrong reason. The probe is only useful if it probes honestly.
_WORKLOAD = {"namespace": "demo", "name": "checkout-svc"}
_PATCH = {
    "namespace": "demo",
    "title": "probe",
    "rationale": "probe",
    "files": ["apps/checkout-svc/deployment.yaml"],
    "contents": ["..."],
}

TOOL_PROBE_ARGS: dict[str, dict] = {
    "get_alert_context": {},
    "recall_similar_incidents": {"signature": "checkout-svc/CrashLoopBackOff"},
    "describe_workload": _WORKLOAD,
    "get_workload_logs": _WORKLOAD,
    "recent_deploys": _WORKLOAD,
    "query_metrics": {**_WORKLOAD, "metric": "error_rate"},
    "get_workload_status": _WORKLOAD,
    "read_repo_file": {"path": "apps/checkout-svc/deployment.yaml"},
    "propose_patch": _PATCH,
    "request_revert": {"namespace": "demo", "pr_number": 1, "reason": "probe"},
    "delete_workload": _WORKLOAD,
    "scale_workload": {**_WORKLOAD, "replicas": 0},
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest-dir", default="manifests/agents")
    p.add_argument("--explain", action="store_true", help="print the reason for each denial")
    p.add_argument(
        "--assert-no-cluster-writes",
        action="store_true",
        help="exit non-zero if any agent can call a cluster-mutating tool",
    )
    args = p.parse_args()

    fleet = load_all(args.manifest_dir)
    tools = sorted(catalog.CATALOG)
    agents = sorted(fleet)

    width = max(len(t) for t in tools) + 2
    print(f"\n{BOLD}Policy matrix{RESET} {DIM}— {len(agents)} agents × {len(tools)} tools{RESET}\n")
    header = " " * width + "".join(a[:6].center(9) for a in agents)
    print(f"{DIM}{header}{RESET}")

    violations: list[str] = []
    explanations: list[str] = []

    for tool in tools:
        spec = catalog.get(tool)
        mutating = spec.mutating if spec else False
        label = f"{tool}{' ⚠' if mutating else '  '}".ljust(width)
        row = label
        for agent in agents:
            probe_args = TOOL_PROBE_ARGS.get(tool, {})
            result = policy.evaluate(fleet[agent], tool, probe_args)
            if result.decision is Decision.allow:
                row += f"{GREEN}  allow  {RESET} "
                if spec and "cluster:demo:write" in spec.scopes:
                    violations.append(f"{agent} may call {tool}")
            else:
                row += f"{RED}  deny   {RESET} "
                explanations.append(f"{agent} · {tool}\n    {result.reason}")
        print(row)

    print(f"\n{DIM}⚠ = mutating tool{RESET}")

    if args.explain:
        print(f"\n{BOLD}Denials{RESET}\n")
        for line in explanations:
            print(f"  {DIM}{line}{RESET}\n")

    if violations:
        print(f"\n{RED}{BOLD}POLICY VIOLATION{RESET}")
        for v in violations:
            print(f"  {RED}✗ {v}{RESET}")
        if args.assert_no_cluster_writes:
            return 1
    else:
        print(f"\n{GREEN}✓ No agent in the fleet can write to the cluster.{RESET}")
        print(f"{DIM}  The only write primitive anywhere in this system is opening a pull request.{RESET}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
