"""A scripted stand-in for Gemini, for development and tests (ADR-006).

Why this exists: with a $10 credit balance you cannot afford to call a real model
on every code change. Recording or scripting a run once and replaying it makes
iteration free and tests deterministic.

**This must never appear in the submitted demo.** The hackathon requires unedited
live execution, so the video runs against real Gemini. `WARDEN_USE_FIXTURES`
defaults to false and every run announces which mode it is in, so it is hard to
show a scripted run on camera by accident.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field


class ScriptedModel(BaseLlm):
    """Replays a fixed list of turns.

    Each turn is either `{"tool": name, "args": {...}}` to emit a function call,
    or `{"text": "..."}` / `{"json": {...}}` to emit a final answer.
    """

    script: list = Field(default_factory=list)
    # Mutable cursor rather than a private attr — pydantic models will not take
    # arbitrary instance attributes, and a dict field is the least surprising
    # way to carry per-run state.
    cursor: dict = Field(default_factory=lambda: {"i": 0})
    tokens_per_turn: int = 160

    def reset(self) -> None:
        self.cursor["i"] = 0

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        i = self.cursor["i"]
        self.cursor["i"] = i + 1
        turn = self.script[i] if i < len(self.script) else {"text": "done"}

        if "tool" in turn:
            part = types.Part(
                function_call=types.FunctionCall(name=turn["tool"], args=turn.get("args", {}))
            )
        elif "json" in turn:
            part = types.Part(text=json.dumps(turn["json"]))
        else:
            part = types.Part(text=turn.get("text", ""))

        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=int(self.tokens_per_turn * 0.75),
                candidates_token_count=int(self.tokens_per_turn * 0.25),
                total_token_count=self.tokens_per_turn,
            ),
        )


# --------------------------------------------------------------------------
# A full bad-config incident, scripted. Mirrors what the real models do against
# FakeAdapter(mode="bad_config") — same tools, same order, same conclusion.
# --------------------------------------------------------------------------

TRIAGE_SCRIPT = [
    {"tool": "get_alert_context", "args": {}},
    {"tool": "recall_similar_incidents", "args": {"signature": "checkout-svc/CrashLoopBackOff"}},
    {
        "json": {
            "severity": "critical",
            "escalate": True,
            "duplicate_of": None,
            "reasoning": "checkout-svc has 0/3 replicas ready and no matching open incident.",
        }
    },
]

DIAGNOSTICIAN_SCRIPT = [
    {"tool": "describe_workload", "args": {"name": "checkout-svc", "namespace": "demo"}},
    {"tool": "get_workload_logs", "args": {"name": "checkout-svc", "namespace": "demo"}},
    {"tool": "recent_deploys", "args": {"name": "checkout-svc", "namespace": "demo"}},
    {
        "json": {
            "hypothesis": "checkout-svc crashloops on startup because PAYMENT_ENDPOINT has a malformed URL scheme.",
            "root_cause": "Revision r42 (commit 9f2c1ab, 4 minutes ago) set PAYMENT_ENDPOINT to 'htps://payments.internal/v2' — 'htps' instead of 'https'.",
            "evidence": [
                {
                    "source": "get_workload_logs",
                    "detail": "FATAL: unsupported URL scheme \"htps\" in PAYMENT_ENDPOINT",
                },
                {
                    "source": "describe_workload",
                    "detail": "container checkout: CrashLoopBackOff, 7 restarts, exit code 1",
                },
                {
                    "source": "recent_deploys",
                    "detail": "r42 'chore: tune payment endpoint and timeouts' deployed 4 minutes before first failure",
                },
            ],
            "suggested_fix": "Restore PAYMENT_ENDPOINT to 'https://payments.internal/v2'.",
            "confidence": 0.93,
        }
    },
]

REMEDIATOR_SCRIPT = [
    {"tool": "read_repo_file", "args": {"path": "apps/checkout-svc/deployment.yaml"}},
    {
        "tool": "propose_patch",
        "args": {
            "title": "fix(checkout-svc): restore https scheme on PAYMENT_ENDPOINT",
            "rationale": (
                "checkout-svc has been crashlooping since r42 (commit 9f2c1ab). That revision "
                "set PAYMENT_ENDPOINT to 'htps://payments.internal/v2' — a typo in the URL "
                "scheme — and the service exits 1 on startup with "
                "'unsupported URL scheme \"htps\"'. This restores the scheme to https and "
                "changes nothing else.\n\n"
                "After merge, watch replicas_ready return to 3/3 and error_rate fall below 0.05."
            ),
            "files": ["apps/checkout-svc/deployment.yaml"],
            "contents": ["# patched by warden\n"],
        },
    },
    {
        "json": {
            "pr_url": None,
            "files_changed": ["apps/checkout-svc/deployment.yaml"],
            "rationale": "Reverted the malformed PAYMENT_ENDPOINT scheme introduced in r42.",
        }
    },
]


def scripted_models() -> dict[str, ScriptedModel]:
    return {
        "triage": ScriptedModel(model="scripted/triage", script=TRIAGE_SCRIPT),
        "diagnostician": ScriptedModel(model="scripted/diagnostician", script=DIAGNOSTICIAN_SCRIPT),
        "remediator": ScriptedModel(model="scripted/remediator", script=REMEDIATOR_SCRIPT),
        "verifier": ScriptedModel(model="scripted/verifier", script=[{"text": "recovered"}]),
    }
