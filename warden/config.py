"""Central configuration. Everything environment-specific lives here and nowhere else."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Verified against google-adk 2.7.1 (2026-08-17). There is NO gemini-3.5-pro:
# the Flash and Pro lines sit on different version numbers.
MODEL_FLASH = "gemini-3.5-flash"  # ADK 2.7.1's own DEFAULT_MODEL
MODEL_PRO = "gemini-3.1-pro"  # current GA Pro


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


def configure_genai_env() -> None:
    """Push Vertex routing into the environment before any google-genai import path runs.

    google-adk does not read these itself; the underlying google-genai client does.
    """
    s = settings()
    os.environ.setdefault("GOOGLE_GENAI_USE_ENTERPRISE", "1" if s.use_vertex else "0")
    if s.use_vertex:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", s.gcp_project)
        os.environ.setdefault("GOOGLE_CLOUD_LOCATION", s.gcp_location)
