#!/usr/bin/env bash
#--------------------------------------------------------------------------------------------------------
# CHART ASSERTIONS
#--------------------------------------------------------------------------------------------------------
#
#   - renders the application chart and checks the properties its templates promise
#
#   - probe wiring, probe separation, image pinning, and the two tuning invariants
#
#   - runs identically from a laptop and from CI, with no cluster
#
# The unit tests cover app/main.py and cannot see the chart, so the defect that
# started this work, both probes pointing at the same endpoint, was invisible to
# them. These are the assertions that would have caught it, plus the invariants that
# only existed as sentences in comments until now.

set -euo pipefail

CHART="${1:-charts/platformcore}"
KUBE_VERSION="${KUBE_VERSION:-1.33.0}"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { printf '  ok  %s\n' "$*"; }

render() { helm template "$CHART" --kube-version "$KUBE_VERSION" "$@"; }

echo "asserting $CHART"


#--------------------------------------------------------------------------------------------------------
# PROBES
#--------------------------------------------------------------------------------------------------------
#
# Readiness must speak for the database and liveness must not. They also must not
# share tuning: a single set of numbers cannot be both quick to pull a Pod from
# rotation and slow to restart a process.


probes="$(render --show-only templates/fastapi/deployment.yaml | awk '
  /readinessProbe:/  { p = "readiness" }
  /livenessProbe:/   { p = "liveness"  }
  p && /path:/             && !seen[p "path"]++    { print p ".path "             $2 }
  p && /periodSeconds:/    && !seen[p "period"]++  { print p ".periodSeconds "    $2 }
  p && /timeoutSeconds:/   && !seen[p "timeout"]++ { print p ".timeoutSeconds "   $2 }
  p && /failureThreshold:/ && !seen[p "fail"]++    { print p ".failureThreshold " $2 }
')"

get() { echo "$probes" | awk -v k="$1" '$1 == k { print $2; found = 1 } END { if (!found) exit 1 }'; }

[ "$(get readiness.path)" = "/ready" ]  || fail "readiness probe is on $(get readiness.path), not /ready"
[ "$(get liveness.path)"  = "/health" ] || fail "liveness probe is on $(get liveness.path), not /health"
pass "readiness on /ready, liveness on /health"

[ "$(get readiness.periodSeconds)" != "$(get liveness.periodSeconds)" ] ||
  fail "both probes share periodSeconds; they are tuned for different costs and must differ"
[ "$(get readiness.failureThreshold)" -lt "$(get liveness.failureThreshold)" ] ||
  fail "readiness must fail faster than liveness: one costs a Pod its endpoint, the other costs a restart"
pass "probes tuned apart (readiness $(get readiness.periodSeconds)s/$(get readiness.failureThreshold), liveness $(get liveness.periodSeconds)s/$(get liveness.failureThreshold))"


#--------------------------------------------------------------------------------------------------------
# TUNING INVARIANTS
#--------------------------------------------------------------------------------------------------------
#
# Two numbers the app reads from the ConfigMap are only correct relative to the probe
# they are tuned against. Both were prose in a comment until this check existed.


configmap="$(render --show-only templates/fastapi/configmap.yaml)"

# Guarded assignment: under `set -e` an unguarded one would take the exit status of
# its own command substitution and end the run before the message below.
cfg() {
  echo "$configmap" | awk -v k="$1" '
    $0 ~ "^[[:space:]]*" k ":" { v = $2; gsub(/"/, "", v); print v; found = 1; exit }
    END { if (!found) exit 1 }
  '
}

ttl="$(cfg READY_CACHE_TTL_SECONDS)"         || fail "the fastapi ConfigMap has no READY_CACHE_TTL_SECONDS"
timeout="$(cfg RDS_CONNECT_TIMEOUT_SECONDS)" || fail "the fastapi ConfigMap has no RDS_CONNECT_TIMEOUT_SECONDS"

# A connect that outlives the probe's own timeout is pure cost: the kubelet has
# already given up, and the handler still holds a threadpool worker.
[ "$timeout" -le "$(get readiness.timeoutSeconds)" ] ||
  fail "RDS_CONNECT_TIMEOUT_SECONDS ($timeout) exceeds the readiness timeout ($(get readiness.timeoutSeconds))"
pass "connect timeout ${timeout}s within the readiness timeout $(get readiness.timeoutSeconds)s"

# A cached success can hide an outage for one TTL. Keeping it inside the probe's own
# failure budget keeps worst-case detection at roughly twice that budget.
budget=$(( $(get readiness.periodSeconds) * $(get readiness.failureThreshold) ))
[ "$ttl" -le "$budget" ] ||
  fail "READY_CACHE_TTL_SECONDS ($ttl) exceeds the probe's failure budget (${budget}s)"
pass "readiness cache ${ttl}s within the ${budget}s failure budget"


#--------------------------------------------------------------------------------------------------------
# IMAGE PINNING
#--------------------------------------------------------------------------------------------------------
#
# Upstream images carry a digest. The application image does not and must not: CI
# writes the commit SHA into its tag and the ECR repository is immutable, so the
# commit is its pin.


while read -r image; do
  case "$image" in
    *dkr.ecr.*/platformcore-app:*) pass "application image pinned by commit SHA" ;;
    *@sha256:*)                    pass "digest pinned: ${image%%@*}" ;;
    *)                             fail "image without a digest: $image" ;;
  esac
done < <(render | awk '$1 == "image:" { print $2 }')

echo "all chart assertions passed"
