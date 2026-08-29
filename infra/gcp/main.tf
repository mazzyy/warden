# The fleet: agents, control plane, and everything that satisfies the hackathon's
# required stack (Gemini 3.5 via Vertex, ADK, and — several times over — Google
# Cloud infrastructure).
#
# Ordering matters here. The budget guard is created before anything that can
# spend money, because with a $10 balance an unnoticed burn is a project-ending
# event, not an inconvenience.
#
#   terraform init
#   terraform apply -var billing_account=XXXXXX-XXXXXX-XXXXXX -var alert_email=you@example.com

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# --------------------------------------------------------------------------
# Variables
# --------------------------------------------------------------------------

variable "project_id" {
  type    = string
  default = "gen-lang-client-0473437618"
}

variable "region" {
  type    = string
  default = "europe-west3" # Frankfurt
}

variable "alert_email" {
  type        = string
  description = "Where budget and health alerts land."
}

variable "billing_account" {
  type        = string
  default     = ""
  description = <<-EOT
    Billing account ID (XXXXXX-XXXXXX-XXXXXX), from:
      gcloud billing accounts list --filter=open=true
    Leave empty to skip the budget guard — but do not leave it empty for long.
    A gen-lang-client-* project comes from AI Studio and often has no billing
    linked at all, in which case Vertex, Cloud Run and Firestore all fail with
    errors that do not mention billing.
  EOT
}

variable "budget_usd" {
  type    = number
  default = 5
}

locals {
  agents = ["triage", "diagnostician", "remediator", "verifier"]
}

# --------------------------------------------------------------------------
# APIs
# --------------------------------------------------------------------------

resource "google_project_service" "apis" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "firestore.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudtrace.googleapis.com",
    "cloudscheduler.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "billingbudgets.googleapis.com",
  ])
  service = each.value

  # Leave them on. Disabling on destroy breaks anything else in the project
  # that happens to share an API.
  disable_on_destroy = false
}

# --------------------------------------------------------------------------
# Spend guards — FIRST
# --------------------------------------------------------------------------

resource "google_monitoring_notification_channel" "email" {
  display_name = "Warden alerts"
  type         = "email"
  labels       = { email_address = var.alert_email }
  depends_on   = [google_project_service.apis]
}

resource "google_billing_budget" "cap" {
  count = var.billing_account == "" ? 0 : 1

  # The bare ID. The provider prepends "billingAccounts/" itself, and doing it
  # here too produces /v1/billingAccounts/billingAccounts/<id>/budgets and a 404
  # whose body is a Google error page rather than anything about billing.
  billing_account = var.billing_account
  display_name    = "Warden ${var.budget_usd} USD cap"

  budget_filter {
    projects = ["projects/${var.project_id}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_usd)
    }
  }

  # Fractions, not percentages. `50` here would mean 5000% and would never
  # fire — which you would discover only once the credit was gone.
  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
    }
  }

  all_updates_rule {
    monitoring_notification_channels = [google_monitoring_notification_channel.email.id]
  }

  depends_on = [google_project_service.apis]
}

# --------------------------------------------------------------------------
# Firestore — the control plane's memory
#
# The location is PERMANENT. It cannot be changed after creation, and `eur3`
# does not include Frankfurt (it is Belgium + Netherlands, witness Finland).
# europe-west3 keeps Firestore in the same region as Cloud Run, so the audit
# write on every tool call is not a cross-region round trip.
# --------------------------------------------------------------------------

resource "google_firestore_database" "default" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  depends_on = [google_project_service.apis]

  lifecycle {
    prevent_destroy = true
  }
}

# --------------------------------------------------------------------------
# Artifact Registry
# --------------------------------------------------------------------------

resource "google_artifact_registry_repository" "warden" {
  location      = var.region
  repository_id = "warden"
  format        = "DOCKER"
  description   = "Warden agent and dashboard images"
  depends_on    = [google_project_service.apis]
}

