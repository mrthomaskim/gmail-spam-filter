#!/usr/bin/env bash
# Deploys the Gmail spam filter stack. Safe to re-run (e.g. after editing KEYWORDS).
#   PROJECT_ID=my-proj SHEET_ID=1AbC...xyz ./deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID}"
REGION="${REGION:-us-central1}"
SHEET_ID="${SHEET_ID:-}"          # optional: Google Sheet to log moved mail
TOPIC="gmail-push"
SECRET="gmail-oauth-token"
SA_NAME="gmail-filter"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
TOPIC_PATH="projects/${PROJECT_ID}/topics/${TOPIC}"

cd "$(dirname "$0")"
gcloud config set project "$PROJECT_ID" >/dev/null

echo "==> Enabling APIs"
gcloud services enable \
  gmail.googleapis.com sheets.googleapis.com pubsub.googleapis.com \
  cloudfunctions.googleapis.com run.googleapis.com eventarc.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com

echo "==> Service account"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name="Gmail spam filter"

echo "==> Pub/Sub topic (Gmail must be allowed to publish)"
gcloud pubsub topics describe "$TOPIC" >/dev/null 2>&1 || gcloud pubsub topics create "$TOPIC"
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher" >/dev/null

echo "==> Secret"
gcloud secrets describe "$SECRET" >/dev/null 2>&1 || \
  gcloud secrets create "$SECRET" --replication-policy=automatic
if [[ -f token.json ]]; then
  gcloud secrets versions add "$SECRET" --data-file=token.json
  echo "    Uploaded token.json — delete the local copy when deploy finishes."
elif ! gcloud secrets versions list "$SECRET" --filter="state=ENABLED" --format="value(name)" | grep -q .; then
  echo "ERROR: no token.json and no secret version. Run get_token.py first." >&2; exit 1
fi
gcloud secrets add-iam-policy-binding "$SECRET" \
  --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor" >/dev/null

COMMON=(--gen2 --region="$REGION" --runtime=python312 --source=.
        --service-account="$SA"
        --set-secrets="TOKEN_JSON=${SECRET}:latest"
        --set-env-vars="SHEET_ID=${SHEET_ID},TOPIC=${TOPIC_PATH}"
        --memory=256Mi)

echo "==> Deploying push handler"
gcloud functions deploy gmail-filter "${COMMON[@]}" \
  --entry-point=on_gmail_push --trigger-topic="$TOPIC" \
  --trigger-service-account="$SA" --timeout=120s --max-instances=3

echo "==> Deploying manage endpoint"
gcloud functions deploy gmail-filter-manage "${COMMON[@]}" \
  --entry-point=manage --trigger-http --no-allow-unauthenticated \
  --timeout=540s --max-instances=1

echo "==> Invoker permissions"
for svc in gmail-filter gmail-filter-manage; do
  gcloud run services add-iam-policy-binding "$svc" --region="$REGION" \
    --member="serviceAccount:${SA}" --role="roles/run.invoker" >/dev/null
done

URL="$(gcloud functions describe gmail-filter-manage --gen2 --region="$REGION" \
  --format='value(serviceConfig.uri)')"

echo "==> Daily watch renewal (Cloud Scheduler)"
gcloud scheduler jobs delete renew-gmail-watch --location="$REGION" --quiet >/dev/null 2>&1 || true
gcloud scheduler jobs create http renew-gmail-watch --location="$REGION" \
  --schedule="17 6 * * *" --time-zone="America/Chicago" \
  --uri="${URL}?action=renew" --http-method=GET \
  --oidc-service-account-email="$SA" --oidc-token-audience="$URL"

echo "==> Starting the Gmail watch now"
curl -fsS -H "Authorization: Bearer $(gcloud auth print-identity-token)" "${URL}?action=renew"
echo
echo "Done. Manage URL: ${URL}"
