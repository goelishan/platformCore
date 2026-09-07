"""
What is actually in the buffer.

  - Reads, never writes. Safe to run against a buffer mid-collection.
  - Grouped by source then subject, because that is how a corpus defect presents:
    a subject the cluster shows and the buffer does not, or a subject with three
    fingerprints where there is one problem.
  - Prints the collection_runs table first. A source that reported unavailable and a
    source that found nothing look identical in the signals table, and the whole
    three-state status exists so they do not have to.

    make -C oncall peek
    make -C oncall peek ARGS="--source k8s_pods --full"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

from oncall import config
from oncall import landing_zone as lz
from oncall.envelope import PROVENANCE_KEY

# Payload keys worth putting on the summary line, in the order a reader wants them.
# Everything else needs --full, because a summary that prints every key is a dump.
HEADLINE = (
    "reason", "phase", "exit_code", "container", "restart_count", "ready",
    "condition", "status", "endpoint_total", "endpoint_ready", "count",
    # k8s_logs. Without these a log row printed its container name and nothing else,
    # so five log signals were indistinguishable from five empty ones and the question
    # "does this source carry any evidence" could not be answered from the output.
    "stream", "log_status", "lines_seen", "lines_kept",
)

# Machinery the reasoner never sees and an auditor always needs. severity_basis says
# which scale the severity column is on for this row; owner_resolution keeps three
# different absences from reading alike, and collapsing them to "owner=-" hid the
# distinction the collector works hardest to preserve.
NOTES = ("severity_basis", "owner_resolution", "trigger_source", "previous_unavailable")


def _fmt_time(value: str) -> str:
    """Trimmed to seconds. Sub-second precision is never the thing being compared by
    eye, and it pushes the interesting columns off the line."""
    return (value or "")[:19].replace("T", " ")


def _headline(payload: dict) -> str:
    return " ".join(
        f"{k}={payload[k]}" for k in HEADLINE if payload.get(k) not in (None, "")
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster", default=config.CLUSTER_NAME)
    ap.add_argument("--hours", type=float, default=24.0, help="window back from now")
    ap.add_argument("--source", help="one source only")
    ap.add_argument("--namespace")
    ap.add_argument("--full", action="store_true", help="whole payload per signal")
    args = ap.parse_args()

    now = datetime.now(UTC)
    lz.bootstrap()

    started: dict[str, str] = {}

    with lz.connect() as conn:
        # Runs first, deliberately. Zero signals from a source that reported ok is a
        # quiet cluster; zero from one that reported unavailable is blindness, and the
        # signals table cannot tell them apart.
        print(f"collection runs, most recent per source ({args.cluster})")
        for source in sorted({r["source"] for r in conn.execute("SELECT DISTINCT source FROM collection_runs")}):
            run = lz.last_run(conn, source)
            if run is None:
                continue
            started[source] = run["started_at"] or ""
            print(
                f"  {source:<14} {run['status']:<12} {run['signal_count'] or 0:>4} signals"
                f"   {_fmt_time(run['finished_at'] or run['started_at'])}"
                f"   {run['error'] or ''}"
            )

        rows = lz.signals_in_window(
            conn,
            args.cluster,
            now - timedelta(hours=args.hours),
            now,
            namespace=args.namespace,
        )

    if args.source:
        rows = [r for r in rows if r["source"] == args.source]

    print(f"\n{len(rows)} signals in the last {args.hours:g}h")

    # A row that stopped being re-collected looks identical to a current one. Its
    # collected_at froze while everything else moved, which is how a resolved problem
    # and an ongoing one become indistinguishable in a prompt.
    #
    # Measured against when the last run started, not against the newest row. Every
    # signal in one batch stamps its own collected_at as it is constructed, so those
    # differ by microseconds and a max-of-rows comparison flags an entire healthy
    # batch as stale except its final row.

    by_source: dict[str, list] = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(row)

    for source in sorted(by_source):
        group = by_source[source]
        fingerprints = {r["fingerprint"] for r in group}
        subjects = {r["subject_name"] for r in group}
        print(f"\n{'=' * 78}")
        print(f"{source}   {len(group)} signals   {len(fingerprints)} fingerprints   {len(subjects)} subjects")
        print("=" * 78)

        # Sorted by subject so the same workload's rows sit together, then by time so a
        # sequence reads as a sequence.
        for row in sorted(group, key=lambda r: (r["subject_name"] or "", r["event_time"])):
            payload = json.loads(row["payload"] or "{}")
            visible = {k: v for k, v in payload.items() if k != PROVENANCE_KEY}
            provenance = payload.get(PROVENANCE_KEY) or {}
            owner = f"{row['owner_kind']}/{row['owner_name']}" if row["owner_name"] else "-"

            print(
                f"\n  {row['severity'] or '-':<8} {row['subject_kind']}/{row['subject_name']}"
                f"   owner={owner}   node={row['node_name'] or '-'}"
            )
            stale = (row["collected_at"] or "") < started.get(source, "")
            print(
                f"    fp={row['fingerprint'][:12]}  event={_fmt_time(row['event_time'])}"
                f"  collected={_fmt_time(row['collected_at'])}"
                f"{'  STALE' if stale else ''}"
                f"{'  BLOB' if row['blob_id'] else ''}{'  REDACTED' if row['redacted'] else ''}"
            )

            headline = _headline(visible)
            if headline:
                print(f"    {headline}")

            notes = " ".join(
                f"{k}={payload.get(k, provenance.get(k))}"
                for k in NOTES
                if payload.get(k, provenance.get(k)) is not None
            )
            if notes:
                print(f"    [{notes}]")

            if args.full:
                for key, value in sorted(payload.items()):
                    print(f"      {key}: {value}")
            else:
                for key in ("message", "excerpt", "error"):
                    if visible.get(key):
                        text = str(visible[key]).replace("\n", " | ")
                        print(f"    {key}: {text[:110]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
