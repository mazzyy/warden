# Warden

A fleet of four autonomous SRE agents that watch a live Kubernetes estate, diagnose failures from real evidence, and land the fix as a reviewed pull request.

The whole system rests on one decision:

> No agent holds production write credentials. Their only write primitive is opening a pull request.

Everything else here — the policy plugin, the YAML manifests, the read-only ServiceAccount, the three verification scripts — exists to make that sentence true rather than merely printed.

Built for the All Things Agentic Hackathon, in the Fortified Enterprise Fleet category.

---

## Status, honestly

This section is first on purpose. A governance project that oversells itself is worse than one that admits its gaps.

There are eight claims the design makes. All eight are now enforced by something outside the agent's reach, and each one has been observed refusing the forbidden action against live infrastructure — not asserted by reading a configuration value back.

| Claim | What enforces it | How it was proven |
| --- | --- | --- |
| An agent cannot call a tool it was not granted | ADK `before_tool_callback`, in-process | `make probe` — 4 agents × 11 tools, every denial exercised |
| No agent can write to the cluster | `warden-reader` ServiceAccount, get/list/watch only | `verify-rbac.sh` — attempts a delete, is refused |
| A patch cannot exceed its blast radius | Namespace, file count and line count, checked twice | Policy tests, plus a second check against the real diff |
| The logs a diagnosis rests on are real | Cluster CA pinned on the API connection | TLS handshake verified before the CA is accepted |
| Only CI can change the estate | `warden-sync` ServiceAccount, namespaced, no `delete` verb | `verify-sync-rbac.sh` — 4 verbs allowed, 6 denied |
| The agent cannot merge its own pull request | A GitHub App, which holds no repository role | Direct commit to protected `main` returned **HTTP 409** |
| The audit trail holds no secrets | Recursive redaction on tool arguments and results | 10 tests, including keys the first version missed |
| A merge closes the incident that opened *that* pull request | HMAC signature check, then a match on `pr_url` | 15 tests — with two incidents in flight, the wrong one stays open |

Row six is the one worth reading twice, because it started as the project's open gap and the fix was not the obvious one. A fine-grained personal access token **inherits the repository role of the human who minted it**. We measured that: with branch protection enabled requiring one approving review, a direct commit to `main` using the agent's own token returned **HTTP 201** and landed. Narrow scopes decide which repositories and which APIs a token may touch. They do not decide who the token *is*.

On that path, "the agent cannot merge its own work" was true only because Warden's code never called the merge endpoint. A promise, not a control. A GitHub App holds no repository role at all, so protection applies to it with no bypass to inherit — the same commit now returns **HTTP 409**, and the App cannot approve a pull request even in principle. The full measurement, both numbers, and the setup are in [`docs/IDENTITY.md`](docs/IDENTITY.md).

Warden still says which credential it is running as, out loud, on every run: the demo header prints `boundary: enforced` or `NOT enforced`, the pull request footer changes wording depending on the answer, and `verify-github-token.sh` exits non-zero on the weaker path.

