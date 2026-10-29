"""
Record an engineer's verdict on a diagnosis from the command line, as the UI's form does.

    make -C oncall feedback DIAGNOSIS=<id> VERDICT=wrong CAUSE=image_pull NOTES="tag typo"
    python -m oncall.eval.feedback <id> correct --remedy worked

verdict is correct, partial or wrong. --cause is the cause as found, from CauseClass;
omitted on a correct verdict it is the model's own rank-1 cause. A wrong verdict naming
the model's own cause is refused as a contradiction.
"""

from __future__ import annotations

import argparse
import sys

from oncall import landing_zone as lz
from oncall.eval import online


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record an engineer's verdict on a diagnosis.")
    ap.add_argument("diagnosis_id")
    ap.add_argument("verdict", choices=sorted(online.HUMAN_VERDICTS))
    ap.add_argument("--cause", default=None, help="the cause as found (a CauseClass value)")
    ap.add_argument("--remedy", default=None, choices=online.REMEDY_RESULTS)
    ap.add_argument("--notes", default=None)
    args = ap.parse_args(argv)

    lz.bootstrap()
    try:
        e = online.record_human(args.diagnosis_id, args.verdict, args.cause or None,
                                args.remedy, args.notes)
    except (LookupError, ValueError) as exc:
        print(f"not recorded: {exc}", file=sys.stderr)
        return 1
    match = e.details.get("top1_match")
    print(f"recorded {e.verdict} for {args.diagnosis_id}; cause {e.cause_class or '-'}"
          + ("" if match is None else f"; rank 1 {'matched' if match else 'did not match'}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
