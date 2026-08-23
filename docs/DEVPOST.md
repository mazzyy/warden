# Devpost submission — draft

Category: **Fortified Enterprise Fleet**

Paste-ready. Each heading below maps to a Devpost field. Numbers marked `<>` are
the ones to refresh from your last live run before submitting.

---

## Tagline

*(200 characters max)*

> Four SRE agents that diagnose live Kubernetes failures and land the fix as a pull request. None of them holds a production write credential. Their only write primitive is opening a PR.

---

## Inspiration

Every agent demo we looked at ended at the same place: the agent works out what
is wrong, and then something writes to production. Usually that something is the
agent, holding a credential that can do it.

That is the part nobody wants to deploy. Not because the diagnosis is bad —
models are genuinely good at reading a crash loop and a deploy history and
telling you which line broke it — but because the moment an autonomous process
holds a production write credential, every reasoning failure becomes an outage,
and the blast radius is whatever the credential allows rather than whatever the
agent intended.

So we inverted it. The agents keep the reasoning. The write primitive gets taken
away entirely and replaced with one that a human already knows how to review:

> No agent holds production write credentials. Their only write primitive is
> opening a pull request.

The interesting engineering is not in making that sentence *true in the code*.
It is in making it true *when the code is wrong* — which is the only version
that survives contact with a real estate.

---

## What it does

Warden runs a fleet of four narrow agents over a live Azure Kubernetes cluster.

An alert fires. **Triage** sees only the alert text and the incident history —
it has no cluster access at all — and decides whether this is real, whether it
duplicates an open incident, and whether it is worth waking anyone else. Most
alerts should die here.

**Diagnostician** reads pod specs, container logs, deploy history and metrics
from the live cluster. Read-only, by credential and by contract. It returns one
root cause with the evidence chain behind it.

**Remediator** is the only agent that can change anything, and only one way. It
reads the GitOps repository, writes a patch, and opens a pull request. It is
capped at three files and twelve changed lines, checked twice: once by policy
before the tool runs, and once against the real diff at the write path.

A human reviews and merges. CI — holding a credential no agent has — applies it.

**Verifier** wakes on the merge webhook, reads cluster status and metrics, and
decides whether the workload actually recovered. If it did not, it opens a
*revert* pull request. It can propose the revert. It cannot apply one.

Two fault classes work end to end today: a bad configuration value committed to
the estate, and an OOMKilled workload whose memory limit is below its actual
working set. They are deliberately different shapes — one is a wrong string, one
requires correlating a crash reason against a resource limit and a deploy that
raised the workload's footprint.

There is a live operations screen showing all seven stages, each agent's tool
calls, arguments and results, the token spend, and every policy denial as it
happens.

---

## How we built it

**Google ADK** with **Gemini 3.5 Flash** through **Vertex AI**. The governance
layer is an ADK `BasePlugin` implementing `before_tool_callback`,
`after_tool_callback` and `on_tool_error_callback`.

That plugin is the whole architecture. It sits between the model's tool request
and the tool's execution, in-process, on a path the model cannot route around —
there is no "call the tool directly" escape, because dispatch itself goes
through the callback. Six checks run in order on every single call: kill switch,
tool exists in the catalog, this agent's manifest grants it, the scopes it costs
are held, the budget has room, the blast radius allows it. A denial returns a
non-empty refusal dict, which the model reads and reasons about.

Everything else hangs off that:

- **Four YAML manifests**, one per agent, declaring tools, scopes and budgets.
  Adding a capability to an agent is a configuration change reviewed as a diff,
  not a code change buried in a prompt.
- **A read-only Kubernetes ServiceAccount** (`warden-reader`, get/list/watch),
  with the cluster CA pinned so a diagnosis cannot rest on logs from something
  that merely answered on the right port.
- **A separate namespaced applier ServiceAccount** (`warden-sync`) used only by
  CI, with no `delete` verb at all, and with `rbac/**` deliberately excluded
  from the sync workflow's trigger paths — so a merged pull request cannot
  widen the permissions of the thing applying it.
