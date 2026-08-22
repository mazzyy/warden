# Dashboard

The judges' hosted URL. One FastAPI process serves both `/api` and the built
React SPA, so it is one Cloud Run service with no CORS and nothing extra to
break on the day you record.

## Run it locally

```bash
cd warden/dashboard/web && npm install && npm run build && cd -
./.venv/bin/python -m uvicorn warden.dashboard.api:app --port 8080
```

Open <http://localhost:8080>. It starts on an in-memory store seeded with three
incidents, so there is something to look at immediately — no cloud required.

For SPA hot-reload while editing, run uvicorn as above and `npm run dev` in
another terminal; Vite proxies `/api` and `/healthz` through to port 8080.

## The three views

**Fleet** — every agent with the manifest it was loaded from: model, tools,
scopes, blast radius, budget, and what it has actually spent. Write scopes are
tinted amber, so "the diagnostician holds none" is visible rather than asserted.

**Incidents** — click one for the full timeline. Every tool call, in order, with
its latency and its allow/deny decision. **A denial renders as a red block with
the policy engine's own reason text**, which is why those reason strings are
written as user-facing copy rather than debug output. This is the screen to open
during the video.

**Policy matrix** — every agent against every tool in the catalog, live from the
policy engine, ending in the verdict: *no agent in the fleet can write to the
cluster*. It is the same evaluation `warden.probe` runs in CI.

The **kill switch** is top-right. Engaging it sets `fleet/state.killSwitch`, and
the proxy refuses every dispatch fleet-wide while it is set — worth doing on
camera mid-incident.

## Health

`GET /healthz` returns 200 healthy / 503 degraded, and every check is a **live
probe rather than a parsed expiry date**. Probing catches revocation, rotation,
quota exhaustion and expiry at once; reading an expiry date catches only the
last of those.

| check | probes | critical |
|---|---|---|
| `store` | reads `fleet/state` | yes |
| `budget` | sums real `usage_metadata` against the cap | yes |
| `credentials` | reports Vertex vs Gemini API | no |
| `estate` | which adapter is active | no |

Put a Cloud Monitoring uptime check on this endpoint (`docs/SETUP.md` §11). It
then does double duty: it emails you when a credential dies, *and* when the
hosted URL goes down during the **Sept 1 – Oct 1 judging window** — the failure
most likely to quietly cost you a prize, precisely because you will not be
watching.

## Storage

Defaults to in-memory with seed data, which is what makes it runnable in ten
seconds from a clean clone. Set `WARDEN_STORE=firestore` for the real thing.
