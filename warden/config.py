"""Central configuration. Everything environment-specific lives here and nowhere else."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("warden.config")

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
#   3. The Gemini API and Vertex have DIFFERENT catalogs, and Vertex
#      availability is per-region. `gemini-3.7-flash` exists on the Gemini API
#      but 404s on Vertex in europe-west3. Always check against the path you
#      will actually deploy on: `python -m warden.doctor --models`.
MODEL_FLASH = "gemini-3.5-flash"  # available on Vertex in europe-west3
MODEL_REASONING = "gemini-3.5-flash"  # same, until a newer one is confirmed there

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

    # Fine-grained PAT scoped to estate-gitops ONLY, with contents + pull
    # requests write and nothing else. Empty means dry-run.
    #
    # This comment used to claim the PAT "deliberately cannot push to main —
    # that is what makes 'no agent can merge its own PR' true in the platform
    # rather than only in the prompt." That was false, and
    # estate-gitops/scripts/verify-github-token.sh disproved it: with branch
    # protection enabled and `enforce_admins: false`, a direct commit to main
    # returned HTTP 201. A PAT inherits the repository role of the human who
    # minted it, and a repo admin bypasses protection. Narrow scopes limit
    # WHICH repositories and WHICH APIs — they do not demote the actor.
    #
    # So on the PAT path the guarantee is a property of our code, not of the
    # platform. Set GITHUB_APP_* below for the enforceable version.
    github_token: str = Field(default="", alias="GITHUB_TOKEN")

    # --- GitHub App: the agent's own identity ------------------------------
    # An App is not a member of the repository and holds no role, so branch
    # protection applies to it with no bypass to inherit. It also cannot
    # approve a pull request, which is what makes "the agent cannot merge its
    # own work" enforced rather than promised.
    #
    # The private key is a PATH, never the key itself: a PEM in .env gets
    # shoulder-surfed, pasted into chat, and committed by accident.
    # Shared secret GitHub signs webhook deliveries with. Without it the
    # /webhook/github endpoint accepts any POST from anyone who learns the URL,
    # and that endpoint starts agent runs — so an unauthenticated one is a
    # remote trigger for the fleet, not merely an untidy default.
    github_webhook_secret: str = Field(default="", alias="GITHUB_WEBHOOK_SECRET")

    github_app_id: str = Field(default="", alias="GITHUB_APP_ID")
    github_app_installation_id: str = Field(default="", alias="GITHUB_APP_INSTALLATION_ID")
    github_app_private_key_path: str = Field(default="", alias="GITHUB_APP_PRIVATE_KEY_PATH")

    # Direct cluster credentials, for local development before Secret Manager
    # exists. Deployed services leave these empty and read from Secret Manager
    # as sa-proxy. The token is the READ-ONLY ServiceAccount token from
    # estate-gitops/rbac/warden-reader.yaml — it cannot mutate anything.
    aks_apiserver: str = Field(default="", alias="AKS_APISERVER")
    aks_reader_token: str = Field(default="", alias="AKS_READER_TOKEN")
    # Path to the cluster CA (ca.crt from the same ServiceAccount token secret
    # as the reader token). Without it the API server's certificate is not
    # verified, which means forged logs are possible, which means a confident
    # diagnosis of a bug that does not exist. Not a secret — it is a public
    # certificate — but it is load-bearing.
    aks_ca_cert_path: str = Field(default="", alias="AKS_CA_CERT_PATH")

    # --- Secret Manager secret NAMES (never the values) --------------------
    # These are lookup keys like "github-token", not credentials. The naming is
    # genuinely confusable, so `_reject_secrets_in_name_fields` below catches a
    # token pasted here and says exactly what to do instead.
    secret_github_token: str = Field(default="github-token", alias="SECRET_GITHUB_TOKEN")
    secret_github_app_key: str = Field(default="github-app-key", alias="SECRET_GITHUB_APP_KEY")
    secret_aks_token: str = Field(default="aks-reader-token", alias="SECRET_AKS_TOKEN")
    secret_aks_apiserver: str = Field(default="aks-apiserver", alias="SECRET_AKS_APISERVER")

    @model_validator(mode="after")
    def _reject_secrets_in_name_fields(self) -> Settings:
        """Catch a credential pasted into a *_NAME field.

        Without this the failure is silent and baffling: the real token sits in
        .env in plain sight while the code looks up a Secret Manager secret
        named `github_pat_...`, finds nothing, and quietly runs in dry-run. You
        would spend an hour wondering why no pull request appeared.
        """
        prefixes = ("github_pat_", "ghp_", "gho_", "ghs_", "ghu_", "AIza", "AQ.")
        checks = {
            "SECRET_GITHUB_TOKEN": (self.secret_github_token, "GITHUB_TOKEN"),
            "SECRET_AKS_TOKEN": (self.secret_aks_token, "the Secret Manager secret name"),
            "SECRET_AKS_APISERVER": (self.secret_aks_apiserver, "the API server URL"),
        }
        for env_name, (value, correct) in checks.items():
            if value.startswith(prefixes) or len(value) > 80:
                raise ValueError(
                    f"\n\n  {env_name} holds what looks like a credential, but it is a "
                    f"Secret Manager secret NAME, not a value.\n\n"
                    f"  In .env, set:\n"
                    f"      {env_name}=github-token        <- the lookup key\n"
                    f"      {correct}=<your actual token>\n\n"
                    f"  Nothing is leaked — .env is gitignored. Just move the value.\n"
                )
        return self

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


def _have_adc() -> bool:
    """Whether Application Default Credentials can be resolved at all.

    Covers all three sources google-auth would use: the well-known ADC file
    from `gcloud auth application-default login`, an explicit
    GOOGLE_APPLICATION_CREDENTIALS, and the metadata server on Cloud Run.
    """
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return True
    try:
        import google.auth

        google.auth.default()
        return True
    except Exception:
        return False


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

        # Fail here, not four layers into an agent run. Without this check the
        # missing-credential error surfaces from inside ADK's node runner and
        # arrives as ~400 lines of stack trace with the one useful sentence at
        # the bottom. Preflighting costs nothing and turns it into one line.
        if not _have_adc():
            raise CredentialError(
                "No Google credentials found for Vertex.\n\n"
                "  Run this once:\n"
                "      gcloud auth application-default login\n\n"
                "  Then confirm with:  python -m warden.doctor\n\n"
                "  Or set GOOGLE_GENAI_USE_ENTERPRISE=0 and GOOGLE_API_KEY=<AI Studio key>\n"
                "  to use the Gemini API instead, or drop --live for scripted models."
            )
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


def resolve_github_token() -> str | None:
    """The PAT, from .env locally or Secret Manager when deployed.

    Returns None for dry-run rather than raising, so the whole loop still runs
    with no GitHub credential at all — you just get a described pull request
    instead of a real one.
    """
    s = settings()
    if s.github_token:
        return s.github_token
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        path = f"projects/{s.gcp_project}/secrets/{s.secret_github_token}/versions/latest"
        return client.access_secret_version(name=path).payload.data.decode()
    except Exception:
        return None


@dataclass(frozen=True)
class GitHubCredential:
    """Which identity the Remediator writes as, and what that identity implies.

    `enforced` is the field that matters. It answers one question: is "the
    agent cannot land a change on its own" a property of the platform, or only
    of our source code? A PAT minted by a repository admin bypasses branch
    protection, so on that path the answer is "only our source code" and the
    demo must say so out loud rather than claiming a guarantee it does not have.
    """

    kind: str  # "app" | "pat" | "none"
    label: str
    enforced: bool
    token: str | None = None
    app_id: str = ""
    installation_id: str = ""
    private_key: str = ""

    @property
    def caveat(self) -> str:
        if self.kind == "app":
            return ""
        if self.kind == "pat":
            return (
                "This PAT inherits your repository role. If you are an admin it can push "
                "to main and merge its own pull request; nothing but our code stops it. "
                "Set GITHUB_APP_ID / GITHUB_APP_INSTALLATION_ID / "
                "GITHUB_APP_PRIVATE_KEY_PATH to make the boundary real."
            )
        return "No GitHub credential — pull requests will be described, not opened."


def _app_private_key() -> str:
    """The App's PEM, from disk locally or Secret Manager when deployed."""
    s = settings()
    if s.github_app_private_key_path:
        path = Path(s.github_app_private_key_path).expanduser()
        if path.is_file():
            return path.read_text()
        log.warning("GITHUB_APP_PRIVATE_KEY_PATH is set but %s does not exist", path)
        return ""
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{s.gcp_project}/secrets/{s.secret_github_app_key}/versions/latest"
        return client.access_secret_version(name=name).payload.data.decode()
    except Exception:
        return ""


