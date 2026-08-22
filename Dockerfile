# One image, one Cloud Run service: the FastAPI app serves both /api and the
# built React SPA from the same origin. No CORS, no second service, nothing
# extra to break on the day you record.
#
#   gcloud run deploy warden-dashboard --source . --region europe-west3 \
#     --service-account sa-dashboard@$PROJECT.iam.gserviceaccount.com \
#     --set-env-vars WARDEN_STORE=firestore,GOOGLE_GENAI_USE_ENTERPRISE=1 \
#     --allow-unauthenticated

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

USER warden

# Cloud Run injects $PORT (8080 by default). Read it rather than hardcoding —
# ADK's own default is 8000, so a hardcoded port silently fails to receive
# traffic here.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn warden.dashboard.api:app --host 0.0.0.0 --port ${PORT} --workers 1
