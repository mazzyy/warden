# Closing the loop automatically

Right now, merging a pull request does nothing. You run `--verify-only` by hand
and the verifier wakes up. This document is how to make the merge itself wake it.

## Why it does not already work

Three separate reasons, and only one of them is a missing setting.

**Nothing is listening.** `make demo-live` is a one-shot process that exits when
the incident reaches `awaiting_merge`. The merge happens minutes or hours later,
against a process that no longer exists. `warden/server.py` is the long-running
half, and it has to actually be running.

**GitHub cannot reach you.** A webhook is GitHub making an HTTP request to a
public URL. `localhost:8081` is not one.

**The endpoint used to run the wrong thing.** This is worth knowing because it
was a real bug rather than a missing config. `/webhook/github` called
`handle_incident(..., verify=True)`, which starts at Triage. On a merge that is
actively wrong: the fleet would open a *fresh* incident for a workload that was
just fixed, re-diagnose a healthy service, and could propose a second pull
request for a fault that no longer exists. The merge is the end of an incident,
not the start of one. It now calls `verify_merged_incident`, which runs the
Verifier alone against the incident that opened that specific pull request.

## What the endpoint does now

```
POST /webhook/github
  |
  +-- no GITHUB_WEBHOOK_SECRET set?      -> 503, refuse to serve
  +-- signature missing or wrong?        -> 401
  +-- event is "ping"?                   -> 200 pong
  +-- not a closed pull_request?         -> ignored
  +-- closed without merging?            -> ignored (the reviewer said no)
  +-- head branch not warden/*?          -> ignored (a human's own PR)
  |
  +-- match pr_url to an open incident   -> run the Verifier, close or revert
```

It fails closed. With no secret configured the endpoint returns 503 rather than
accepting deliveries, because this endpoint starts agent runs — an
unauthenticated one is a remote trigger for the fleet available to anyone who
learns the URL.

The pull request URL is matched back to the incident that opened it. Verifying
"the most recent incident" is only correct when exactly one is in flight; with
two open, a merge would close the wrong one and mark a real, unfixed fault as
resolved.

## Setting it up locally

**1. Pick a secret.**

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Put it in `warden/.env` as `GITHUB_WEBHOOK_SECRET=...`. You will paste the same
value into GitHub in step 4.

**2. Run the server.**

```bash
cd warden
WARDEN_STORE=jsonl ./.venv/bin/uvicorn warden.server:app --port 8081
```

`WARDEN_STORE=jsonl` matters. Without it the server defaults to Firestore, and
with `memory` it would keep its own private state — either way the dashboard
and `make demo-live` would be looking at a different set of incidents than the
webhook is closing. Check it came up armed:

```bash
curl -s localhost:8081/healthz | python3 -m json.tool
```

You want `"webhook": "armed"`. If it says `NOT CONFIGURED`, the secret did not
load.

**3. Give GitHub a public URL.**

```bash
cloudflared tunnel --url http://localhost:8081
```

or

```bash
ngrok http 8081
```

Either prints an HTTPS URL. This is a development convenience — deployed, the
server runs on Cloud Run and has a real one.

**4. Add the webhook.**

On `estate-gitops`: Settings, Webhooks, Add webhook.

| Field | Value |
| --- | --- |
| Payload URL | `https://your-tunnel-url/webhook/github` |
| Content type | `application/json` |
| Secret | the value from step 1 |
| Events | Let me select individual events, then **Pull requests** only |

Or from the command line:

```bash
gh api -X POST repos/mazzyy/estate-gitops/hooks --input - <<JSON
{
  "name": "web",
  "active": true,
  "events": ["pull_request"],
  "config": {
    "url": "https://your-tunnel-url/webhook/github",
    "content_type": "json",
    "secret": "$GITHUB_WEBHOOK_SECRET",
    "insecure_ssl": "0"
  }
}
JSON
```

GitHub sends a `ping` immediately. A green tick in the Recent Deliveries tab
means the signature checked out.

**5. Watch it work.**

```bash
cd ../estate-gitops
./scripts/inject.sh bad-config
sleep 45

cd ../warden
make demo-live
```

Review and merge the pull request. Within a second or two the server logs:

```
PR #7 merged (warden/fix-...) — verifying
INC-... -> resolved after merge (4,210 tokens)
```

and the dashboard's last two columns go green on their own. You did not run
anything.

## Debugging a delivery that did not land

GitHub keeps every delivery under Settings, Webhooks, Recent Deliveries, with
the full request and response and a **Redeliver** button. That button is the
most useful debugging tool here — you can replay a real merge as many times as
you like without merging anything again.

| What you see | What it means |
| --- | --- |
| 503 `webhook secret not configured` | `GITHUB_WEBHOOK_SECRET` is not in `.env`, or the server started before you added it |
| 401 `bad signature` | The secret in GitHub does not match the one in `.env` |
| 200 `ignored` | The event was not a merged `warden/*` pull request. Check `head.ref` in the payload |
| 200 `accepted`, nothing happens | Look at the server log. `nothing to verify for <url>` means no stored incident carries that `pr_url` — usually because the incident was created in a different store |
| Nothing at all in Recent Deliveries | The tunnel is down, or the URL is wrong |

## Deployed

On Cloud Run the tunnel disappears and the payload URL becomes the service URL.
Two things change:

The webhook secret comes from Secret Manager rather than `.env`, mounted as
`GITHUB_WEBHOOK_SECRET` on the service. Only `sa-runtime` holds
`roles/secretmanager.secretAccessor` for it.

`WARDEN_STORE` is left unset so it defaults to Firestore, which is what lets an
incident survive a cold start — a Cloud Run instance that scales to zero between
the pull request and the merge would otherwise lose the incident entirely, and
the webhook would arrive with nothing to verify.