def resolve_github_credential() -> GitHubCredential:
    """Prefer the App. Fall back to the PAT, loudly. Then dry-run."""
    s = settings()

    if s.github_app_id and s.github_app_installation_id:
        key = _app_private_key()
        if key:
            return GitHubCredential(
                kind="app",
                label=f"GitHub App {s.github_app_id} · installation {s.github_app_installation_id}",
                enforced=True,
                app_id=s.github_app_id,
                installation_id=s.github_app_installation_id,
                private_key=key,
            )
        # Silently demoting to the PAT here would be the worst outcome: you
        # would believe you were running the enforced path while running the
        # bypassable one.
        log.warning(
            "GITHUB_APP_ID is set but no private key was found — falling back to the PAT, "
            "which does NOT enforce the review boundary."
        )

    token = resolve_github_token()
    if token:
        return GitHubCredential(
            kind="pat", label="personal access token", enforced=False, token=token
        )
    return GitHubCredential(kind="none", label="dry run — no GitHub credential", enforced=False)


def credential_mode() -> str:
    """One line describing where model calls will authenticate from."""
    s = settings()
    if s.use_vertex:
        return f"Vertex AI · project {s.gcp_project} · {s.gcp_location}"
    if s.google_api_key:
        return f"Gemini API · key {s.google_api_key[:6]}…{s.google_api_key[-4:]}"
    return "Gemini API · NO KEY SET"