Two incident types have been built. One — a bad configuration value — has been fixed end to end against real infrastructure several times. The second, an OOMKilled workload, is injected and waiting. See [What it can actually fix](#what-it-can-actually-fix).

123 tests pass across 12 files. No cloud project, API key or spend is required to run them.

---

## The shape of it

An incident moves through seven stages. The fleet occupies four of them.

```mermaid
flowchart LR
    A["1 Alert<br/><i>Cloud Monitoring</i>"] --> T["2 Triage<br/><b>agent</b>"]
    T --> D["3 Diagnose<br/><b>agent</b>"]
    D --> R["4 Patch<br/><b>agent</b>"]
    R --> H["5 Human review<br/><i>a person</i>"]
    H --> C["6 Apply<br/><i>warden-sync CI</i>"]
    C --> V["7 Verify<br/><b>agent</b>"]
    V -->|recovered| X(["incident closed"])
    V -->|did not recover| R

    classDef agent fill:#3d3218,stroke:#d9a441,color:#f2e7d0
    classDef human fill:#1c2a1f,stroke:#4fbb8c,color:#dbeae0
    class T,D,R,V agent
    class A,H,C human
```

Stages 1, 5 and 6 have no agent in them, and that is the point of the picture. Nothing the fleet does reaches the cluster without passing through a human and a CI credential no agent holds. `tests/test_live_view.py` asserts that no agent can ever appear in the review or apply columns.

### The four agents

Each is a separate LLM call with its own tool grants, its own token budget and its own YAML manifest.

**triage** — the phone screener. Sees only the alert text and past incidents. It has no cluster access at all. Decides severity, whether this duplicates an open incident, and whether it is worth waking the rest of the fleet. Most alerts should die here.

**diagnostician** — the investigator. Reads pod specs, container logs, deployment history and metrics from the live cluster. Holds no write scope of any kind, to the cluster or the repository. Produces one root cause with the evidence chain behind it.

**remediator** — the only agent that can change anything, and only one way. Reads the GitOps repository and opens a pull request. Capped at three files and twelve changed lines, enforced at the write path against the real diff.

**verifier** — the checker. After a merge, reads cluster status and metrics and decides whether the workload actually recovered. If not, it opens a revert pull request. It can propose a revert; it cannot apply one.

### What each agent may call

Thirteen tools exist in the catalog. Two of them are granted to nobody.

| Tool | triage | diagnostician | remediator | verifier | Reaches |
| --- | :---: | :---: | :---: | :---: | --- |
| `get_alert_context` | yes | | | | the alert itself |
| `recall_similar_incidents` | yes | | | | incident store |
| `describe_workload` | | yes | | | cluster, read |
| `get_workload_logs` | | yes | | | cluster, read |
| `recent_deploys` | | yes | | | cluster, read |
| `query_metrics` | | yes | | yes | cluster, read |
| `get_workload_status` | | | | yes | cluster, read |
| `list_repo_files` | | | yes | | repository, read |
| `read_repo_file` | | | yes | | repository, read |
| `propose_patch` | | | yes | | opens a pull request |
| `request_revert` | | | | yes | opens a pull request |
| `delete_workload` | | | | | nothing — granted to nobody |
| `scale_workload` | | | | | nothing — granted to nobody |

`delete_workload` and `scale_workload` are registered deliberately and granted to no manifest. An agent can genuinely attempt the call and policy genuinely refuses it. A denial demo against a tool that does not exist would prove nothing.

Run `make probe` to print this matrix from the live policy engine rather than from this table.

---

## Where an alert comes from, and how a failure is recognised

The fleet does not watch anything. Something else notices, and hands it a signal. That distinction is load-bearing: an agent that polls a cluster on a loop is a monitoring system with a language model bolted on, and it has to hold cluster credentials continuously to do it. Warden is woken, does one incident, and goes back to sleep.

There are three doors and two of them are open today.

**Pub/Sub push.** `POST /pubsub` takes a Cloud Monitoring notification, base64-decodes the payload and starts an incident in the background. This is the intended production path: Cloud Monitoring owns the thresholds, Pub/Sub owns delivery and retry, and the fleet owns only the response. The endpoint acknowledges before it works, because an incident takes tens of seconds and Pub/Sub's ack deadline is shorter than that — a late ack means redelivery, and redelivery means running the fleet twice on one alert and opening two pull requests for one fault.

**The demo, locally.** `make demo-live` reads the live cluster and builds the alert out of what it actually finds. This is the honest version of a canned demo, and it exists because of a bug: the alert used to say `0/3 replicas ready` while three replicas were serving traffic perfectly well. Handing an agent a symptom that did not happen grades the run on the wrong thing — the diagnosis can match the alert beautifully and prove nothing about the cluster.

**A GitHub webhook.** `POST /webhook/github` is not an alert path. It is how a merge tells the Verifier there is something to check, and it is listed here because it is the other thing that wakes the fleet without a human running a command.

### What the signal actually contains

Four fields, and Triage sees nothing beyond them.

| Field | Example | What reads it |
| --- | --- | --- |
| `title` | `checkout-svc: rollout BLOCKED — 0/3 pods on the new revision, OOMKilled` | Triage, as prose |
| `signature` | `checkout-svc/OOMKilled` | Triage, to look for duplicates |
| `workload` / `namespace` | `checkout-svc` / `demo` | every later stage, as the target |
| `source` | `cloud-monitoring` | the audit record |

Triage has no cluster access at all — no logs, no pod specs, no metrics, no deploy history. That is deliberate rather than incidental. The first stage is a phone screener, and a phone screener that *can* investigate will investigate: it would cost what the investigator costs and answer a different question. Triage decides severity, whether this duplicates something already open, and whether the rest of the fleet is worth waking. Everything it needs for that is in the four fields above.

The signature is derived from structured cluster fields rather than from the summary prose, and it names the container's failure reason in preference to the symptom — `checkout-svc/OOMKilled`, never `checkout-svc/RolloutBlocked` when a reason is available. Both of those rules are scar tissue. Splitting the summary on an em dash once produced `checkout-svc/Error`, too vague for an agent whose only other tool is a history lookup. And leading with the symptom made every stalled rollout share one signature, so the second fault the fleet ever saw matched the first one's and would have been dismissed as a duplicate before anyone looked at the cluster.

### How a failure is recognised at all

No model decides what "broken" means. The estate adapter does, in plain Python, before any agent runs.

It reads the Deployment's replica counts and every pod's container statuses, and treats a fixed set of container reasons as failure: `CrashLoopBackOff`, `OOMKilled`, `ImagePullBackOff`, `ErrImagePull`, `InvalidImageName`, `CreateContainerConfigError`, `CreateContainerError`, `RunContainerError`, and a bare `Error`. That list is a constant in `warden/estate/aks.py`, reviewable as a diff, and a reason not on it is not an incident.

Rollout health is a separate question from replica health, and conflating them cost a live run. `readyReplicas == desiredReplicas` stays true for the *entire duration* of a completely blocked rollout, because the previous ReplicaSet keeps serving while the new one crashloops behind it. So the adapter also reads `updatedReplicas` and the `ProgressDeadlineExceeded` condition, and a stalled rollout reports as blocked even while every replica is ready and every user is fine.

---

## How the governance actually works

Two independent tiers, so a failure in one does not become a breach.

```mermaid
flowchart LR
    M["Gemini asks<br/>for a tool"] --> P

    subgraph t1["Tier 1 — in-process"]
        P["policy plugin<br/><i>six checks, in order</i>"]
    end

    P -->|all pass| R["tool runs"]

    subgraph t2["Tier 2 — out of process"]
        RB["Kubernetes RBAC<br/>get / list / watch only"]
        GH["GitHub<br/>branch protection"]
        IAM["GCP IAM<br/>no secretAccessor<br/>on agent service accounts"]
    end

    R --> RB
    R --> GH
    R --> IAM

    P -->|any check fails| D["denied<br/><i>audited with a reason</i>"]

    classDef bad fill:#38201c,stroke:#e4796b,color:#f3ded9
    classDef good fill:#14301f,stroke:#4fbb8c,color:#dbeae0
    class D bad
    class RB,GH,IAM good
```

Tier one runs six checks in order, and the first failure denies the call and writes an audit record with the reason:

1. **Kill switch.** One flag drains the whole fleet. It denies every tool for every agent, including read-only ones.
2. **Catalog membership.** A tool absent from `tools/catalog.py` cannot be called at all, and a manifest granting an unknown tool fails loudly at load time rather than mysteriously at run time.
3. **The manifest allow-list.** Whatever the model asks for, an agent gets only what its YAML grants.
4. **Scopes.** Each tool declares the scopes it costs; the agent must hold all of them.
5. **Blast radius.** Namespace, maximum files per patch, maximum changed lines.
6. **Budget.** Per-run token and tool-call ceilings, plus a circuit breaker that opens after three consecutive failures.

That tier is an ADK plugin, not a separate service. ADK's plugin hooks fire for every tool of every agent in the tree and run before per-agent callbacks, so there is no path around them from inside the process — and no network hop on the hot path.

The blast radius is checked **twice**: once here against the declared file list, and again at the write path against the real computed diff. Policy alone would not have caught the pull request that stayed inside one permitted file while deleting thirty-nine lines of it.

Tier two is why this is defence in depth rather than a single point of failure. The agent service accounts do not hold `roles/secretmanager.secretAccessor`, so even a hypothetically bypassed policy check leaves an agent unable to obtain a cluster or GitHub credential at all.

Two ADK contract details are load-bearing here and are easy to get wrong. Plugin callbacks are keyword-only and use `tool_args` / `result`, where the per-agent callbacks use `args` / `tool_response` — getting the names wrong means the hook silently never fires. And the tool is skipped whenever the return value is not `None`, but the callback chain only breaks on a *truthy* return, so returning `{}` blocks the tool while still iterating remaining callbacks. Always deny with a non-empty dict.

### One incident, in sequence

```mermaid
sequenceDiagram
    autonumber
    participant Alert as Cloud Monitoring
    participant Tri as triage
    participant Dia as diagnostician
    participant Rem as remediator
    participant GH as estate-gitops
    participant You as reviewer
    participant CI as warden-sync
    participant K8s as AKS cluster
    participant Ver as verifier

    Alert->>Tri: signal
    Tri->>Tri: get_alert_context, recall_similar_incidents
    Note over Tri: escalate, or close as noise or duplicate
    Tri->>Dia: escalated
    Dia->>K8s: describe_workload, get_workload_logs,<br/>recent_deploys, query_metrics
    K8s-->>Dia: real pod state, real logs, real revisions
    Dia->>Rem: root cause plus evidence chain
    Rem->>GH: list_repo_files, read_repo_file
    Rem->>GH: propose_patch (branch only, never main)
    Note over Rem,GH: refused if it touches more than<br/>3 files or 12 lines
    GH->>You: pull request awaiting review
    You->>GH: approve and merge
    GH->>CI: sync workflow
    CI->>K8s: kubectl apply (namespaced, no delete verb)
    Ver->>K8s: get_workload_status, query_metrics
    Note over Ver: an independent status read decides,<br/>not the model's prose
    Ver-->>Alert: incident closed
```

---

## What it can actually fix

**It does not fix application source code, and it structurally cannot.** This is the first question everyone asks, so it belongs at the top of the section rather than buried in the caveats. The remediator holds exactly one repository — `estate-gitops`, the declared desired state of the estate — and the service's own source lives somewhere else entirely. There is no tool in the catalog that reaches it. If the bug is a null dereference in the checkout handler, Warden will tell you the container is crashing, show you the stack trace it read from the pod, and stop. Fixing it is a human's job in a different repository.

What it changes is the *deployment* of the code: environment variables, resource limits, image tags, replica counts, probe configuration, timeouts. YAML, in other words — the layer where a large share of production incidents actually originate, and the layer where a wrong value is a one-line diff rather than a rewrite.

That boundary is not a limitation we are apologising for. It is most of the reason the central claim holds. A remediator that could edit arbitrary source would need write access to every repository in the estate; one that edits declarative state needs access to a single repository whose entire contents are reviewable in a diff, and whose changes only reach the cluster through a CI credential the agent does not have.

Beyond that boundary, a fix is possible when two things are true at once: the root cause is visible in what the diagnostician can read, and the fix is a small edit to a YAML file in the GitOps repository.

**Proven end to end against real infrastructure.** A wrong environment variable value — `htps://` instead of `https://` — crashlooping the service on startup. Real crashloop, real container logs, agent-authored one-line diff, human merge, cluster recovered. Pull requests #5 and #6 on `estate-gitops`.

**Built, never run live.** An OOMKilled workload: `inject.sh oom` sets the memory limit to 64Mi against a 128MB warm-up. The fix is a different edit to a different part of the same file, which is exactly why it is the valuable second test.

**The class it covers.** Wrong config values, resource limits set too low, a bad image tag, replica counts, misconfigured readiness or liveness probes, timeouts that are too aggressive.

**What it cannot do.**

- Anything needing an application code change, per the boundary above.
- Anything larger than three files or twelve changed lines. Refused at the write path, not merely discouraged. That cap exists because a live run once proposed a fix that also replaced the container image with one that did not exist and deleted thirty-nine lines of working entrypoint on the way.
- Anything outside the `demo` namespace. Refused by policy before dispatch.
- Anything requiring a cluster action rather than a file change — restarting a pod, draining a node, scaling up, rolling back.
- Anything whose evidence is not in logs, pod specs, deploy history or metrics. There is no distributed tracing and no APM, and `query_metrics` is derived from workload state rather than a real Prometheus. That is documented in the adapter and worth saying out loud rather than letting a reader discover it.

---

## Running it

Python 3.11 or newer. Node 18+ only if you want the dashboard.

### Everything below this line needs no cloud, no key, and no spend

```bash
git clone https://github.com/mazzyy/warden.git && cd warden
make install
cp .env.example .env
make doctor
```

`make doctor` prints what is configured, what is missing, and what you can run right now. It is the first thing to try when something does not work.

**One incident, end to end, offline.**

```bash
make demo
```

Injects a bad config into a fake in-memory estate and runs the whole fleet against scripted models. You get the full trace: every tool call, every allow and deny, the token count, the estimated cost, and a described pull request. Nothing is opened on GitHub.

`make demo-oom` runs the same loop against the other failure mode — an OOMKilled workload rather than a bad config — so you can see the fleet handle a different root cause without a cluster.

Scripted models are a development affordance. The hackathon requires unedited live execution, so the submitted video must run `--live`; the demo says so on screen every time you run it offline.

Every run writes to a file-backed store under `.warden-state/` and clears it first, so rehearsing three times does not open the fourth take on a wall of stale incidents. `--keep-history` appends instead, and `--memory` leaves no trace at all.

**The policy matrix.**

```bash
make probe
```

Evaluates every agent against every tool through the live policy engine and prints the result.

```bash
./.venv/bin/python -m warden.probe --assert-no-cluster-writes
```

That exits non-zero if any agent in the fleet can reach a cluster write. It runs as part of `make check`, so the central claim is a build failure rather than a sentence in a README.

**The test suite.**

```bash
make test     # 123 tests
make check    # lint, tests, and the no-cluster-writes assertion
```

**The operations dashboard.**

```bash
make dashboard
```

Builds the SPA if needed and serves it with the API on `http://localhost:8080`. Open it in one terminal and run an incident in another — the agents move on screen as they work. Click any tool call under *What it did* to see the arguments it was called with and the result it got back.

If no incident has ever run against the store, the header says SAMPLE DATA in amber until a real one arrives.

### Live models

Two paths, and they have different model catalogs.

**Vertex AI** — recommended. Uses application default credentials, no key in a file.

```bash
gcloud auth application-default login
```

In `.env`:

```
GOOGLE_GENAI_USE_ENTERPRISE=1
GOOGLE_CLOUD_PROJECT=your-project
GOOGLE_CLOUD_LOCATION=europe-west3
```

**Gemini API** — simpler to start, has a 5 requests-per-minute free tier that will rate-limit a four-agent incident. Warden retries and honours the server's own `retryDelay`, but expect a slow run.

```
GOOGLE_GENAI_USE_ENTERPRISE=0
GOOGLE_API_KEY=your-key
```

Check which models your credential can actually reach before trusting a manifest:

```bash
make models
```

The catalogs genuinely differ. `gemini-3.7-flash` exists on the Gemini API and 404s on Vertex in `europe-west3`. All four manifests currently pin `gemini-3.5-flash`, which is available on both.

```bash
make demo-live
```

### Live against a real cluster

You need an AKS cluster with the `estate-gitops` manifests applied. `infra/azure/main.tf` provisions one; see [`docs/SETUP.md`](docs/SETUP.md) for the full walkthrough.

Extract the read credential from the cluster:

```bash
cd ../estate-gitops
./scripts/extract-reader-credentials.sh
```

That script only reads. It prints four lines to paste into `warden/.env`, and it refuses to print anything unless the CA it found actually completes a TLS handshake against your API server first — a CA that does not verify is worse than none, because it fails mid-incident and looks like the cluster went down.

```
ESTATE_ADAPTER=aks
AKS_APISERVER=https://...
AKS_READER_TOKEN=...
AKS_CA_CERT_PATH=~/.warden/cluster-ca.crt
```

The CA is not optional. Skipping certificate verification does not weaken writes — the agent has no write scope to weaken. It weakens knowing who you are talking to, and the diagnostician's entire output is derived from the logs it reads. Feed it forged logs and it will open a confident, fully-evidenced pull request fixing a bug that never existed.

Then the full loop:

```bash
# 1. break something, the way a human breaks things: by committing it
cd ../estate-gitops
./scripts/inject.sh bad-config
sleep 45

# 2. run the fleet
cd ../warden
make demo-live

# 3. review the pull request it opened, then merge it

# 4. apply the merged change
cd ../estate-gitops
git pull
kubectl apply -f apps/checkout-svc/deployment.yaml
kubectl rollout status deploy/checkout-svc -n demo --timeout=180s

# 5. close the loop
cd ../warden
./.venv/bin/python -m warden.agents.demo --live --verify-only
```

Step 5 exists because the merge is a human action that happens after the demo process has exited. By the time verifying means anything, the process that would have done it is long gone. `--verify-only` runs the verifier alone against the last incident in the store.

To make the merge itself wake the verifier — no step 5 at all — add a webhook on `estate-gitops` pointing at a running dashboard. The dashboard mounts the ingest router, so `make dashboard` already serves `/webhook/github` alongside the operations screen; there is no second process and no second URL. That is [`docs/WEBHOOK.md`](docs/WEBHOOK.md), and the only awkward part is giving GitHub a public hostname, which deploying to Cloud Run solves permanently.

`./scripts/inject.sh restore` puts the estate back to known-good. Run it before every take.

---

## What you need to connect

| What | Why | Where it goes | Required for |
| --- | --- | --- | --- |
| Nothing | offline demo, tests, policy matrix | — | `make demo`, `make test`, `make probe` |
| Vertex ADC or a Gemini API key | live model calls | `.env` | `make demo-live` |
| GitHub fine-grained PAT | opening real pull requests | `GITHUB_TOKEN` in `.env` | live pull requests |
| GitHub App id, installation id, private key path | the enforced write boundary | `GITHUB_APP_*` in `.env` | a write path branch protection refuses |
| AKS API server, reader token, cluster CA | reading a real estate | `AKS_*` in `.env` | `ESTATE_ADAPTER=aks` |
| `KUBE_APISERVER`, `KUBE_TOKEN`, `KUBE_CA` | CI applying merged changes | GitHub repository secrets | the sync workflow |
| A Google Cloud project | Firestore, Cloud Run, Pub/Sub | `infra/gcp/main.tf` | deployment |

The GitHub token is scoped to `estate-gitops` only, with contents and pull-requests write and nothing else. The App private key is referenced by **path**, never pasted into `.env` — a PEM in a dotenv file gets shoulder-surfed on a screen share and committed by accident.

`.gitignore` covers `.env`, `.env.*` and `*.pem`. The `.env.*` glob is deliberate: a `.env.bak` made while editing still holds the token.

Secret Manager names are configured separately from secret values, and `config.py` refuses to start if what looks like a credential is pasted into a `SECRET_*` name field. That check exists because the failure is otherwise silent and baffling — the real token sits in `.env` in plain sight while the code looks up a Secret Manager secret named `github_pat_...`, finds nothing, and quietly runs in dry-run.

---

## The three proofs

Each lives in the `estate-gitops` repository and each attempts the forbidden action rather than reading a configuration value. All three are safe to run on camera.

```bash
./scripts/verify-rbac.sh            # the agent's read credential
./scripts/verify-sync-rbac.sh       # the CI applier's write credential
./scripts/verify-github-token.sh    # the agent's GitHub credential
```

`verify-rbac.sh` exists because the obvious version of this test is wrong. Running `kubectl --token=$TOKEN delete deploy/checkout-svc` appears to work — and it deletes the deployment. Not because RBAC failed, but because an AKS admin kubeconfig authenticates with **client certificates**, and Kubernetes authenticates a valid client cert regardless of any bearer token you also pass. The `--token` flag is silently ignored and the command runs as cluster-admin. `--kubeconfig=/dev/null` is what forces the token to be the only credential in play.

That mistake is the reason for the rule the rest of this project follows: a control you have not seen refuse something is not a control.

---

## Repository layout

```
warden/
  manifests/agents/       four YAML files — the fleet, as configuration
  warden/
    proxy/plugin.py       the choke point. Every tool call passes here
    control_plane/
      policy.py           pure functions: kill switch, catalog, scopes, blast radius
      registry.py         loads and validates manifests, rejects unknown tools
      budget.py           per-run token and tool-call ledger
      store.py            Store protocol, in-memory and Firestore
      jsonl_store.py      file-backed store shared by the demo and the dashboard
    estate/
      base.py             EstateAdapter protocol — the cloud-agnostic boundary
      aks.py              the real thing, read-only by contract and by credential
      fake.py             four failure modes, no cloud, free
    tools/
      catalog.py          every tool and the scopes it costs
      toolbox.py          binds tools to an incident
      github_client.py    the only write path in the system
    agents/
      definitions.py      the instructions. Prompts are source code here
      orchestrator.py     the pipeline, in plain Python rather than an ADK tree
      runtime.py          one agent run: budget, retries, usage accounting
      demo.py             the terminal trace
    dashboard/            FastAPI plus a React SPA, and the ingest router
    doctor.py             preflight: what is configured, what is missing
    probe.py              the policy matrix, and the CI assertion
    ingest.py             Pub/Sub push and GitHub webhook, as a mountable router
    server.py             the same router, standalone, for running it without the UI
  infra/azure/            AKS cluster, Free tier control plane
  infra/gcp/              Firestore, Pub/Sub, Artifact Registry, six service accounts
  docs/SETUP.md           cloud setup, verified command by command
  docs/IDENTITY.md        who the agent writes as, and why it matters
  docs/WEBHOOK.md         making the merge itself wake the verifier
  tests/                  123 tests across 12 files
```

`ingest.py` is a router rather than an app because the webhook needs a stable public URL and the dashboard needs to be deployed anyway. Two apps meant two Cloud Run services, two URLs and two deploys; one router mounted by both means the operations screen you are already running is also the webhook receiver.

The orchestrator is deliberately plain Python rather than an ADK multi-agent delegation tree. Each agent gets its own run, its own budget and its own audit trail, and the handoffs between them are explicit and inspectable. When the demo is a live break-and-fix on camera, being able to point at exactly what happened and when is worth more than elegance.

---

## What the live runs found

Every one of these came from running against real infrastructure, not from a test. Each produced a permanent fix and a regression test. This list is the most useful thing in the repository, because it is the difference between a demo and a system.

| What happened | What it cost, and what fixed it |
| --- | --- |
| Triage closed an incident as a duplicate of itself | The incident is persisted before any agent runs, so recall handed it back. Bind the current incident and exclude it. |
| Two pull requests opened containing no changes | The agent wrote back content identical to what was there. No-op detection now deletes the branch and returns an error. An empty pull request claiming to fix something looks like success. |
| The remediator guessed file paths, 404ing twice per run | Added `list_repo_files`. Token use fell from 26,030 to 10,517. |
| The RBAC proof was fake | `kubectl --token` was silently ignored because the admin kubeconfig authenticates with client certificates. The delete succeeded and destroyed the deployment. |
| A pull request proposed a container image that does not exist and deleted 39 lines of working entrypoint | Root cause: `describe_workload` was not returning `command` and `args`, so the agent saw a bare base image and concluded it was wrong. Added those fields and a line-level blast radius. |
| The demo hardcoded `0/3 replicas ready` | False against the real cluster, where three replicas were serving fine. The alert is now read from the estate. A fabricated symptom grades the agents on the wrong thing. |
| The agent's PAT committed straight to a protected `main` — HTTP 201 | A PAT inherits its owner's repository role. Documented, warned about at runtime, and fixed properly by a GitHub App. |
| A blocked rollout reported itself as healthy | `ready == desired` is true for the entire duration of a blocked rollout, because the previous ReplicaSet is still serving. Triage read `3/3 replicas ready` and closed the incident, correctly, on a false sentence. Status now reads `updatedReplicas` and `ProgressDeadlineExceeded`. |
| Triage refused to escalate a real incident | Its instruction said an escalation "wastes an engineer's attention". True on a human rota, false here — escalating wakes three agents for a few cents. The cost model was wrong and Triage applied it correctly. |
| The sync workflow claimed to hold the only credential that can change the estate | It had never run, and its design wanted Contributor on the whole resource group — enough to delete the cluster — in order to patch one Deployment. |
| TLS verification was off against the real API server | Forged logs produce a confident diagnosis of a bug that never existed. The CA was in the same Secret as the token the whole time. |
| The token verification script read HTTP 403 as "no rule exists" | A credential that cannot read its own guardrail is behaving correctly. The script now attempts the write instead of reading configuration. |
| A tool that raised an exception was never audited | `after_tool_callback` does not fire on an error, so a cluster timeout left no trace. An audit log that drops failures reads as a clean run. |
| The result redactor missed `x-api-key` | It listed spellings instead of normalising keys. Caught by its own test, which is the only reason to write tests for a redactor. |
| The merge webhook re-ran the whole pipeline | It called `handle_incident(..., verify=True)`, which starts at Triage. On a merge that opens a *fresh* incident for a workload that was just fixed, and could propose a second pull request for a fault that no longer exists. The merge is the end of an incident, not the start of one. |
| The merge closed "the most recent incident" | Correct with one incident in flight; with two it marks a real, unfixed fault as resolved. It matches on `pr_url` now, and verifies nothing at all if no stored incident carries that URL. |

The one exception to "found by running it": two different faults shared one signature. Triage deduplicates on that string, and it led with the symptom — `checkout-svc/RolloutBlocked` — but an OOMKilled workload and a workload crashlooping on a bad configuration value both stall a rollout. The second fault the fleet ever saw would have matched the first one's signature and been dismissed as a duplicate of an incident already awaiting a merge, before anyone looked at the cluster. Found while setting up the second live run rather than during it. The signature now leads with the container reason and falls back to the symptom only when the cluster offers no cause; the regression test asserts neither signature is a substring of the other, because the recall tool matches by substring.

---

## What is remaining

Every governance control is now enforced and proven. What is left is coverage, deployment and submission.

**Run the second incident type live.** `./scripts/inject.sh oom` produces an OOMKilled workload — a different symptom, different evidence, and a patch to a different part of the same file than the configuration fault. The estate is currently sitting in exactly that state. Running it is what turns "it fixed a typo" into "it diagnoses root causes", and it is the highest-value remaining item.

**Apply the Google Cloud Terraform.** `infra/gcp/main.tf` provisions Firestore, Pub/Sub, Artifact Registry, six service accounts with least-privilege bindings, empty Secret Manager containers and a five-dollar budget alert. Firestore is what lets an incident outlive a local process — and, once deployed, survive a Cloud Run instance scaling to zero between the pull request and the merge.

**Deploy to Cloud Run.** One service, one origin: the SPA, `/api/*`, the Pub/Sub push endpoint and the GitHub webhook all come from the same image. That consolidation is why the webhook is nearly free at this point — it needs a stable public URL, and the deploy is what provides one. A Cloud Monitoring uptime check against `/healthz` guards the demo URL through the judging window.

**Add the GitHub webhook.** Ten minutes once the service URL exists. [`docs/WEBHOOK.md`](docs/WEBHOOK.md) has the payload URL, the secret handling and a debugging table. Until then the last step of the loop is `--verify-only`, run by hand.

**Record the four-minute video.** Live, unedited execution is required by the rules. Rehearse three times, and run `./scripts/inject.sh restore` before every take.

**Write the Devpost submission.**

Done since the last revision of this section:

- **The CI applier is wired.** `rbac/warden-sync.yaml` applied, `sync.yml` in `.github/workflows/`, and all three repository secrets set through stdin so the cluster token never touched a scrollback.
- **TLS verification is on**, and the handshake is checked before the CA is trusted rather than after.
- **The GitHub App holds the write path**, and branch protection refuses it — HTTP 409 where the personal access token got 201.
- **The runtime and the dashboard are one service.** The Pub/Sub and webhook endpoints moved into a router (`warden/ingest.py`) that both apps mount, so one deploy and one URL serve the operations screen, the alert ingress and the GitHub webhook. Two services meant two deploys and two URLs, and the webhook's was the one that had to be stable and public.
- **The operations screen distinguishes agents from everything else.** All four agents share one chassis and carry a different instrument; the alert, the reviewer and CI are drawn as deliberately different things; and each stage header carries an `AGENT` / `SIGNAL` / `HUMAN` / `CI` tag. A viewer should not have to infer from a silhouette which three of the seven stages the fleet cannot occupy, because that is the whole argument.
- **The stale `warden/*` branches are deleted** from `estate-gitops`.

---

## Cost

One full incident against live Gemini costs roughly two tenths of a cent. A representative run: triage 3,456 tokens, diagnostician 18,136, remediator 13,532 — about $0.0015 in total, on `gemini-3.5-flash` through Vertex.

The asymmetry between agents is the argument for having four narrow ones instead of one large one. Triage costs a fiftieth of what the diagnostician costs and stops most runs before they begin. The diagnostician is expensive because it is the one reading real logs and correlating them against deploy history, which is where the value is.

Budget guards are enforced by the policy proxy before every dispatch: per-agent token ceilings, per-agent tool-call ceilings, and a circuit breaker that opens after three consecutive failures. `infra/gcp/main.tf` sets a five-dollar budget alert on the project, and `/healthz` degrades to HTTP 503 when spend crosses a configured fraction of the cap.

---

## Related repositories

[`estate-gitops`](https://github.com/mazzyy/estate-gitops) is the estate — the fake company's production, written down as files. It holds the service being managed, the namespace that defines the blast radius, both RBAC credentials, the fault injectors, the three verification scripts, and the CI workflow that applies merged changes.

It is a separate repository on purpose. If the two were one, the agent would need write access to its own source code and its own permission files. Keeping them apart means the agent proposes changes to the estate, never to itself — which is also why `rbac/**` is not among the sync workflow's trigger paths. A merged pull request must not be able to widen the permissions of the thing applying it.