# --------------------------------------------------------------------------
# Identities — one per agent, plus the proxy and dashboard.
#
# This is tier two of the governance story and the reason it is defence in
# depth rather than a single point of failure. The policy plugin denies an
# out-of-scope call in-process; these bindings mean that even a hypothetically
# bypassed check leaves an agent unable to obtain a credential.
#
# Note what the agent service accounts do NOT have: secretAccessor, and
# datastore.user. They cannot read the estate token or the GitHub token, and
# they cannot write to Firestore — so an agent cannot erase its own audit trail.
# --------------------------------------------------------------------------

resource "google_service_account" "agents" {
  for_each     = toset(local.agents)
  account_id   = "sa-${each.value}"
  display_name = "Warden ${each.value}"
}

resource "google_service_account" "proxy" {
  account_id   = "sa-proxy"
  display_name = "Warden policy proxy — the only identity holding credentials"
}

resource "google_service_account" "dashboard" {
  account_id   = "sa-dashboard"
  display_name = "Warden dashboard"
}

resource "google_project_iam_member" "agent_roles" {
  for_each = {
    for pair in setproduct(local.agents, [
      "roles/aiplatform.user",
      "roles/datastore.viewer",
      "roles/pubsub.publisher",
      "roles/cloudtrace.agent",
    ]) : "${pair[0]}-${replace(pair[1], "/", "-")}" => {
      agent = pair[0]
      role  = pair[1]
    }
  }
  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.agents[each.value.agent].email}"
}

resource "google_project_iam_member" "proxy_roles" {
  for_each = toset([
    "roles/secretmanager.secretAccessor", # the ONLY identity with this
    "roles/datastore.user",               # the ONLY identity that can write audit records
    "roles/aiplatform.user",
    "roles/cloudtrace.agent",
    # Cloud Run runs as this identity. A custom runtime service account does not
    # inherit the default compute account's logging grant, and the symptom is a
    # service that works while producing no logs at all.
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.proxy.email}"
}

resource "google_project_iam_member" "dashboard_roles" {
  for_each = toset(["roles/datastore.viewer", "roles/cloudtrace.agent"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.dashboard.email}"
}

# --------------------------------------------------------------------------
# Event bus
# --------------------------------------------------------------------------

resource "google_pubsub_topic" "events" {
  name       = "warden-events"
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "deadletter" {
  name       = "warden-deadletter"
  depends_on = [google_project_service.apis]
}

resource "google_service_account" "pubsub_invoker" {
  account_id   = "pubsub-invoker"
  display_name = "Pub/Sub to Cloud Run invoker"
}

# The step everyone misses. The Pub/Sub service agent — identified by project
# NUMBER, not project ID — needs token creator to mint the OIDC token for
# authenticated push. Without it, push delivery 403s with a message that does
# not point at this binding.
data "google_project" "this" {
  project_id = var.project_id
}

resource "google_project_iam_member" "pubsub_token_creator" {
  project = var.project_id
  role    = "roles/iam.serviceAccountTokenCreator"
  member  = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# --------------------------------------------------------------------------
# Secrets — containers only. Values are set out of band, never through
# Terraform, because Terraform writes them to state in plaintext.
#
#   printf '%s' "$TOKEN" | gcloud secrets versions add aks-reader-token --data-file=-
# --------------------------------------------------------------------------

resource "google_secret_manager_secret" "secrets" {
  for_each = toset([
    "github-token",
    "aks-reader-token",
    "aks-apiserver",
    "aks-ca-cert",           # the cluster CA, as an inline PEM
    "github-app-key",        # the App private key, mounted as a file on Cloud Run
    "github-webhook-secret", # without it /webhook/github refuses every delivery
  ])
  secret_id = each.value

  replication {
    user_managed {
      replicas { location = var.region }
    }
  }
  depends_on = [google_project_service.apis]
}

# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------

output "artifact_registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/warden"
}

output "service_accounts" {
  value = {
    agents    = { for k, v in google_service_account.agents : k => v.email }
    proxy     = google_service_account.proxy.email
    dashboard = google_service_account.dashboard.email
  }
}

output "budget_guard" {
  value = var.billing_account == "" ? "NOT CREATED — pass -var billing_account=... and re-apply" : "active at $${var.budget_usd}"
}
