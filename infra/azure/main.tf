# The managed estate (ADR-004).
#
# Warden's agents run on Google Cloud; the workloads they watch run here. That
# split is deliberate: the only expensive non-AI component is a Kubernetes
# cluster, and spending Azure credit on it preserves the entire Google balance
# for Gemini tokens — the one cost that cannot move.
#
# It is also a better enterprise story than a single-cloud demo. Real fleets
# govern workloads they did not provision.
#
#   terraform init && terraform apply
#   az aks get-credentials -g $(terraform output -raw resource_group) \
#                          -n $(terraform output -raw cluster_name)

terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

# --------------------------------------------------------------------------
# Variables
# --------------------------------------------------------------------------

variable "subscription_id" {
  type        = string
  description = "Azure subscription to bill against."
  default     = "77f69e31-9603-4766-8e47-93a380c2cfd1"
}

variable "location" {
  type    = string
  default = "germanywestcentral"
}

variable "prefix" {
  type    = string
  default = "warden"
}

variable "node_count" {
  type        = number
  default     = 2
  description = "Two gives realistic scheduling behaviour. One works and halves the cost."
}

variable "node_size" {
  type    = string
  # NOT a B-series. Microsoft does not support burstable VMs for AKS system
  # node pools, and `az aks create` rejects them — this is the single most
  # likely thing to fail on a first attempt. D2s_v5 is ~EUR 58 for 11 days,
  # which against a EUR 1000 balance is noise.
  default = "Standard_D2s_v5"
}

# --------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------

resource "azurerm_resource_group" "warden" {
  name     = "rg-${var.prefix}"
  location = var.location

  tags = {
    purpose    = "all-things-agentic-hackathon"
    managed-by = "terraform"
    # So a forgotten cluster is obvious in the portal three weeks from now.
    delete-after = "2026-10-15"
  }
}

resource "azurerm_kubernetes_cluster" "warden" {
  name                = "aks-${var.prefix}"
  location            = azurerm_resource_group.warden.location
  resource_group_name = azurerm_resource_group.warden.name
  dns_prefix          = "aks-${var.prefix}"

  # Free tier: no SLA on the control plane, no charge for it either. Correct
  # for a demo estate; `standard` is the flag to change for a real one.
  sku_tier = "Free"

  default_node_pool {
    name       = "system"
    node_count = var.node_count
    vm_size    = var.node_size
    # 30GB is well under the default and enough for one demo workload.
    os_disk_size_gb = 32
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin = "azure"
    network_policy = "calico"
  }

  tags = azurerm_resource_group.warden.tags

  lifecycle {
    # Node count drifts if anything autoscales; that is not worth a diff.
    ignore_changes = [default_node_pool[0].node_count]
  }
}

# --------------------------------------------------------------------------
# Outputs
#
# Deliberately no kubeconfig output. Terraform writes outputs to state in
# plaintext, and the admin kubeconfig is a cluster-admin credential — the exact
# thing this project's whole argument says should not be lying around. Fetch it
# with `az aks get-credentials`, and give Warden the read-only ServiceAccount
# token from estate-gitops/rbac/warden-reader.yaml instead.
# --------------------------------------------------------------------------

output "resource_group" {
  value = azurerm_resource_group.warden.name
}

output "cluster_name" {
  value = azurerm_kubernetes_cluster.warden.name
}

output "apiserver" {
  description = "Feed this into the aks-apiserver secret in Google Secret Manager."
  value       = "https://${azurerm_kubernetes_cluster.warden.fqdn}:443"
}

output "next_steps" {
  value = <<-EOT

    1. az aks get-credentials -g ${azurerm_resource_group.warden.name} -n ${azurerm_kubernetes_cluster.warden.name}
    2. cd ../../../estate-gitops
       kubectl apply -f namespace.yaml
       kubectl apply -f rbac/warden-reader.yaml
       kubectl apply -f apps/checkout-svc/
    3. Prove the reader token cannot mutate, and keep the output for the video:
       TOKEN=$(kubectl -n demo get secret warden-reader-token -o jsonpath='{.data.token}' | base64 -d)
       kubectl --token=$TOKEN --server=${azurerm_kubernetes_cluster.warden.fqdn} -n demo delete deploy checkout-svc
    4. Push both into Google Secret Manager:
       printf '%s' "$TOKEN" | gcloud secrets create aks-reader-token --data-file=-
       printf '%s' "https://${azurerm_kubernetes_cluster.warden.fqdn}:443" | gcloud secrets create aks-apiserver --data-file=-
    5. Set ESTATE_ADAPTER=aks in warden/.env

  EOT
}
