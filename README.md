# Warden

**A governed fleet of autonomous SRE agents.** Warden watches a live cloud estate, diagnoses failures, and lands the fix as a reviewed GitOps pull request — with every action bounded by a versioned policy manifest, a token budget, and an append-only audit trail.

The design decision the whole system rests on:

> **No agent holds production write credentials. Their only write primitive is opening a pull request.**

Built for the All Things Agentic Hackathon · **Fortified Enterprise Fleet**.

---

## Spin up from a clean clone

Requires Python 3.11+. **No cloud project, no API key, and no spend for any of
this** — the whole system runs offline against a fake estate by design.

```bash
git clone https://github.com/mazzyy/warden.git && cd warden
make install
cp .env.example .env
```

### 1. Watch a real incident get fixed

```bash
make demo
```

Injects a bad config into the fake estate, then runs the whole fleet: triage
escalates, the diagnostician correlates the crashloop to the deploy that caused
it, and the remediator writes a pull request. Prints every tool call with its
allow/deny decision, latency, token count and estimated cost. Takes two seconds.

```bash
make demo-oom      # the other failure mode
make demo-live     # the same run against real Gemini (this one costs tokens)
```

### 2. See the policy matrix

```bash
make probe
```

Every agent against every tool in the catalog, allow or deny, with `--explain`
showing the reason for each refusal. This is the honest way to demonstrate the
governance layer — you cannot reliably get a well-prompted model to attempt a
tool it has been told it does not have, so asking the policy engine directly is
both more convincing and what an operator would actually want after editing a
manifest.

### 3. Run the gate

```bash
make check      # lint + 24 tests + the security assertion
```

The suite includes a full ADK agent run in which an agent attempts a tool it was
not granted and the policy plugin stops the tool function from ever being
entered — `tests/test_proxy_blocks.py`. `make check` also runs
`warden.probe --assert-no-cluster-writes`, which turns *no agent can write to
the cluster* from a claim in this README into a build failure if it ever stops
being true. All three run in CI on every push.

### 4. The real thing

`docs/SETUP.md` has verified, copy-pasteable commands for both clouds. Then set
`ESTATE_ADAPTER=aks` in `.env`.

---

## Architecture

```
Cloud Monitoring ─┐
Cloud Scheduler   ├─→ Pub/Sub ─→ Triage ─→ Diagnostician ─→ Remediator ─→ Verifier
GitHub webhooks  ─┘                  │          │              │             │
                                     └──────────┴──────────────┴─────────────┘
                                                      ↓
                                        Policy Tool Proxy   ← every tool call
                                                      ↓
                                    Estate (read)  ·  GitOps repo (write PR)
```

| Agent | Model | Can do | Cannot do |
|---|---|---|---|
| **Triage** | `gemini-3.5-flash` | Dedupe, score severity, escalate | Touch the estate |
| **Diagnostician** | `gemini-3.1-pro` | Read logs, specs, deploys, metrics | Write anything, anywhere |
| **Remediator** | `gemini-3.1-pro` | Open a pull request | Reach the cluster; merge its own PR |
| **Verifier** | `gemini-3.5-flash` | Watch SLOs, propose a revert | Apply a revert |

### Governance is enforced in two independent layers

**Tier 1 — the policy proxy, in-process.** `warden/proxy/plugin.py` is an ADK
Plugin. ADK's plugin hooks fire for every tool of every agent in the tree and run
*before* per-agent callbacks, so there is no path around them from inside the
process. Returning a non-empty dict from `before_tool_callback` prevents the tool
function from being entered at all. Every call — allowed or denied — is written
to the audit log with a reason.

**Tier 2 — IAM, out-of-process.** Agent service accounts do not hold
`roles/secretmanager.secretAccessor`. Only `sa-proxy` can read the estate token
or the GitHub token, and only `sa-proxy` holds `roles/datastore.user`. So even a
hypothetically bypassed policy check leaves an agent unable to obtain a
credential — or to erase its own audit trail.

Policy denies it in-process. IAM makes it impossible out-of-process.

### Agents are declared, not coded

Each agent is a manifest in `manifests/agents/`, versioned in git and loaded into
a Firestore registry. It is GitOps for agents:

```yaml
apiVersion: warden.dev/v1
kind: Agent
metadata: { name: remediator }
spec:
  model: gemini-3.1-pro
  tools: [read_repo_file, propose_patch]
  scopes: [repo:estate-gitops:read, repo:estate-gitops:write-pr]
  blastRadius: { namespace: demo, maxFilesPerPatch: 3 }
  budget: { maxTokensPerRun: 120000, maxToolCalls: 20 }
  approval: required
```

A manifest granting a tool the catalog has never heard of fails at load with the
offending name, rather than surfacing as a mysterious denial three days later.

### The denial is real

`delete_workload` is deliberately registered in `warden/tools/catalog.py` and
deliberately granted to nobody. It exists so the agent can *genuinely* attempt the
call and policy can *genuinely* stop it — a denial demo against a tool that
doesn't exist would prove nothing. See `tests/test_proxy_blocks.py`.

---

## Layout

```
warden/
├── manifests/agents/      the registry's source of truth
├── warden/
│   ├── control_plane/     registry · policy · budget · audit · store
│   ├── proxy/             the ADK policy plugin ← the choke point
│   ├── estate/            EstateAdapter · AksAdapter · FakeAdapter
│   ├── tools/             catalog + implementations, reachable only via the proxy
│   ├── agents/            ADK agent definitions
│   └── dashboard/         FastAPI + React
├── infra/{gcp,azure}/     Terraform
├── scripts/               failure injection
└── tests/
```

`estate-gitops` is a **separate repository** — it is what the Remediator opens
pull requests against, and its token is scoped to that repo alone. That
separation is what makes the credential claim provable rather than asserted.

---

## Notes for anyone reading the code

Three things about ADK 2.x that cost time to discover, documented here so they
don't cost you any:

- **`gemini-3.5-pro` does not exist.** The Flash and Pro lines sit on different
  version numbers: `gemini-3.5-flash` and `gemini-3.1-pro`. ADK never validates
  the model ID, so a typo fails at the API call rather than at construction.
- **`auto_create_session` defaults to `False`.** Create the session before
  calling `run_async` or it raises.
- **`usage_metadata` is emitted per model call, not per turn.** Sum it. Reading
  the last event undercounts a tool-using turn several-fold.

Plugin callbacks are keyword-only and use `tool_args` / `result`; the per-agent
callbacks use `args` / `tool_response`. Getting the names wrong means the hook
silently never fires.

## Licence

Apache-2.0.
