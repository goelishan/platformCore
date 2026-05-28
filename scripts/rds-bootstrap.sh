#!/usr/bin/env bash
# Idempotent bootstrap for the RDS IAM auth user.
#   - Creates `fastapi` login role if it doesn't exist (DO $$ guard)
#   - Grants rds_iam so FastAPI can authenticate via generate_db_auth_token
# Safe to run on every make up — no-op if user already present.
set -euo pipefail

echo "  Fetching RDS host from terraform output..."
RDS_HOST=$(cd terraform && terraform output -raw rds_host)

echo "  Fetching master password from Secrets Manager..."
SECRET_JSON=$(aws secretsmanager get-secret-value \
  --secret-id platformcore/db/master \
  --region us-east-1 \
  --query SecretString \
  --output text)
MASTER_PASS=$(echo "$SECRET_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")

echo "  Launching rds-bootstrap pod..."
# SQL is passed via env var so no shell layer re-processes the dollar-quote
# delimiters. $body$ is used instead of $$ — identical to PostgreSQL but
# carries no special meaning in bash, kubectl, or any intermediate layer.
SQL="DO \$body\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fastapi') THEN CREATE USER fastapi WITH LOGIN; END IF; END \$body\$; GRANT rds_iam TO fastapi;"

kubectl run rds-bootstrap \
  --image=postgres:16 \
  --restart=Never \
  --env="PGPASSWORD=${MASTER_PASS}" \
  --env="RDS_HOST=${RDS_HOST}" \
  --env="BOOTSTRAP_SQL=${SQL}" \
  -- sh -c 'psql -h "$RDS_HOST" -U platformcore -d platformcore -c "$BOOTSTRAP_SQL"'

echo "  Waiting for pod to complete..."
while [[ $(kubectl get pod rds-bootstrap -o jsonpath='{.status.phase}') =~ ^(Pending|Running)$ ]]; do
  sleep 2
done

kubectl logs rds-bootstrap

PHASE=$(kubectl get pod rds-bootstrap -o jsonpath='{.status.phase}')
kubectl delete pod rds-bootstrap --ignore-not-found >/dev/null

if [[ "$PHASE" != "Succeeded" ]]; then
  echo "ERROR: rds-bootstrap pod failed (phase: $PHASE)"
  exit 1
fi

echo "  RDS IAM auth user ready."
