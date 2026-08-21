"""Central configuration. Everything environment-specific lives here and nowhere else."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Verified against a live `models.list` on 2026-08-21.
#
# Two traps here, both confirmed the hard way:
#
#   1. There is no `gemini-3.5-pro` and no GA `gemini-3.1-pro` — the Pro line
#      is preview-only (`gemini-3.1-pro-preview`).
#   2. More importantly, the hackathon requires "Gemini 3.5 or NEWER". A judge
#      reading `3.1` at Stage One — which is pass/fail on requirement
#      compliance — has a literal reason to fail the submission. Not a fight
#      worth having for a model we do not need.
#
# So every model here is >= 3.5 by version number, with no preview suffix.
MODEL_FLASH = "gemini-3.5-flash"    # ADK 2.7.1's own DEFAULT_MODEL
MODEL_REASONING = "gemini-3.7-flash"  # newest GA Flash; the heavy-reasoning tier

# Back-compat alias; prefer MODEL_REASONING.
MODEL_PRO = MODEL_REASONING


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Google Cloud -----------------------------------------------------
    gcp_project: str = Field(default="gen-lang-client-0473437618", alias="GOOGLE_CLOUD_PROJECT")
    gcp_project_number: str = Field(default="184359031908", alias="GOOGLE_CLOUD_PROJECT_NUMBER")
    gcp_location: str = Field(default="europe-west3", alias="GOOGLE_CLOUD_LOCATION")

    # google-genai reads GOOGLE_GENAI_USE_ENTERPRISE (newer name, takes precedence
    # over GOOGLE_GENAI_USE_VERTEXAI when both are set). ADK 2.7.1's own scaffold
    # writes ENTERPRISE=1, so we match it.
    use_vertex: bool = Field(default=True, alias="GOOGLE_GENAI_USE_ENTERPRISE")

    # --- Azure estate -----------------------------------------------------
    azure_subscription_id: str = Field(
        default="77f69e31-9603-4766-8e47-93a380c2cfd1", alias="AZURE_SUBSCRIPTION_ID"
    )
    azure_region: str = Field(default="germanywestcentral", alias="AZURE_REGION")
    # Defaults to `fake` so a fresh clone runs offline with no cloud, no key and
    # no spend. Set ESTATE_ADAPTER=aks in .env once the cluster exists.
    estate_adapter: str = Field(default="fake", alias="ESTATE_ADAPTER")  # aks | gke | fake
    estate_namespace: str = Field(default="demo", alias="ESTATE_NAMESPACE")

    # --- GitOps -----------------------------------------------------------
    github_owner: str = Field(default="mazzyy", alias="GITHUB_OWNER")
    gitops_repo: str = Field(default="estate-gitops", alias="GITOPS_REPO")
    gitops_base_branch: str = Field(default="main", alias="GITOPS_BASE_BRANCH")

    # --- Model credentials -------------------------------------------------
    # The ONLY secret value read from .env rather than Secret Manager, because
    # the development path (AI Studio free tier) has nowhere else to put it.
    # .env is gitignored. Deployed services use Vertex + the runtime service
    # account and set no key at all.
    google_api_key: str = Field(default="", alias="GOOGLE_API_KEY")

    # --- Secret Manager secret names (never the values) --------------------
    secret_github_token: str = Field(default="github-token", alias="SECRET_GITHUB_TOKEN")
    secret_aks_token: str = Field(default="aks-reader-token", alias="SECRET_AKS_TOKEN")
    secret_aks_apiserver: str = Field(default="aks-apiserver", alias="SECRET_AKS_APISERVER")

    # --- Budget guards ----------------------------------------------------
    # Enforced by the proxy. These are the ones that keep a runaway loop from
    # eating the whole credit balance.
    budget_usd_cap: float = Field(default=5.0, alias="BUDGET_USD_CAP")
    budget_warn_ratio: float = Field(default=0.8, alias="BUDGET_WARN_RATIO")

    # --- Runtime ----------------------------------------------------------
    pubsub_topic: str = Field(default="warden-events", alias="PUBSUB_TOPIC")
    manifest_dir: str = Field(default="manifests/agents", alias="MANIFEST_DIR")
    use_fixtures: bool = Field(default=False, alias="WARDEN_USE_FIXTURES")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def gitops_full_name(self) -> str:
        return f"{self.github_owner}/{self.gitops_repo}"


@lru_cache
def settings() -> Settings:
    return Settings()


class CredentialError(RuntimeError):
    pass


def configure_genai_env() -> None:
    """Put model routing into os.environ before any google-genai client is built.

    This has to exist because of a trap that silently produces auth errors:
    pydantic-settings reads `.env` into *this Settings object*, but never into
    `os.environ`. The google-genai SDK only reads `os.environ`. So a
    GOOGLE_API_KEY sitting in .env reaches Settings and nothing else — the SDK
    never sees it, and you get an unauthenticated error while staring at a file
    that plainly contains the key.

    Two supported paths:

      Vertex (deployed)  GOOGLE_GENAI_USE_ENTERPRISE=1 + application-default
                         credentials or the Cloud Run service account. No key.
      Gemini API (dev)   GOOGLE_GENAI_USE_ENTERPRISE=0 + GOOGLE_API_KEY.
                         Use the AI Studio free tier here so iteration costs
                         nothing and the $10 of credit survives for the demo.
    """
    s = settings()
    os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "1" if s.use_vertex else "0"

    if s.use_vertex:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", s.gcp_project)
        os.environ.setdefault("GOOGLE_CLOUD_LOCATION", s.gcp_location)
        return

    key = s.google_api_key or os.environ.get("GOOGLE_API_KEY", "")
    if not key:
        raise CredentialError(
            "No model credential found.\n\n"
            "  For the free development path, put your AI Studio key in .env:\n"
            "      GOOGLE_GENAI_USE_ENTERPRISE=0\n"
            "      GOOGLE_API_KEY=AIza...\n"
            "    Get one at https://aistudio.google.com/apikey\n\n"
            "  For Vertex, set GOOGLE_GENAI_USE_ENTERPRISE=1 and run\n"
            "      gcloud auth application-default login\n\n"
            "  Or drop --live to run against scripted models, which need neither."
        )
    os.environ["GOOGLE_API_KEY"] = key
    # Setting project/location while the Vertex flag is false makes the SDK
    # raise, so they are deliberately not exported on this path.
    for stale in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        os.environ.pop(stale, None)


def credential_mode() -> str:
    """One line describing where model calls will authenticate from."""
    s = settings()
    if s.use_vertex:
        return f"Vertex AI · project {s.gcp_project} · {s.gcp_location}"
    if s.google_api_key:
        return f"Gemini API · key {s.google_api_key[:6]}…{s.google_api_key[-4:]}"
    return "Gemini API · NO KEY SET"
