# Infrastructure

Two clouds, on purpose (ADR-004). The agents and everything that satisfies the
hackathon's required stack run on **Google**; the Kubernetes estate they watch
runs on **Azure**, because compute is the only expensive non-AI component and
spending Azure credit on it preserves the whole Google balance for Gemini
tokens — the one cost that cannot move.

> ⚠️ These configs were syntax-checked but **not** run through `terraform validate`
> or `plan` (no provider access from where they were written). Run
> `terraform init && terraform plan` and read the plan before applying. Expect
> to fix a provider argument or two.

## Azure first — it is unblocked right now

Nothing here depends on the Google billing question, so you can have a real
cluster before the weekend regardless of how that resolves.

```bash
cd infra/azure
terraform init
terraform plan          # read it
terraform apply
```

Creates a resource group and an AKS cluster: Free-tier control plane, two
`Standard_D2s_v5` nodes, system-assigned identity, Azure CNI with Calico.
Roughly **€58 for eleven days** against your €1000.

**`Standard_D2s_v5`, not `Standard_B2s`.** Microsoft does not support burstable
B-series VMs for AKS system node pools and the create call rejects them — this
is the single most likely thing to fail on a first attempt.

`terraform output next_steps` prints the exact sequence for wiring the cluster
into Warden. Note there is deliberately **no kubeconfig output**: Terraform
writes outputs to state in plaintext, and the admin kubeconfig is a
cluster-admin credential — precisely the thing this project argues should not be
lying around. Use `az aks get-credentials`, and give Warden the read-only
ServiceAccount token from `estate-gitops/rbac/warden-reader.yaml`.

To stop paying overnight: `az aks stop -g rg-warden -n aks-warden`.

## Google

```bash
gcloud billing accounts list --filter=open=true    # get the ID

cd infra/gcp
terraform init
terraform apply \
  -var billing_account=XXXXXX-XXXXXX-XXXXXX \
  -var alert_email=you@example.com
```

Creates 12 APIs, the Firestore database, Artifact Registry, six service
accounts with least-privilege bindings, the Pub/Sub topics, three Secret Manager
containers, and the **$5 budget guard** — which is created before anything that
can spend money, because with a $10 balance an unnoticed burn ends the project
rather than inconveniencing it.

Three things worth knowing before you apply:

**The Firestore location is permanent.** It cannot be changed after creation,
and `eur3` does not include Frankfurt (it is Belgium + Netherlands, witness in
Finland). `europe-west3` keeps Firestore in the same region as Cloud Run, so the
audit write on every tool call is not a cross-region round trip.

**Budget thresholds are fractions, not percentages.** `0.5`, not `50`. Passing
`50` creates a 5000% threshold that never fires — a mistake you would discover
only once the credit was gone.

**Your project is `gen-lang-client-*`**, which means AI Studio created it and it
very likely has no billing account linked. Vertex, Cloud Run and Firestore will
all fail with errors that never mention billing. Check first:

```bash
gcloud billing projects describe gen-lang-client-0473437618
```

Leave `billing_account` empty and the budget guard is skipped — the apply still
works, but do not leave it that way.

## What Terraform deliberately does not do

**No secret values.** The Secret Manager resources are empty containers; values
are added out of band, because Terraform writes them to state in plaintext:

```bash
printf '%s' "$TOKEN" | gcloud secrets versions add aks-reader-token --data-file=-
```

**No Cloud Run services.** Those come from `gcloud run deploy` against built
images, so image tags do not end up in Terraform state on every push.

## The IAM shape, and why it matters

This is tier two of the governance argument, and the reason it is defence in
depth rather than one enforcement point that can fail.

| Identity | Has | Notably lacks |
|---|---|---|
| `sa-triage`, `sa-diagnostician`, `sa-remediator`, `sa-verifier` | `aiplatform.user`, `datastore.viewer`, `pubsub.publisher`, `cloudtrace.agent` | **`secretAccessor`**, **`datastore.user`** |
| `sa-proxy` | `secretAccessor`, `datastore.user`, `aiplatform.user` | — |
| `sa-dashboard` | `datastore.viewer` | everything else |

Two consequences follow directly, and both are worth saying out loud in the
video: an agent **cannot obtain a credential** even if the policy check were
somehow bypassed, and an agent **cannot write to Firestore** — so it cannot
erase its own audit trail. Policy denies it in-process; IAM makes it impossible
out-of-process.

One binding is easy to miss and hard to debug: the Pub/Sub **service agent**
needs `roles/iam.serviceAccountTokenCreator` to mint OIDC tokens for
authenticated push, and it is identified by project **number**, not project ID.
Without it, push delivery returns 403 with a message that points nowhere near
the cause. It is in `main.tf`; just know why it is there.
