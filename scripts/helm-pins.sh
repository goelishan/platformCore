#!/usr/bin/env bash
#--------------------------------------------------------------------------------------------------------
# PINNED VERSUS UPSTREAM
#--------------------------------------------------------------------------------------------------------
#
#   - reads the resolved versions out of the bootstrap chart's Chart.lock
#
#   - asks each upstream repository for its newest published version
#
#   - prints one line per chart, marking the ones upstream has moved past
#
# Makes "is anything behind?" a command instead of a memory. Nothing here changes a
# pin: a bump is an edit to Chart.yaml followed by `make helm-relock`, which lands
# in review as a Chart.lock diff.

set -euo pipefail

CHART_DIR="${1:-charts/platform-bootstrap}"
LOCK="$CHART_DIR/Chart.lock"

# The lock rather than Chart.yaml, because the lock records what was actually
# resolved and installed. When the two disagree someone edited a version without
# relocking, and that is worth seeing here rather than discovering at bootstrap.
if [ ! -f "$LOCK" ]; then
  echo "no $LOCK - run: make helm-relock" >&2
  exit 1
fi

printf '%-34s %-12s %-12s\n' CHART PINNED LATEST

behind=0
while IFS="$(printf '\t')" read -r name repo version; do
  # `helm show chart --repo` reads the repository index over HTTP without
  # registering the repo, so this reports on a laptop that has never run
  # `helm repo add` and leaves no helm state behind either way.
  latest="$(helm show chart --repo "$repo" "$name" 2>/dev/null | awk '$1 == "version:" { print $2; exit }')"
  latest="${latest:-unreachable}"

  if [ "$latest" = "$version" ]; then
    printf '%-34s %-12s %-12s\n' "$name" "$version" "$latest"
  else
    behind=$((behind + 1))
    printf '%-34s %-12s %-12s  <- upstream moved\n' "$name" "$version" "$latest"
  fi
done < <(awk '
  $1 == "-" && $2 == "name:" { name = $3 }
  $1 == "repository:"        { repo = $2 }
  $1 == "version:"           { print name "\t" repo "\t" $2 }
' "$LOCK")

echo
if [ "$behind" -eq 0 ]; then
  echo "every pin matches upstream."
else
  echo "$behind chart(s) behind upstream. Bump in $CHART_DIR/Chart.yaml, then: make helm-relock"
fi
