"""
One collection cycle, for every registered source, on demand.

  - A stand-in for the background scheduler, which DESIGN.md schedules after M6.
  - Prints what each source managed, because a run that collected nothing and a run
    that could not look are the distinction the whole three-state status exists for,
    and a bare exit code loses it.
  - Writes to the SQLite buffer only. The store is reached by shipper.py, so nothing
    here needs Postgres.

    make -C oncall collect
"""

from __future__ import annotations

import sys

from oncall import config
from oncall.collectors import registry
from oncall.envelope import SourceStatus


def main() -> int:
    print(f"cluster:    {config.CLUSTER_NAME}")
    print(f"namespaces: {', '.join(config.NAMESPACES) or 'all except ' + ', '.join(sorted(config.EXCLUDE_NAMESPACES))}")
    print(f"buffer:     {config.BUFFER_DB_PATH}")
    print()

    results = registry.run_all()

    unavailable = 0
    for source, result in results.items():
        if result is None:
            # The collector raised rather than returning unavailable, so the failure is
            # in the mapping and not in the cluster. Its run row is still open.
            print(f"  {str(source):<14} raised       (see the log; its run has no finished_at)")
            unavailable += 1
            continue

        status, count = result
        print(f"  {str(source):<14} {str(status):<12} {count} signals")
        if status is SourceStatus.UNAVAILABLE:
            unavailable += 1

    print()
    # Non-zero when any source could not look. An empty cluster is a success; a
    # cluster nobody could reach is not, and the two must not exit the same way.
    return 1 if unavailable else 0


if __name__ == "__main__":
    sys.exit(main())
