"""
Freeze one bundle per golden subject, so the evaluation runs on evidence that cannot move.

  - Reads the buffer only. Run it straight after collecting from a lab with every fault
    applied, stage 5's actions included, so each subject's evidence is fresh.
  - Writes fixtures/<subject>.json in the snapshot format, and fixtures/manifest.json with
    each one's evidence receipt. run.py refuses a fixture that no longer matches it.
  - Refuses to write a fixture in which any redaction rule still fires. These files are
    committed, and a fixture is the one place a secret could leave the laptop in git.

    make -C oncall lab-all
    (stage 5 actions, wait two minutes)
    make -C oncall collect && sleep 60 && make -C oncall collect
    make -C oncall eval-freeze
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from oncall import config
from oncall import landing_zone as lz
from oncall.eval import cases
from oncall.evidence import bundle as assembler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster", default=config.CLUSTER_NAME)
    ap.add_argument("--root", type=Path, default=cases.EVAL_DIR)
    args = ap.parse_args(argv)

    golden = cases.load(args.root / "golden.yaml")
    fixtures = args.root / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, str] = {}
    problems = 0
    for case in golden:
        with lz.connect() as conn:
            b = assembler.assemble(conn, args.cluster, subject_name=cases.subject_of(case.subject))
        text = json.dumps(b.model_dump(mode="json"), indent=1, sort_keys=True)
        _, fired = assembler.redact_rules(text)
        if fired:
            print(f"  REFUSED {case.subject:18} redaction rules fired: {', '.join(fired)}")
            problems += 1
            continue
        (fixtures / f"{case.subject}.json").write_text(text + "\n")
        receipt = assembler.evidence_receipt(b)
        manifest[case.subject] = receipt
        note = "  (no findings: is this fault applied and collected?)" if not b.findings else ""
        print(f"  {case.subject:18} {len(b.findings):3} findings  {receipt[:16]}{note}")

    (fixtures / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(f"froze {len(manifest)} of {len(golden)} into {fixtures}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
