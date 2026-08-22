# Warden — Cloud Setup Runbook

**GitHub:** `mazzyy` · `github.com/mazzyy/warden` · `github.com/mazzyy/estate-gitops`
Every command below is verified against current docs (Aug 2026). Run them in order — the budget guards come **first**, before anything can spend money.

Set these once per shell:

```bash
export PROJECT_ID="warden-hack-2026"        # pick yours
export REGION="europe-west3"                # Frankfurt
export AZ_RG="rg-warden"
export AZ_REGION="germanywestcentral"
export AKS_NAME="aks-warden"
export ALERT_EMAIL="musawar.soomro25@gmail.com"
```

---

## Two corrections before you start

**1. B-series VMs are not supported for AKS system node pools.** My earlier €20–25 estimate assumed `Standard_B2s` — Microsoft doesn't allow it for the system pool, so that would have failed at `az aks create`. Use `Standard_D2s_v5`. Revised cost: **~€58 for eleven days** of two nodes. Against €1000, still comfortably noise.

**2. Your Firestore location is permanent and cannot be changed after creation.** Also, the `eur3` multi-region does *not* include Frankfurt (it's Belgium + Netherlands + Finland witness). If you want Frankfurt, `europe-west3` single-region is the only option, and that's the right call here — keep Firestore in the same region as Cloud Run to avoid cross-region latency on every audit write.

---

## 1 · GCP — project and spend guards

Do this before enabling anything else. With $10 of credit, the budget alert is not optional.

```bash
gcloud projects create $PROJECT_ID
gcloud config set project $PROJECT_ID

# link billing (find your account id first)
gcloud billing accounts list --filter=open=true --format="table(name,displayName,open)"
export BILLING_ACCOUNT="XXXXXX-XXXXXX-XXXXXX"     # strip the billingAccounts/ prefix
gcloud billing projects link $PROJECT_ID --billing-account=$BILLING_ACCOUNT

gcloud services enable billingbudgets.googleapis.com monitoring.googleapis.com

# email channel (note: 'channels' is beta-only, there is no GA equivalent)
gcloud beta monitoring channels create \
  --display-name="Warden alerts" --type=email \
  --channel-labels=email_address=$ALERT_EMAIL
export CHANNEL="projects/$PROJECT_ID/notificationChannels/CHANNEL_ID"   # from the output above

gcloud billing budgets create \
  --billing-account=$BILLING_ACCOUNT \
  --display-name="Warden 5 USD cap" \
  --budget-amount=5USD \
  --threshold-rule=percent=0.50 \
  --threshold-rule=percent=0.90 \
  --threshold-rule=percent=1.00 \
  --notifications-rule-monitoring-notification-channels=$CHANNEL
```

**Gotcha:** `percent=` takes a **fraction** (`0.50`), not `50`. Passing `50` silently creates a 5000% threshold that will never fire — which is exactly the kind of failure you only discover when the credit is already gone.

Set the budget at **$5, not $10**. You want to hear about it with half your balance intact.

## 2 · GCP — APIs

```bash
gcloud services enable \
  aiplatform.googleapis.com \
  run.googleapis.com \
  pubsub.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  cloudtrace.googleapis.com \
  cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  logging.googleapis.com \
  iam.googleapis.com
```

*Note: Vertex AI is now branded "Gemini Enterprise Agent Platform" in the docs. The API host, the `gcloud ai` command group and the metric prefixes are unchanged — it's branding only, so don't be thrown when docs URLs redirect.*

## 3 · GCP — Firestore

```bash
gcloud firestore databases create \
  --database="(default)" \
  --location=$REGION \
  --type=firestore-native \
  --edition=standard
```

**Irreversible.** Re-read the correction above before running it.

## 4 · GCP — Artifact Registry

```bash
gcloud artifacts repositories create warden \
  --repository-format=docker --location=$REGION \
  --description="Warden agent images"
gcloud auth configure-docker ${REGION}-docker.pkg.dev
```

## 5 · GCP — service accounts, one per agent

This is not ceremony. Per-agent identities are what make ADR-002's isolation story provable at the IAM layer as well as the proxy layer, and it's a question a judge may well ask.

```bash
for A in triage diagnostician remediator verifier proxy dashboard; do
  gcloud iam service-accounts create sa-$A --display-name="Warden $A"
done

# agents: publish/consume events, read Firestore, call Gemini. Nothing else.
for A in triage diagnostician remediator verifier; do
  SA="sa-$A@$PROJECT_ID.iam.gserviceaccount.com"
  gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$SA --role=roles/aiplatform.user
  gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$SA --role=roles/datastore.viewer
  gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$SA --role=roles/pubsub.publisher
  gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$SA --role=roles/cloudtrace.agent
done

# the proxy is the ONLY identity that can read secrets or write Firestore
PROXY="sa-proxy@$PROJECT_ID.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$PROXY --role=roles/secretmanager.secretAccessor
gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$PROXY --role=roles/datastore.user

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member=serviceAccount:sa-dashboard@$PROJECT_ID.iam.gserviceaccount.com --role=roles/datastore.viewer
```

Say this out loud in the video: **the agents cannot read a secret even if they wanted to.** Only the proxy holds estate and GitHub credentials, and only the proxy can write the audit log — so an agent cannot erase its own trail.

## 6 · GCP — Pub/Sub with authenticated push

```bash
gcloud pubsub topics create warden-events
gcloud pubsub topics create warden-deadletter

gcloud iam service-accounts create pubsub-invoker --display-name="Pub/Sub → Cloud Run invoker"
INVOKER="pubsub-invoker@$PROJECT_ID.iam.gserviceaccount.com"

# after the triage service exists:
gcloud run services add-iam-policy-binding warden-triage --region=$REGION \
  --member=serviceAccount:$INVOKER --role=roles/run.invoker

# the step everyone misses — uses the project NUMBER, not the ID
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member=serviceAccount:service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com \
  --role=roles/iam.serviceAccountTokenCreator

gcloud pubsub subscriptions create warden-triage-sub \
  --topic=warden-events --ack-deadline=600 \
  --push-endpoint=$TRIAGE_URL/ \
  --push-auth-service-account=$INVOKER \
  --dead-letter-topic=warden-deadletter --max-delivery-attempts=5
```

If push delivery 403s, it is almost always that `serviceAccountTokenCreator` grant — and almost always because the project **ID** got used where the **number** was required.

## 7 · Azure — resource group and AKS

```bash
az group create --name $AZ_RG --location $AZ_REGION

az aks create \
  --resource-group $AZ_RG --name $AKS_NAME \
  --location $AZ_REGION \
  --tier free \
  --node-count 2 \
  --node-vm-size Standard_D2s_v5 \
  --enable-managed-identity \
  --generate-ssh-keys

az aks get-credentials --resource-group $AZ_RG --name $AKS_NAME
kubectl create namespace demo
```

`--tier free` is the current flag (`--uptime-sla` / `--no-uptime-sla` are gone). Managed identity is already the default on new clusters; the flag is harmless.

**To save credit overnight:** `az aks stop -g $AZ_RG -n $AKS_NAME` and `az aks start` in the morning. Optional at your balance — don't spend an hour automating it.

## 8 · Azure — the read-only identity

This is the credential that proves ADR-001. Get it right.

```yaml
# estate-gitops/rbac/warden-reader.yaml
apiVersion: v1
kind: ServiceAccount
metadata: { name: warden-reader, namespace: demo }
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: warden-reader, namespace: demo }
rules:
  - apiGroups: [""]
    resources: [pods, pods/log, events, services, configmaps]
    verbs: [get, list, watch]
  - apiGroups: ["apps"]
    resources: [deployments, replicasets]
    verbs: [get, list, watch]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: warden-reader, namespace: demo }
roleRef: { apiGroup: rbac.authorization.k8s.io, kind: Role, name: warden-reader }
subjects:
  - { kind: ServiceAccount, name: warden-reader, namespace: demo }
---
apiVersion: v1
kind: Secret
metadata:
  name: warden-reader-token
  namespace: demo
  annotations:
    kubernetes.io/service-account.name: warden-reader
type: kubernetes.io/service-account-token
```

```bash
kubectl apply -f estate-gitops/rbac/warden-reader.yaml
TOKEN=$(kubectl -n demo get secret warden-reader-token -o jsonpath='{.data.token}' | base64 -d)
APISERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
```

**Why the Secret rather than `kubectl create token`:** on AKS, `kubectl create token --duration` is capped by an apiserver flag that AKS does not expose, so you cannot mint a token that outlives the hackathon. The annotated Secret produces a non-expiring token instead. This removes the expiry risk entirely for your eleven days — which, conveniently, is also why the health monitor in §10 probes credentials rather than parsing expiry dates.

**Prove it's read-only, and keep the output** — this terminal capture belongs in your video:

```bash
kubectl --token=$TOKEN --server=$APISERVER -n demo get pods          # works
kubectl --token=$TOKEN --server=$APISERVER -n demo delete deploy checkout-svc   # Forbidden
```

Now push both values into Google Secret Manager, where only `sa-proxy` can reach them:

```bash
printf '%s' "$TOKEN"     | gcloud secrets create aks-reader-token --data-file=-
printf '%s' "$APISERVER" | gcloud secrets create aks-apiserver   --data-file=-
```

## 9 · Azure — budget alert

```bash
SUB=$(az account show --query id -o tsv)
az consumption budget create \
  --budget-name warden-cap --amount 150 --time-grain Monthly \
  --category Cost --scope "subscriptions/$SUB" \
  --start-date 2026-08-01 --end-date 2026-12-31
```

If the CLI fights you, do it in **Cost Management → Budgets** in the portal — two minutes, and it's a one-off.

## 10 · GitHub — a token that can only do one thing

Create a **fine-grained PAT** scoped to `mazzyy/estate-gitops` **only**, with:

- Contents: **Read and write**
- Pull requests: **Read and write**
- Everything else: **No access**
- Expiry: **90 days** (comfortably past judging on Oct 1)

Then protect `main` on `estate-gitops`: require a pull request before merging. That's what makes "no agent can merge its own PR" true in the platform rather than only in your prompt.

```bash
gh api repos/mazzyy/estate-gitops --jq '.default_branch'
printf '%s' "$GH_TOKEN" | gcloud secrets create github-token --data-file=-
```

**Verify the token cannot push to main before you trust it** — attempt a direct push and confirm it's rejected. Keep that output too.

---

## 11 · The alerting layer — your token/credit expiry idea

Your instinct here was right, and it turns out to be worth more than a safety net: **credential and budget health is exactly what the Fortified Enterprise Fleet brief means by "security governance and observability."** Real agent fleets don't die from bad reasoning, they die from an expired token at 3am. So this gets built once and serves twice — it protects your eleven days *and* it scores on the rubric.

### New story: W-107 · Fleet health and credential monitor — MUST — 2h

`GET /healthz` on the dashboard service. Every check is a **live probe, not a metadata parse** — probing catches revocation, rotation, quota exhaustion and expiry all at once, where reading an expiry date catches only the last of those.

| Check | Probe | Degraded when |
|---|---|---|
| `github` | `GET /user` with the PAT | non-200, or `github-authentication-token-expiration` < 7 days away |
| `estate` | `list_workloads()` through `AksAdapter` | auth failure or unreachable |
| `vertex` | last `generateContent` outcome | auth failure, or sustained 429s |
| `budget` | Σ `usageMetadata.totalTokenCount` from `runs` × unit price | > 80% of configured cap |
| `firestore` | read `fleet/state` | error |
| `killswitch` | `fleet/state.killSwitch` | reported, not an error |

Returns **200** when everything is healthy, **503** when any critical check fails.

**Read real token counts, don't estimate.** Every Gemini response carries `usageMetadata` with `promptTokenCount`, `candidatesTokenCount` and `totalTokenCount`. Persist those on the `runs` document and your budget ledger becomes exact rather than approximate — which matters when the whole balance is $10.

**Also attach cost-attribution labels to every model call:**

```python
{"contents": [...], "labels": {"agent": "remediator", "incident": incident_id}}
```

Vertex forwards these to billing, so per-agent cost becomes a real query rather than an inference. That is a genuine fleet-governance feature and it costs you one line.

### Wire it to an alert

```bash
gcloud monitoring uptime create "warden-health" \
  --resource-type=uptime-url \
  --resource-labels=host=DASHBOARD_HOST,project_id=$PROJECT_ID \
  --protocol=https --port=443 --path=/healthz \
  --period=5 --timeout=30 --status-classes=2xx

gcloud monitoring uptime list-configs     # grab the generated check_id
gcloud monitoring policies create --policy-from-file=infra/gcp/health-alert.json
```

The policy JSON goes in the repo; it references the `check_id` (not the display name) and the `$CHANNEL` from §1.

**The dual purpose is the point.** The same uptime check that emails you when a credential dies also emails you if the demo URL goes down during the **Sept 1 – Oct 1 judging window** — which is the failure mode most likely to quietly cost you a prize, precisely because you won't be looking.

### Independent safety nets

`/healthz` only helps while Warden is running. These fire even if everything you built is down, so set all three:

- GCP billing budget, $5 at 50/90/100% (§1)
- Azure budget, €150 (§9)
- A calendar reminder for **Sept 28** to re-check the hosted URL before judging closes

---

## 12 · Verification checklist

Don't move on until each of these is a thing you have actually seen:

- [ ] `gcloud billing budgets list --billing-account=$BILLING_ACCOUNT` shows a $5 budget with three thresholds
- [ ] A test email from the notification channel arrived
- [ ] Firestore responds in `europe-west3`
- [ ] `kubectl get pods -n demo` works with the reader token
- [ ] `kubectl delete` with the reader token is **Forbidden** — output saved
- [ ] A direct push to `estate-gitops` `main` with the PAT is **rejected** — output saved
- [ ] Both secrets readable by `sa-proxy`, and **not** by `sa-remediator`
- [ ] A Pub/Sub message reaches a Cloud Run service with authenticated push
- [ ] One Gemini call succeeds and you can read `usageMetadata.totalTokenCount` off the response

The two "Forbidden" captures are demo assets, not just tests. Put them somewhere you'll find them on the 29th.

---

## Revised cost forecast

| | Estimate | Budget | Headroom |
|---|---|---|---|
| Azure — 2× D2s_v5, 11 days | ~€58 | €1000 | vast |
| GCP infra — Run, Firestore, Pub/Sub, Secrets | ~$0 (free tiers) | — | — |
| **Gemini tokens** | the entire question | $10 now, $150 pending | **tight** |

Everything that matters is the last row. Fixtures (ADR-006), Flash for iteration, and manifest token caps are what keep it survivable — and the budget ledger you're building for the demo is the same mechanism that enforces it. That's a true story worth telling in the video.
