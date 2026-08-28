# The half of the estate that makes the fleet autonomous.
#
# main.tf provisions identities, memory and an event bus. Nothing consumes the
# bus, and nothing serves a URL — so the only way to start an incident is for a
# person to type a command, which is the one thing the hackathon's premise says
# an agent should not need.
#
# This file closes that: a Cloud Run service that hosts the operations screen,
# the alert ingress and the GitHub webhook on one origin; a push subscription
# that wakes the fleet; and a scheduled sweep so the loop runs with nobody at a
# keyboard.
#
# ORDER MATTERS. Cloud Run refuses to create a service whose image does not
# exist, so build and push once before the first apply:
#
#   gcloud builds submit --tag europe-west3-docker.pkg.dev/$PROJECT/warden/warden:latest
#   terraform apply -var alert_email=you@example.com -var billing_account=...
#
# After that, `gcloud run deploy warden --image ...` redeploys without Terraform
# fighting you for the image field — see the lifecycle block below.

variable "image" {
  type        = string
  default     = ""
  description = <<-EOT
    Full image URI. Defaults to <region>-docker.pkg.dev/<project>/warden/warden:latest,
    which is what the `gcloud builds submit` line above produces.
  EOT
}

variable "sweep_schedule" {
  type        = string
  default     = "*/10 * * * *"
  description = <<-EOT
    How often the fleet wakes itself to look at the estate. Ten minutes is a
    rehearsal cadence: frequent enough that a judge watching the video does not
    wait, cheap enough that it cannot run away with the budget — triage closes a
    healthy sweep for a fraction of a cent and the other three agents never run.
    Set to a longer interval, or disable the job, once recording is done.
  EOT
}

locals {
  image = var.image != "" ? var.image : "${var.region}-docker.pkg.dev/${var.project_id}/warden/warden:latest"
}