- **A GitHub App** as the agent's write identity, on which more below.
- **Firestore** for incidents and the audit trail, **Pub/Sub** for alert
  ingress, **Cloud Run** for the service, **Secret Manager** for the webhook
  secret and the App private key, with six service accounts on least-privilege
  bindings — all in Terraform.
- **FastAPI + React** for the operations screen, mounted with the ingest router
  so one service and one URL serve the dashboard, the alert push endpoint and
  the GitHub webhook.

The orchestrator is plain Python rather than an ADK multi-agent delegation tree.
Each agent gets its own run, its own budget and its own audit trail, and the
handoffs are explicit. When the demo is a live break-and-fix, being able to
point at exactly what happened and when is worth more than elegance.

---

## Challenges we ran into

**The credential that inherits its owner.** We set up branch protection on the
estate repository requiring one approving review, and then — as a check we
expected to be boring — tried a direct commit to `main` using the agent's own
fine-grained personal access token.

It returned **HTTP 201**. It landed.

A fine-grained PAT inherits the repository role of the human who minted it.
Narrow scopes decide which repositories and which APIs a token may touch. They
do not decide *who the token is*. Our token was owned by an admin, and admins
bypass protection.

So the central claim of the project was, on that path, true only because our
code never called the merge endpoint. A promise, not a control.

The fix is a GitHub App. An App holds no repository role at all, so branch
protection applies to it with nothing to inherit and no bypass — and an App
cannot approve a pull request even in principle. The same commit now returns
**HTTP 409**. Both numbers are recorded, and `verify-github-token.sh` attempts
the forbidden write on every run rather than reading a config value back.

**The status that was true and wrong.** We injected a bad image tag and the
fleet closed the incident as healthy. Its evidence: `3/3 replicas ready`.

That was accurate. Kubernetes had not taken the old ReplicaSet down, because the
new one never became ready — so `readyReplicas == desiredReplicas` stayed true
for the entire duration of a completely blocked rollout. The number the whole
diagnosis rested on was correct and meant the opposite of what it looked like.
The estate adapter now reads `updatedReplicas` and the `ProgressDeadlineExceeded`
condition, and reports `rollout BLOCKED — 0/3 pods on the new revision; the
previous revision is still serving 3/3`.

**The tool that raised.** `after_tool_callback` does not fire when a tool throws.
Every failed tool call was silently missing from the audit trail — the calls
most worth auditing. Fixed with `on_tool_error_callback`: a tool that raised
still happened.

**The redactor that missed its own test.** Our secret scrubber matched key names
like `password` and `token`, and sailed straight past `x-api-key`. Keys are now
normalised — punctuation and case stripped — before matching.

**The merge that closed the wrong incident.** The webhook originally verified
"the most recent incident". With two incidents in flight that closes the wrong
one and marks a real, unfixed fault as resolved. It now matches on `pr_url`, and
verifies nothing at all if no stored incident carries that URL.

**The webhook that ran the wrong pipeline.** It called the full
`handle_incident(..., verify=True)`, which starts at Triage. On a merge that is
actively wrong: the fleet would open a *fresh* incident for a workload that had
just been fixed and could propose a second pull request for a fault that no
longer existed. The merge is the end of an incident, not the start of one.

Every one of those came from a live run. Not one came from a test.

The exception, and the only bug we caught before it cost us anything: two
different faults sharing one signature. Triage deduplicates incidents on a
signature string, and ours led with the symptom — `checkout-svc/RolloutBlocked`.
But an OOMKilled workload and one crashlooping on a bad configuration value both
stall a rollout. The second fault the fleet ever saw would have matched the
first one's signature, and Triage — correctly, given what it was told — would
have read a brand-new, unrelated incident as a duplicate of one already awaiting
a merge, and stopped before anyone looked at the cluster. The signature now
leads with the container reason and falls back to the symptom only when the
cluster offers no cause. The regression test asserts not just that the two
differ, but that neither is a substring of the other, because the recall tool
matches by substring.

---

## Accomplishments that we're proud of

