#!/usr/bin/env bash
#--------------------------------------------------------------------------------------------------------
# PREDESTROY - EMPTY THE CLUSTER OF EVERYTHING THAT OWNS AN AWS RESOURCE, BEFORE TERRAFORM DESTROY
#--------------------------------------------------------------------------------------------------------
#
# Kubernetes objects own AWS resources Terraform never sees: an Ingress owns an ALB, a PVC
# owns an EBS volume. They can only be released while the cluster is up. make down-all
# skips its cleanup when the cluster is unreachable; this script refuses instead, because
# a teardown that carries on without it leaves orphans that block the VPC delete (seen
# 2026-10-13, U1 in the drill log).
#
#   scripts/predestroy.sh && make down-all && scripts/sweep-orphans.sh --delete
#
# What it removes, in order:
#   1. the oncall release and its PVC (the PVC is helm.sh/resource-policy: keep, so an
#      uninstall alone leaves it and its volume)
#   2. every Argo CD Application (they carry no finalizer, so their resources stay; step 3
#      removes the ones that matter)
#   3. every Ingress in the cluster, then waits until the controller has deleted their ALBs
#
# The kube-prometheus-stack volumes are gp3-retain: deleting their PVCs leaves the EBS
# volumes by design. make down-all deletes the PVCs; sweep-orphans.sh --delete removes
# the volumes once nothing holds them.
#
# Written for the bash 3.2 that macOS ships.
#

set -euo pipefail

CLUSTER="${CLUSTER:-platformcore}"
REGION="${AWS_REGION:-us-east-1}"
ALB_WAIT_SECONDS="${ALB_WAIT_SECONDS:-300}"

for tool in aws kubectl helm jq; do
  command -v "$tool" >/dev/null || { echo "$tool not found" >&2; exit 2; }
done

fail() {
  echo "predestroy: $*" >&2
  echo "predestroy: do not run make down-all yet. If the cluster is already gone, run it and then scripts/sweep-orphans.sh --delete." >&2
  exit 1
}



#--------------------------------------------------------------------------------------------------------
# THE CLUSTER MUST ANSWER
#--------------------------------------------------------------------------------------------------------

aws eks describe-cluster --name "$CLUSTER" --region "$REGION" --no-cli-pager >/dev/null 2>&1 \
  || fail "cluster $CLUSTER not found in $REGION"
aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" --no-cli-pager >/dev/null \
  || fail "could not write kubeconfig for $CLUSTER"
kubectl get --raw /readyz >/dev/null 2>&1 \
  || fail "the API server of $CLUSTER does not answer"
echo "==> cluster $CLUSTER reachable ($(kubectl config current-context))"



#--------------------------------------------------------------------------------------------------------
# 1. ONCALL AND ITS VOLUME
#--------------------------------------------------------------------------------------------------------

if helm status oncall -n oncall >/dev/null 2>&1; then
  echo "==> uninstalling oncall"
  helm uninstall oncall -n oncall --wait --timeout 5m
fi
if kubectl get namespace oncall >/dev/null 2>&1; then
  echo "==> deleting PVCs in oncall"
  kubectl -n oncall delete pvc --all --wait=true --timeout=5m
fi



#--------------------------------------------------------------------------------------------------------
# 2. ARGO CD APPLICATIONS
#--------------------------------------------------------------------------------------------------------
#
# Deleted first so self-heal cannot recreate an Ingress removed in step 3.

if kubectl get crd applications.argoproj.io >/dev/null 2>&1; then
  echo "==> deleting Argo CD Applications"
  kubectl delete applications.argoproj.io --all -A --wait=true --timeout=2m
fi



#--------------------------------------------------------------------------------------------------------
# 3. INGRESSES, THEN WAIT FOR THEIR ALBS
#--------------------------------------------------------------------------------------------------------

echo "==> deleting every Ingress"
kubectl delete ingress --all -A --wait=true --timeout=5m

cluster_albs() {
  local arns
  arns=$(aws elbv2 describe-load-balancers --region "$REGION" --no-cli-pager --output json \
           | jq -r '.LoadBalancers[].LoadBalancerArn')
  [ -n "$arns" ] || return 0
  echo "$arns" | xargs -n 20 aws --region "$REGION" --no-cli-pager --output json \
      elbv2 describe-tags --resource-arns \
    | jq -r --arg c "$CLUSTER" \
        '.TagDescriptions[] | select(any(.Tags[]; .Key == "elbv2.k8s.aws/cluster" and .Value == $c)) | .ResourceArn'
}

waited=0
while :; do
  left=$(cluster_albs)
  [ -z "$left" ] && break
  if [ "$waited" -ge "$ALB_WAIT_SECONDS" ]; then
    fail "ALBs still present after ${ALB_WAIT_SECONDS}s: $(echo "$left" | tr '\n' ' ')"
  fi
  echo "    waiting for the controller to delete: $(echo "$left" | awk -F/ '{print $(NF-1)}' | tr '\n' ' ')"
  sleep 15
  waited=$((waited + 15))
done

echo "==> predestroy complete: no oncall release, no Applications, no Ingresses, no cluster ALBs"
echo "    next: make down-all && scripts/sweep-orphans.sh --delete"