# --------------------------------------------------------------------------
# The service
#
# One service, one identity, and that identity is the proxy — the only one in
# the project holding secretAccessor and datastore.user. That is deliberate and
# it is worth stating plainly: after the runtime and the dashboard were
# consolidated onto one origin, the process serving the read-only operations
# screen is the same process that dispatches tools. Splitting them again would
# mean two services, two URLs, and a webhook endpoint whose URL is the one that
# has to stay stable — so the consolidation wins, and the identity follows the
# credential-holding half rather than the read-only half.
#
# The agent service accounts are still credential-less. Nothing here widens
# them: they are what an agent's own IAM would be if an agent ever held one.
# --------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "warden" {
  name     = "warden"
  location = var.region

  # The webhook and the Pub/Sub push both arrive from outside the project.
  # Push is authenticated with OIDC (below); the webhook is authenticated by
  # its HMAC signature, which is why /webhook/github returns 503 rather than
  # accepting anything when the shared secret is unset.
  ingress = "INGRESS_TRAFFIC_ALL"

  # A hackathon project gets torn down and rebuilt. Flip this to true once the
  # URL is the one in the Devpost submission.
  deletion_protection = false

  template {
    service_account = google_service_account.proxy.email

    # An incident is tens of seconds of model calls. The default 300s would cut
    # a slow diagnostician off mid-evidence-chain.
    timeout = "900s"

    scaling {
      # Zero is correct even though an incident can arrive at any time: Pub/Sub
      # push and the GitHub webhook both start an instance. It is also why the
      # store must be Firestore rather than JSONL — the instance that opens the
      # pull request is usually gone by the time the merge arrives.
      min_instance_count = 0
      max_instance_count = 3
    }

    # One incident at a time per instance. The budget ledger and the circuit
    # breaker are per-run, and concurrent runs on one instance would interleave
    # their accounting.
    max_instance_request_concurrency = 1

    containers {
      image = local.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      env {
        name  = "WARDEN_STORE"
        value = "firestore"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "GOOGLE_GENAI_USE_ENTERPRISE"
        value = "1"
      }
      env {
        name  = "ESTATE_ADAPTER"
        value = "aks"
      }
      env {
        name  = "ESTATE_NAMESPACE"
        value = "demo"
      }
      env {
        name  = "GITHUB_OWNER"
        value = "mazzyy"
      }
      env {
        name  = "GITOPS_REPO"
        value = "estate-gitops"
      }
      env {
        name  = "GITHUB_APP_ID"
        value = var.github_app_id
      }
      env {
        name  = "GITHUB_APP_INSTALLATION_ID"
        value = var.github_app_installation_id
      }

      # The private key is a PATH, never an environment value — the same rule
      # the .env file follows, for the same reason. Mounted from Secret Manager
      # below.
      env {
        name  = "GITHUB_APP_PRIVATE_KEY_PATH"
        value = "/secrets/github/key.pem"
      }

      # The cluster CA travels as an inline PEM. _ca_cert_path() accepts either
      # a path or the certificate itself and writes it out, which is what makes
      # a secret env var workable here — a certificate is not a secret, but it
      # is unwieldy, and it must not be missing.
      env {
        name = "AKS_CA_CERT"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["aks-ca-cert"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "AKS_APISERVER"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["aks-apiserver"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "AKS_READER_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["aks-reader-token"].secret_id
            version = "latest"
          }
        }
      }

      # Without this the webhook endpoint refuses every delivery. That is the
      # correct failure — an unauthenticated webhook that runs agents is a
      # remote trigger for anyone who learns the URL.
      env {
        name = "GITHUB_WEBHOOK_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["github-webhook-secret"].secret_id
            version = "latest"
          }
        }
      }

      volume_mounts {
        name       = "github-app-key"
        mount_path = "/secrets/github"
      }

      startup_probe {
        http_get { path = "/healthz" }
        initial_delay_seconds = 5
        timeout_seconds       = 5
        period_seconds        = 5
        failure_threshold     = 12
      }
    }

    volumes {
      name = "github-app-key"
      secret {
        secret = google_secret_manager_secret.secrets["github-app-key"].secret_id
        items {
          path    = "key.pem"
          version = "latest"
        }
      }
    }
  }

  lifecycle {
    # `gcloud run deploy` is how a new build reaches the service during a
    # rehearsal. Without this, the next `terraform apply` would quietly roll it
    # back to whatever tag this file names.
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_project_service.apis,
    google_firestore_database.default,
  ]
}

variable "github_app_id" {
  type        = string
  default     = ""
  description = "GITHUB_APP_ID. Not a secret — an App id is public in the App's URL."
}

variable "github_app_installation_id" {
  type        = string
  default     = ""
  description = "GITHUB_APP_INSTALLATION_ID, from the installation's settings URL."
}

# --------------------------------------------------------------------------
# The alert path
#
# pubsub_invoker exists in main.tf and has never been allowed to invoke
# anything. This is the binding that was missing, and its absence is a 403 on
# every push delivery with a message that does not name it.
# --------------------------------------------------------------------------

resource "google_cloud_run_v2_service_iam_member" "pubsub_invoker" {
  project  = google_cloud_run_v2_service.warden.project
  location = google_cloud_run_v2_service.warden.location
  name     = google_cloud_run_v2_service.warden.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.pubsub_invoker.email}"
}

resource "google_pubsub_subscription" "alerts" {
  name  = "warden-alerts"
  topic = google_pubsub_topic.events.id

  # /pubsub acks immediately and runs the incident in a background task, so the
  # deadline covers delivery rather than the fleet. Keep it short: a long
  # deadline on a fast handler only delays the retry when an instance dies.
  ack_deadline_seconds = 60

  # An alert that cannot be handled must not be redelivered forever — a poison
  # message would wake the fleet, and the budget, on a loop.
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.deadletter.id
    max_delivery_attempts = 5
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.warden.uri}/pubsub"

    oidc_token {
      service_account_email = google_service_account.pubsub_invoker.email
      audience              = google_cloud_run_v2_service.warden.uri
    }
  }

  depends_on = [
    google_cloud_run_v2_service_iam_member.pubsub_invoker,
    google_project_iam_member.pubsub_token_creator,
  ]
}

