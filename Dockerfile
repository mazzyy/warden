# One image, one Cloud Run service, one origin. The FastAPI app serves four
# things that used to want to be separate deployments:
#
#   GET  /             the built React SPA
#   GET  /api/*        the operations screen's data
#   POST /pubsub       an alert fired    -> triage, diagnose, propose a PR
#   POST /webhook/github  a human merged -> verify, or open a revert
#
# The last two arrive from `warden/ingest.py`, mounted as a router. Splitting
# them into a second service would mean a second URL, and the webhook URL is the
# one that has to be stable and public — which is exactly what a laptop tunnel
# is worst at providing. Cloud Run gives it for free.
#
#   gcloud run deploy warden --source . --region europe-west3 \
#     --service-account sa-dashboard@$PROJECT.iam.gserviceaccount.com \
#     --set-env-vars WARDEN_STORE=firestore,GOOGLE_GENAI_USE_ENTERPRISE=1 \
#     --set-secrets GITHUB_WEBHOOK_SECRET=warden-webhook-secret:latest,\
# GITHUB_APP_PRIVATE_KEY=warden-github-app-key:latest \
#     --allow-unauthenticated
#
# Then point the GitHub webhook at https://<service-url>/webhook/github and
# check https://<service-url>/healthz — it reports `webhook: armed` only when
# GITHUB_WEBHOOK_SECRET actually reached the container. Unauthenticated is
# correct here: GitHub signs its deliveries and the endpoint verifies the HMAC
# itself, so IAM would only lock out the caller that is supposed to reach it.
#
# `--allow-unauthenticated` does leave /api/* and the SPA public. For a demo
# estate that is the intended trade; for anything real, split the ingest router
# onto its own unauthenticated service and put IAP in front of the dashboard.

# ---- stage 1: build the SPA -------------------------------------------------
# The build runs here rather than being a step you have to remember, because
# the step you have to remember is the one you forget at 1am on the 29th.
FROM node:22-slim AS web

WORKDIR /web
COPY warden/dashboard/web/package*.json ./
RUN npm ci --no-audit --no-fund
COPY warden/dashboard/web/ ./
RUN npm run build

# ---- stage 2: the app -------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app
RUN adduser --disabled-password --gecos "" warden

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY warden/ ./warden/
COPY manifests/ ./manifests/
COPY --from=web /web/dist ./warden/dashboard/web/dist

# COPY preserves the source file's mode from the build context, so a file that
# is not world-readable on the machine that ran `docker build` arrives in the
# image unreadable by the unprivileged user below. The failure is a
# PermissionError on an import, deep in a traceback, on whichever module
# happened to be restrictive — here it was control_plane/budget.py, and only
# on one developer's laptop.
#
# a+rX, not a+r: capital X sets the execute bit on directories only, so
# directories stay traversable and .py files do not become executable.
RUN chown -R warden:warden /app && chmod -R a+rX /app

USER warden

# Cloud Run injects $PORT (8080 by default). Read it rather than hardcoding —
# ADK's own default is 8000, so a hardcoded port silently fails to receive
# traffic here.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn warden.dashboard.api:app --host 0.0.0.0 --port ${PORT} --workers 1
