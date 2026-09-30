"""
One bundle, exactly as the reasoner will receive it.

  - Reads, never writes. The assembler opens no incident and attaches nothing, so
    running this repeatedly changes nothing it will later report.
  - Prints both receipts. Two runs over unchanged evidence should share bundle_sha256;
    if they do not, the receipt is hashing something other than the evidence.
  - --at pins the moment the window is derived from, so a receipt that moves between
    runs can be traced either to the evidence or to the clock.

    make -C oncall bundle SUBJECT=crashloop
    make -C oncall bundle ARGS="--at 2026-09-29T14:00:00+00:00"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from oncall import config
from oncall import landing_zone as lz
from oncall.evidence import bundle


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("subject", nargs="?", default=None,
                    help="subject name; omit for a cluster-wide bundle")
    ap.add_argument("--cluster", default=config.CLUSTER_NAME)
    ap.add_argument("--at", type=datetime.fromisoformat, default=None,
                    help="derive the window from this moment instead of now")
    ap.add_argument("--json", action="store_true",
                    help="print the receipt basis instead of the rendered view")
    args = ap.parse_args()

    # A naive time would be read as local by some paths and UTC by others, and the
    # window would then be off by the offset with nothing to say so.
    if args.at is not None and args.at.tzinfo is None:
        ap.error("--at needs an offset, e.g. 2026-09-29T14:00:00+00:00")

    with lz.connect() as conn:
        result = bundle.assemble(conn, args.cluster, subject_name=args.subject, now=args.at)

    text = bundle.render(result)
    if args.json:
        print(json.dumps(bundle.receipt_basis(result), indent=2, sort_keys=True, default=str))
    else:
        print(text)

    print()
    print(f"bundle_sha256  {bundle.evidence_receipt(result)}")
    print(f"render_sha256  {bundle.prompt_receipt(text)}   (render only; M5 wraps it)")
    print(f"window         {result.window_start.isoformat()} .. "
          f"{result.window_end.isoformat()}  ({result.window_basis})")
    print(f"candidates     {len(result.candidate_signal_ids)} signals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