# Dead letters need somewhere to sit, or the topic drops them and the failure
# mode is silence.
resource "google_pubsub_subscription" "deadletter" {
  name                       = "warden-deadletter-sink"
  topic                      = google_pubsub_topic.deadletter.id
  message_retention_duration = "604800s" # 7 days
}

resource "google_pubsub_topic_iam_member" "deadletter_publisher" {
  topic  = google_pubsub_topic.deadletter.id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription_iam_member" "deadletter_subscriber" {
  subscription = google_pubsub_subscription.alerts.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# --------------------------------------------------------------------------
# The thing that starts an incident when nobody is watching
#
# checkout-svc is a ClusterIP service on AKS, so a Cloud Monitoring uptime check
# cannot reach it and there is no GCP-native signal to alert on. A scheduled
# sweep is the honest alternative, and it is arguably the better demonstration:
# triage's entire job is deciding that most signals are not worth waking the
# fleet for, and a sweep that mostly closes as noise is that argument running
# every ten minutes at a fraction of a cent.
#
# When the estate does emit a real alert, publish it to the same topic and
# nothing downstream changes.
# --------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "sweep" {
  name        = "warden-sweep"
  description = "Wakes the fleet to look at the estate. Triage closes it if nothing is wrong."
  schedule    = var.sweep_schedule
  time_zone   = "Etc/UTC"
  region      = var.region

  pubsub_target {
    topic_name = google_pubsub_topic.events.id
    data = base64encode(jsonencode({
      source    = "scheduler"
      title     = "scheduled estate sweep"
      workload  = "checkout-svc"
      namespace = "demo"
    }))
  }

  depends_on = [google_project_service.apis]
}

# --------------------------------------------------------------------------
# Guarding the demo URL through the judging window
# --------------------------------------------------------------------------

resource "google_monitoring_uptime_check_config" "healthz" {
  display_name = "warden /healthz"
  timeout      = "10s"
  period       = "300s"

  http_check {
    path         = "/healthz"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = replace(google_cloud_run_v2_service.warden.uri, "https://", "")
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "healthz" {
  display_name = "Warden demo URL is down"
  combiner     = "OR"

  conditions {
    display_name = "uptime check failing"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\"",
        "resource.type=\"uptime_url\"",
        "metric.label.check_id=\"${google_monitoring_uptime_check_config.healthz.uptime_check_id}\"",
      ])
      comparison      = "COMPARISON_LT"
      threshold_value = 1
      duration        = "300s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }

      trigger { count = 1 }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]
}

# --------------------------------------------------------------------------
# Outputs — the two URLs the submission needs
# --------------------------------------------------------------------------

output "service_url" {
  value       = google_cloud_run_v2_service.warden.uri
  description = "The hosted project URL. This is the Devpost field."
}

output "webhook_url" {
  value       = "${google_cloud_run_v2_service.warden.uri}/webhook/github"
  description = "Payload URL for the estate-gitops webhook. Content type: application/json."
}

output "next_steps" {
  value = <<-EOT
    1. Set the secret values (Terraform never sees them):
         printf '%s' "$WEBHOOK_SECRET" | gcloud secrets versions add github-webhook-secret --data-file=-
         gcloud secrets versions add github-app-key      --data-file=/path/to/app-private-key.pem
         gcloud secrets versions add aks-ca-cert         --data-file=$HOME/.warden/cluster-ca.crt
         printf '%s' "$AKS_APISERVER"    | gcloud secrets versions add aks-apiserver     --data-file=-
         printf '%s' "$AKS_READER_TOKEN" | gcloud secrets versions add aks-reader-token  --data-file=-
    2. Add the webhook on estate-gitops using webhook_url, event: pull_request.
    3. curl -sf $(terraform output -raw service_url)/healthz
    4. Watch one sweep land:  gcloud run services logs tail warden --region ${var.region}
  EOT
}
