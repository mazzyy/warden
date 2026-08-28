#!/usr/bin/env bash
# Put the five secret VALUES into Secret Manager, from the config already on
# this machine.
#
# Terraform creates the secret containers and deliberately never creates a
# version: a value passed through Terraform is written to state in plaintext.
# That leaves an ordering trap — Cloud Run refuses to start a revision whose
# secret has no `latest` version, so the values must exist before the apply
# that needs them, not after.
#
# Nothing here echoes a value. Every secret is piped or read from a file, the
# same rule estate-gitops/scripts/ci-credentials.sh follows, and for the same
# reason: a token in your scrollback is a token on the screen share.
#
#   ./scripts/push-secrets.sh          # push everything missing
#   ./scripts/push-secrets.sh --check  # report what is present, change nothing

set -euo pipefail
cd "$(dirname "$0")/.."

CHECK=false
[[ "${1:-}" == "--check" ]] && CHECK=true

[[ -f .env ]] || { echo "no .env here — run this from the warden repo" >&2; exit 1; }

# Read .env without sourcing it. Sourcing runs whatever is in the file, and a
# PEM pasted across lines would be executed rather than read.
get() {
  local key="$1"
  sed -n "s/^${key}=//p" .env | head -1 | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

PROJECT="$(get GOOGLE_CLOUD_PROJECT)"
[[ -n "$PROJECT" ]] || { echo "GOOGLE_CLOUD_PROJECT is not set in .env" >&2; exit 1; }
echo "project: $PROJECT"
echo

missing=0

# Secrets whose value is a literal string in .env.
push_literal() {
  local secret="$1" key="$2"
  local value; value="$(get "$key")"
  if [[ -z "$value" ]]; then
    echo "  MISSING  $secret  <- $key is empty in .env"; missing=1; return
  fi
  if $CHECK; then echo "  ready    $secret  <- \$$key"; return; fi
  printf '%s' "$value" | gcloud secrets versions add "$secret" \
    --project="$PROJECT" --data-file=- >/dev/null
  echo "  pushed   $secret"
}

# Secrets whose value is a file the .env points at.
push_file() {
  local secret="$1" key="$2"
  local path; path="$(get "$key")"
  path="${path/#\~/$HOME}"
  if [[ -z "$path" || ! -f "$path" ]]; then
    echo "  MISSING  $secret  <- $key points at '${path:-<empty>}' which is not a file"; missing=1; return
  fi
  if $CHECK; then echo "  ready    $secret  <- $path"; return; fi
  gcloud secrets versions add "$secret" \
    --project="$PROJECT" --data-file="$path" >/dev/null
  echo "  pushed   $secret"
}

push_file    aks-ca-cert           AKS_CA_CERT_PATH
push_literal aks-apiserver         AKS_APISERVER
push_literal aks-reader-token      AKS_READER_TOKEN
push_file    github-app-key        GITHUB_APP_PRIVATE_KEY_PATH
push_literal github-webhook-secret GITHUB_WEBHOOK_SECRET

echo
if [[ $missing -ne 0 ]]; then
  echo "Some values are not on this machine. Fill them into .env and re-run;"
  echo "Cloud Run will refuse the revision until every one has a version."
  exit 1
fi

$CHECK && { echo "All five are ready. Re-run without --check to push."; exit 0; }

echo "Versions now in Secret Manager:"
for s in aks-ca-cert aks-apiserver aks-reader-token github-app-key github-webhook-secret; do
  n=$(gcloud secrets versions list "$s" --project="$PROJECT" --format='value(name)' 2>/dev/null | wc -l | tr -d ' ')
  printf '  %-24s %s version(s)\n' "$s" "$n"
done
echo
echo "Now:  cd infra/gcp && terraform apply"
