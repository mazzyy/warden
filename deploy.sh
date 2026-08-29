#!/usr/bin/env bash
# Build, push, deploy. One script because $PROJECT keeps getting lost between
# shells and a half-set variable produces a tag like
# "europe-west3-docker.pkg.dev//warden/warden:latest" that buildx rejects —
# after which `terraform apply` cheerfully updates the service around an image
# that was never rebuilt, which is the confusing version of this failure.
set -euo pipefail
cd "$(dirname "$0")"

PROJECT="gen-lang-client-0473437618"
IMAGE="europe-west3-docker.pkg.dev/${PROJECT}/warden/warden:latest"

echo "==> building ${IMAGE}"
docker buildx build --platform linux/amd64 --provenance=false -t "$IMAGE" --push .

echo "==> applying infrastructure"
cd infra/gcp
# Non-fatal on purpose. The budget guard is an email alert, and it has already
# blocked this script three times for reasons that had nothing to do with the
# service — a doubled URL prefix, then an unset ADC quota project. A
# nice-to-have must not stand between a build and the checks that say whether
# the build works. Read the warning, fix it separately, keep moving.
if ! terraform apply -auto-approve; then
  echo
  echo "  !! terraform apply reported an error (see above)."
  echo "     Continuing to deploy and verify the service. Re-run terraform"
  echo "     on its own once the cause is fixed."
  echo
fi
URL="$(terraform output -raw service_url)"
cd ../..

# Terraform will NOT pick this up. Cloud Run resolves a tag to a digest when a
# revision is created and pins it there, so pushing over :latest changes
# nothing that is running — and runtime.tf deliberately ignores the image field
# so that this command can own it. Without this line the apply succeeds, the
# plan says no changes, and production keeps serving the previous build.
echo "==> rolling a new revision onto the image just pushed"
gcloud run deploy warden \
  --project "$PROJECT" --region europe-west3 \
  --image "$IMAGE" --quiet >/dev/null
echo "    revision: $(gcloud run services describe warden --project "$PROJECT" \
  --region europe-west3 --format='value(status.latestReadyRevisionName)')"

echo
echo "==> checking ${URL}"
printf '  /healthz          '
curl -s -o /tmp/warden-health -w '%{http_code}\n' "$URL/healthz"
head -c 400 /tmp/warden-health; echo

printf '  /pubsub  no token '
curl -s -o /dev/null -w '%{http_code}  (want 403)\n' -X POST "$URL/pubsub" \
  -H 'content-type: application/json' \
  -d '{"message":{"data":"e30="},"subscription":"warden-alerts"}'

printf '  /pubsub  bad token'
curl -s -o /dev/null -w '%{http_code}  (want 403)\n' -X POST "$URL/pubsub" \
  -H 'content-type: application/json' -H 'authorization: Bearer not-a-real-token' \
  -d '{"message":{"data":"e30="},"subscription":"warden-alerts"}'

printf '  /webhook no sig   '
curl -s -o /dev/null -w '%{http_code}  (want 401 or 403)\n' -X POST "$URL/webhook/github" \
  -H 'content-type: application/json' -d '{}'