Eight governance claims, and every one of them has been *observed refusing
something*, against real infrastructure, rather than asserted by reading a
setting back:

| Claim | Proof |
| --- | --- |
| An agent cannot call an ungranted tool | `make probe` — 4 agents × 11 tools, every denial exercised |
| No agent can write to the cluster | attempts a delete, is refused |
| A patch cannot exceed its blast radius | checked in policy, then again against the real diff |
| The logs a diagnosis rests on are real | cluster CA pinned, TLS handshake verified first |
| Only CI can change the estate | 4 verbs allowed, 6 denied |
| The agent cannot merge its own pull request | HTTP 409 |
| The audit trail holds no secrets | recursive redaction, 10 tests |
| A merge closes the incident that opened *that* PR | HMAC verified, then matched on `pr_url` |

The three verification scripts all attempt the forbidden action and are safe to
run on camera.

We are most proud of the rule those scripts came from, which we learned the
embarrassing way. Our first RBAC test ran `kubectl --token=$READER_TOKEN delete
deploy/checkout-svc` and it *worked* — the deployment vanished. Not because RBAC
failed, but because an AKS admin kubeconfig authenticates with client
certificates, and Kubernetes accepts a valid client cert regardless of any
bearer token you also pass. The `--token` flag was silently ignored and the
command ran as cluster-admin.

**A control you have not seen refuse something is not a control.**

Also: one full incident against live Gemini costs about **$0.0015**. Triage
costs a fiftieth of what the diagnostician costs and stops most runs before they
begin — which is the argument for four narrow agents instead of one large one.

---

## What we learned

**Governance is a two-tier problem or it is theatre.** In-process policy is
fast, gives the model a refusal it can reason about, and is worth having. It is
also code, and code has bugs. Every control worth the name is backed by
something outside the process that stays true when our plugin is wrong: RBAC,
branch protection, IAM. Tier one is UX. Tier two is the control.

**Scope is not identity.** The single most transferable thing we found. Every
credential system we touched has a dimension that narrow scoping does not
address — a PAT's owner, a client certificate silently outranking a bearer
token, an admin bypass. Ask what a credential *is*, not only what it may touch.

**Prompts are source code.** Triage refused to escalate a real incident because
its instruction said escalation "wastes an engineer's attention" — a sane cost
model for a human pager, and wrong for a fleet where the next stage is a $0.001
model call. The prompt was the bug. It lives in version control and is reviewed
as a diff.

**Testing an agent is testing its environment.** 123 tests pass, and they caught
real problems. Seventeen serious bugs came from live runs. Not one came from a
test. The gap is never the logic — it is what the world actually returns.

---

## What's next

Deploy to Cloud Run and point the GitHub webhook at it, closing the loop with no
human step between merge and verification. Then more fault classes: failing
readiness probes, resource starvation across a whole node pool, and a
misconfigured HPA. The estate adapter is a protocol with an AKS implementation
behind it, so GKE and EKS are a file each.

The direction we care about most: the policy engine currently answers "may this
agent call this tool?". The version we want answers "may this agent call this
tool *given what it has already done this run*" — treating the sequence, not the
individual call, as the unit of authority.

---

## Built with

`google-adk` · `gemini-3.5-flash` · Vertex AI · Cloud Run · Firestore · Pub/Sub ·
Secret Manager · Cloud Monitoring · Terraform · Azure Kubernetes Service ·
Kubernetes RBAC · GitHub Apps · GitHub Actions · FastAPI · React · Python ·
PyGithub

---

## Try it out

- Code: `github.com/mazzyy/warden`
- The estate it manages: `github.com/mazzyy/estate-gitops`
- Live dashboard: `<cloud-run-url>`

No cloud project, API key or spend is needed to see the governance work:

```bash
git clone https://github.com/mazzyy/warden && cd warden
make setup
make demo     # a full incident, scripted models, free
make probe    # the policy matrix, printed from the live engine
make test     # 123 tests
```

`make probe` is the one to run. It prints the allow/deny matrix from the policy
engine itself, and `make probe -- --assert-no-cluster-writes` is the same check
wired as a CI assertion.
